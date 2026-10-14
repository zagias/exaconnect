"""Sales orders (ADR 0055): what an accepted quote turns into, and its fulfilment.

Each quote line becomes one order item. Items run in a fixed order (plan, then
sites, then links, then everything else), each in its own savepoint so one
failure never undoes the rest:

* ``subscription`` starts the plan (billing.plans);
* ``site`` adds the organisation's locations and their Connect sites;
* ``link`` adds a carrier link to a site, on the next free path;
* ``immediate`` needs nothing set up: the contract starts;
* ``manual`` (a box to ship, an installation) waits for ExaCarib staff to mark it done.

When an item is done its charges start: a one-off charge on this month's
invoice, or a contract item billed every month (or by use) from that day. Plan
fees, sites and link commits keep being billed from the plan price list, so a
quoted discount on them becomes a monthly credit instead. A failed item keeps
its error; staff fix the cause (or the item's details) and try again.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit, inventory, organisation
from ..billing import plans as plans_mod
from ..billing.core import BillingError, dec
from . import quotes, rating

RANK = {"subscription": 0, "site": 1, "link": 2, "immediate": 3, "manual": 4}
AUTO = ("subscription", "site", "link", "immediate")
PLAN_PRICED = ("subscription", "site", "link")
STATUS_LABELS = {
    "acknowledged": "Received",
    "in_progress": "In progress",
    "completed": "Completed",
    "partial": "Partly completed",
    "cancelled": "Cancelled",
}


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def create_from_quote(conn: psycopg.Connection, quote_id: Any, actor: str) -> dict:
    q = quotes._row(conn, quote_id)
    if conn.execute("SELECT 1 FROM sales_orders WHERE quote_id = %s", (q["id"],)).fetchone():
        raise BillingError(f"{q['number']} already has an order.", 409)
    lines = conn.execute(
        """SELECT l.*, i.fulfilment FROM quote_lines l JOIN catalogue_items i ON i.id = l.item_id
           WHERE l.quote_id = %s ORDER BY l.position""",
        (q["id"],),
    ).fetchall()
    order = conn.execute(
        """INSERT INTO sales_orders (number, customer_id, quote_id, currency, term_months, created_by)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (quotes._number(conn, "SO", "sales_orders"), q["customer_id"], q["id"], q["currency"], q["term_months"], actor),
    ).fetchone()
    ordered = sorted(lines, key=lambda x: (RANK[x["fulfilment"]], x["position"]))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO sales_order_items (order_id, customer_id, position, quote_line_id, item_id, code, name,
                 product, charge_type, fulfilment, quantity, config, description, amount, list_amount, price, discount)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    order["id"],
                    q["customer_id"],
                    i,
                    x["id"],
                    x["item_id"],
                    x["code"],
                    x["name"],
                    x["product"],
                    x["charge_type"],
                    x["fulfilment"],
                    x["quantity"],
                    Jsonb(x["config"]),
                    x["description"],
                    x["amount"],
                    x["list_amount"],
                    Jsonb(x["price"]),
                    Jsonb({"term_pct": f"{x['term_discount_pct']:f}", "pct": f"{x['discount_pct']:f}"}),
                )
                for i, x in enumerate(ordered, 1)
            ],
        )
    audit.record(conn, actor, "order.create", order["number"], q["customer_id"], {"quote": q["number"]})
    run(conn, order["id"], actor)
    return get(conn, order["id"])


# ---- fulfilment --------------------------------------------------------------------------


def run(conn: psycopg.Connection, order_id: Any, actor: str) -> dict:
    """Carry out every automatic item still pending, in order."""
    order = _order(conn, order_id, lock=True)
    items = conn.execute(
        "SELECT * FROM sales_order_items WHERE order_id = %s AND status = 'pending' ORDER BY position",
        (order["id"],),
    ).fetchall()
    for item in items:
        if item["fulfilment"] in AUTO:
            _attempt(conn, order, item, actor)
    _refresh(conn, order["id"])
    return get(conn, order["id"])


def _attempt(conn, order: dict, item: dict, actor: str) -> bool:
    try:
        with conn.transaction():
            result = _fulfil(conn, order, item, actor)
            _complete(conn, order, item, result, actor)
        return True
    except (BillingError, organisation.OrgSetupError, psycopg.DatabaseError) as e:
        msg = str(e).split("\n")[0][:300] if not isinstance(e, psycopg.DatabaseError) else "The database refused it."
        conn.execute(
            "UPDATE sales_order_items SET status = 'failed', error = %s WHERE id = %s",
            (msg, item["id"]),
        )
        audit.record(
            conn, actor, "order.item.failed", f"{order['number']} {item['name']}", order["customer_id"], {"error": msg}
        )
        return False


def _fulfil(conn, order: dict, item: dict, actor: str) -> dict:
    cid = order["customer_id"]
    kind = item["fulfilment"]
    if kind == "subscription":
        return _start_plan(conn, cid, item, actor)
    if kind == "site":
        return _add_sites(conn, cid, item, actor)
    if kind == "link":
        return _add_link(conn, cid, item, actor)
    return {}


def _start_plan(conn, cid, item: dict, actor: str) -> dict:
    cat = conn.execute("SELECT plan_id FROM catalogue_items WHERE id = %s", (item["item_id"],)).fetchone()
    plan = plans_mod.get_plan(conn, cat["plan_id"])
    today = _today()
    held = conn.execute(
        """SELECT s.*, p.name AS plan FROM subscriptions s JOIN plans p ON p.id = s.plan_id
           WHERE s.customer_id = %s AND s.product = %s AND (s.ends_on IS NULL OR s.ends_on > %s)
           ORDER BY s.starts_on LIMIT 1""",
        (cid, plan["product"], today),
    ).fetchone()
    if held:
        if held["plan_id"] == plan["id"]:
            return {"subscription_id": str(held["id"]), "note": f"Already on {plan['name']}."}
        raise BillingError(
            f"The organisation holds {held['plan']}. Change its plan under Billing > Plans, then try again.", 409
        )
    sub = plans_mod.subscribe(conn, cid, plan["id"], today, actor)
    return {"subscription_id": str(sub["id"]), "note": f"{plan['name']} starts {today:%d %B %Y}."}


def _add_sites(conn, cid, item: dict, actor: str) -> dict:
    if not plans_mod.holds(conn, cid, "connect"):
        raise BillingError("The organisation needs the Connect plan before sites can be added.", 409)
    names = list(item["config"].get("locations") or [])
    qty = int(item["quantity"])
    taken_locs = {
        r["name"].lower() for r in conn.execute("SELECT name FROM org_locations WHERE customer_id = %s", (cid,))
    }
    n = 1
    while len(names) < qty:
        candidate = f"Site {n}"
        if candidate.lower() not in taken_locs:
            names.append(candidate)
            taken_locs.add(candidate.lower())
        n += 1
    made = []
    for name in names:
        loc = conn.execute(
            "SELECT * FROM org_locations WHERE customer_id = %s AND lower(name) = lower(%s)", (cid, name)
        ).fetchone()
        if loc is None:
            loc = organisation.save_location(conn, cid, actor, {"name": name})
        site = conn.execute("SELECT id, name FROM sites WHERE org_location_id = %s", (loc["id"],)).fetchone()
        if site:
            made.append({"site_id": str(site["id"]), "site": site["name"], "location": loc["name"], "existing": True})
            continue
        made.append({**_new_site(conn, cid, loc, actor), "location": loc["name"]})
    return {"sites": made}


def _new_site(conn, cid, loc: dict, actor: str) -> dict:
    """A Connect site for a location, with no links yet (links follow as their own items)."""
    taken = conn.execute("SELECT name, asn, overlay_host FROM sites WHERE customer_id = %s", (cid,)).fetchall()
    names = {t["name"] for t in taken}
    base = organisation._slug(loc["name"])
    name, n = base, 2
    while name in names:
        name, n = f"{base}-{n}", n + 1
    asn = organisation._free({t["asn"] for t in taken}, *organisation.PRIVATE_ASN, "network number")
    host = organisation._free({t["overlay_host"] for t in taken}, *organisation.OVERLAY_HOSTS, "overlay address")
    sid = inventory.upsert_site(
        conn,
        cid,
        actor,
        name=name,
        kind="site",
        location=loc["city"] or loc["name"],
        timezone=loc["timezone"] or "UTC",
        asn=asn,
        lan_prefixes=[],
        overlay_host=host,
        latitude=loc["latitude"],
        longitude=loc["longitude"],
    )
    conn.execute("UPDATE sites SET org_location_id = %s WHERE id = %s", (loc["id"], sid))
    return {"site_id": str(sid), "site": name}


def _add_link(conn, cid, item: dict, actor: str) -> dict:
    cfg = item["config"]
    carrier = (cfg.get("carrier") or "").strip()
    if not carrier:
        raise BillingError("Name the link's carrier (edit the item's details), then try again.")
    where = (cfg.get("location") or "").strip()
    sites = conn.execute(
        """SELECT s.id, s.name, l.name AS location FROM sites s LEFT JOIN org_locations l ON l.id = s.org_location_id
           WHERE s.customer_id = %s AND s.kind = 'site' ORDER BY s.created_at""",
        (cid,),
    ).fetchall()
    if where:
        match = [s for s in sites if where.lower() in ((s["location"] or "").lower(), s["name"].lower())]
    else:
        match = sites if len(sites) == 1 else []
    if not match:
        raise BillingError(
            f"No Connect site at {where}." if where else "Say which location the link is for, then try again."
        )
    site = match[0]
    used = {r["path"] for r in conn.execute("SELECT path FROM links WHERE site_id = %s", (site["id"],))}
    sat = cfg.get("underlay_type") in organisation.SAT_TYPES
    options = [organisation.SATELLITE] if sat else list(organisation.TERRESTRIAL)
    free = [o for o in options if o[0] not in used]
    if not free:
        kind = "satellite link" if sat else "two terrestrial links"
        raise BillingError(f"{site['name']} already has its {kind}.", 409)
    path, iface = free[0]
    lid = inventory.upsert_link(
        conn,
        cid,
        site["id"],
        actor,
        carrier_id=inventory.ensure_carrier(conn, carrier[:120], actor),
        path=path,
        underlay_type=cfg.get("underlay_type") or "fibre",
        underlay_interface=iface,
        underlay_ip=None,
        commit_mbps=float(item["quantity"]),
        cost_per_mbps=0,
        burst_price=0,
    )
    return {"link_id": str(lid), "site_id": str(site["id"]), "site": site["name"], "path": path}


def _complete(conn, order: dict, item: dict, result: dict, actor: str, on: dt.date | None = None) -> None:
    """Mark an item done and start what it is charged."""
    on = on or _today()
    cid = order["customer_id"]
    conn.execute(
        """UPDATE sales_order_items SET status = 'done', result = %s, error = NULL, done_by = %s, done_at = now()
           WHERE id = %s""",
        (Jsonb(result), actor, item["id"]),
    )
    if item["fulfilment"] not in PLAN_PRICED and not plans_mod.holds(conn, cid, item["product"], on):
        name = plans_mod.PRODUCTS[item["product"]]
        raise BillingError(f"The organisation needs a {name} plan before {item['name']} can be charged.", 409)
    disc = item["discount"] or {}
    combined = rating.combined_pct(disc.get("term_pct") or 0, disc.get("pct") or 0)
    base = {
        "customer_id": cid,
        "order_item_id": item["id"],
        "item_id": item["item_id"],
        "product": item["product"],
        "code": item["code"],
        "currency": order["currency"],
        "starts_on": on,
        "term_months": order["term_months"],
        "created_by": actor,
    }
    if item["charge_type"] == "one_off":
        conn.execute(
            """INSERT INTO one_off_charges (customer_id, order_item_id, product, code, description, quantity, unit,
                 amount, currency, charge_on, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                cid,
                item["id"],
                item["product"],
                item["code"],
                item["description"],
                item["quantity"],
                _unit(conn, item),
                item["amount"],
                order["currency"],
                on,
                actor,
            ),
        )
        return
    if item["fulfilment"] in PLAN_PRICED:
        # Billed from the plan price list; only a discount needs a line of its own.
        off = dec(item["list_amount"]) - dec(item["amount"])
        if off <= 0:
            return
        pct = rating._text(combined)
        if item["fulfilment"] == "site" and result.get("sites"):
            each = rating.q4(off / len(result["sites"]))
            for s in result["sites"]:
                _contract(
                    conn,
                    {
                        **base,
                        "description": f"Discount {pct}%: site {s['site']}",
                        "monthly_amount": -each,
                        "site_id": s["site_id"],
                        "follows": "site",
                        "quantity": 1,
                        "unit": "site",
                        "discount_pct": combined,
                    },
                )
        elif item["fulfilment"] == "link" and result.get("link_id"):
            _contract(
                conn,
                {
                    **base,
                    "description": f"Discount {pct}%: {item['description']}",
                    "monthly_amount": -off,
                    "link_id": result["link_id"],
                    "follows": "link",
                    "quantity": item["quantity"],
                    "unit": "Mbps",
                    "discount_pct": combined,
                },
            )
        else:
            _contract(
                conn,
                {
                    **base,
                    "description": f"Discount {pct}%: {item['description']}",
                    "monthly_amount": -off,
                    "quantity": 1,
                    "unit": "month",
                    "discount_pct": combined,
                },
            )
        return
    if item["charge_type"] == "recurring":
        _contract(
            conn,
            {
                **base,
                "description": item["description"],
                "monthly_amount": item["amount"],
                "quantity": item["quantity"],
                "unit": _unit(conn, item),
                "discount_pct": combined,
                "price": item["price"],
            },
        )
        return
    meter = conn.execute("SELECT meter FROM catalogue_items WHERE id = %s", (item["item_id"],)).fetchone()["meter"]
    _contract(
        conn,
        {
            **base,
            "charge_type": "usage",
            "description": item["name"],
            "meter": meter,
            "price": item["price"],
            "quantity": 0,
            "unit": _unit(conn, item),
            "discount_pct": combined,
            "monthly_amount": 0,
        },
    )


def _unit(conn, item: dict) -> str:
    return conn.execute("SELECT unit FROM catalogue_items WHERE id = %s", (item["item_id"],)).fetchone()["unit"]


def _contract(conn, row: dict) -> None:
    row = {"charge_type": "recurring", **row}
    if "price" in row:
        row["price"] = Jsonb(row["price"] or {})
    cols = list(row)
    conn.execute(
        f"INSERT INTO contract_items ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        tuple(row.values()),
    )


def _refresh(conn, order_id: Any) -> str:
    counts = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM sales_order_items WHERE order_id = %s GROUP BY status", (order_id,)
        )
    }
    done, open_ = counts.get("done", 0), counts.get("pending", 0) + counts.get("failed", 0)
    if open_:
        status = "in_progress" if done else "acknowledged"
    elif done and counts.get("cancelled", 0):
        status = "partial"
    elif done:
        status = "completed"
    else:
        status = "cancelled"
    conn.execute(
        """UPDATE sales_orders SET status = %s, updated_at = now(),
             completed_at = CASE WHEN %s IN ('completed', 'partial') THEN coalesce(completed_at, now()) END
           WHERE id = %s""",
        (status, status, order_id),
    )
    return status


# ---- staff actions ---------------------------------------------------------------------------


def _item(conn, item_id: Any, lock: bool = True) -> tuple[dict, dict]:
    try:
        item = conn.execute(
            "SELECT * FROM sales_order_items WHERE id = %s" + (" FOR UPDATE" if lock else ""), (str(item_id),)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        item = None
    if item is None:
        raise BillingError("Order item not found.", 404)
    return _order(conn, item["order_id"], lock=lock), item


def mark_done(conn: psycopg.Connection, item_id: Any, note: str, actor: str) -> dict:
    """Staff mark a manual item done (the box shipped, the site installed), or one
    they finished by hand after it failed. Its charges start today."""
    order, item = _item(conn, item_id)
    if item["status"] not in ("pending", "failed"):
        raise BillingError(f"{item['name']} is already {item['status']}.", 409)
    note = str(note or "").strip()[:300]
    _complete(conn, order, item, {"note": note} if note else {}, actor)
    audit.record(
        conn, actor, "order.item.done", f"{order['number']} {item['name']}", order["customer_id"], {"note": note}
    )
    _refresh(conn, order["id"])
    return get(conn, order["id"])


def retry(conn: psycopg.Connection, item_id: Any, actor: str, config: dict | None = None) -> dict:
    """Try a failed automatic item again, optionally with corrected details."""
    order, item = _item(conn, item_id)
    if item["status"] not in ("failed", "pending"):
        raise BillingError(f"{item['name']} is {item['status']}.", 409)
    if item["fulfilment"] not in AUTO:
        raise BillingError(f"{item['name']} is done by ExaCarib staff: mark it done when it is.", 409)
    if config is not None:
        cat = conn.execute("SELECT * FROM catalogue_items WHERE id = %s", (item["item_id"],)).fetchone()
        clean = quotes._clean_config(cat, config, dec(item["quantity"]))
        conn.execute("UPDATE sales_order_items SET config = %s WHERE id = %s", (Jsonb(clean), item["id"]))
        item = {**item, "config": clean}
    conn.execute("UPDATE sales_order_items SET status = 'pending', error = NULL WHERE id = %s", (item["id"],))
    ok = _attempt(conn, order, {**item, "status": "pending"}, actor)
    audit.record(conn, actor, "order.item.retry", f"{order['number']} {item['name']}", order["customer_id"], {"ok": ok})
    _refresh(conn, order["id"])
    return get(conn, order["id"])


def cancel_item(conn: psycopg.Connection, item_id: Any, reason: str, actor: str) -> dict:
    order, item = _item(conn, item_id)
    if item["status"] not in ("pending", "failed"):
        raise BillingError(f"{item['name']} is {item['status']}: end its contract item instead.", 409)
    conn.execute(
        "UPDATE sales_order_items SET status = 'cancelled', error = %s, done_by = %s, done_at = now() WHERE id = %s",
        (str(reason or "Cancelled")[:300], actor, item["id"]),
    )
    audit.record(
        conn,
        actor,
        "order.item.cancel",
        f"{order['number']} {item['name']}",
        order["customer_id"],
        {"reason": str(reason or "")[:300]},
    )
    _refresh(conn, order["id"])
    return get(conn, order["id"])


def cancel(conn: psycopg.Connection, order_id: Any, reason: str, actor: str) -> dict:
    """Cancel what hasn't been done. Done items stay, with their charges."""
    order = _order(conn, order_id, lock=True)
    reason = str(reason or "").strip()[:300]
    if not reason:
        raise BillingError("Say why the order is cancelled.")
    n = conn.execute(
        """UPDATE sales_order_items SET status = 'cancelled', error = %s, done_by = %s, done_at = now()
           WHERE order_id = %s AND status IN ('pending', 'failed')""",
        (reason, actor, order["id"]),
    ).rowcount
    if not n:
        raise BillingError(f"Everything on {order['number']} is already done or cancelled.", 409)
    conn.execute(
        "UPDATE sales_orders SET cancelled_by = %s, cancelled_at = now(), cancel_reason = %s WHERE id = %s",
        (actor, reason, order["id"]),
    )
    audit.record(conn, actor, "order.cancel", order["number"], order["customer_id"], {"reason": reason, "items": n})
    _refresh(conn, order["id"])
    return get(conn, order["id"])


# ---- reading -----------------------------------------------------------------------------


def _order(conn, order_id: Any, customer_id: Any | None = None, lock: bool = False) -> dict:
    try:
        row = conn.execute(
            "SELECT * FROM sales_orders WHERE id = %s" + (" FOR UPDATE" if lock else ""), (str(order_id),)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None or (customer_id is not None and str(row["customer_id"]) != str(customer_id)):
        raise BillingError("Order not found.", 404)
    return row


def get(conn: psycopg.Connection, order_id: Any, customer_id: Any | None = None) -> dict:
    o = _order(conn, order_id, customer_id)
    cust = conn.execute("SELECT name FROM customers WHERE id = %s", (o["customer_id"],)).fetchone()
    quote = conn.execute("SELECT id, number, title FROM quotes WHERE id = %s", (o["quote_id"],)).fetchone()
    items = conn.execute("SELECT * FROM sales_order_items WHERE order_id = %s ORDER BY position", (o["id"],)).fetchall()
    return {
        **o,
        "customer": cust["name"] if cust else "",
        "quote": quote,
        "status_label": STATUS_LABELS[o["status"]],
        "items": [{**i, "automatic": i["fulfilment"] in AUTO} for i in items],
    }


def list_orders(conn: psycopg.Connection, customer_id: Any | None, status: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT o.*, c.name AS customer, q.number AS quote_number,
                  count(i.*) AS item_count,
                  count(i.*) FILTER (WHERE i.status = 'done') AS done_count,
                  count(i.*) FILTER (WHERE i.status = 'failed') AS failed_count,
                  count(i.*) FILTER (WHERE i.status = 'pending' AND i.fulfilment = 'manual') AS waiting_count
           FROM sales_orders o JOIN customers c ON c.id = o.customer_id
           LEFT JOIN quotes q ON q.id = o.quote_id
           LEFT JOIN sales_order_items i ON i.order_id = o.id
           WHERE (%(c)s::uuid IS NULL OR o.customer_id = %(c)s) AND (%(s)s::text IS NULL OR o.status = %(s)s)
           GROUP BY o.id, c.name, q.number ORDER BY o.created_at DESC LIMIT 500""",
        {"c": customer_id, "s": status},
    ).fetchall()
    return [{**r, "status_label": STATUS_LABELS[r["status"]]} for r in rows]
