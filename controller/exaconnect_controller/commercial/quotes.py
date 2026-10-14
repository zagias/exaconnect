"""Quotes (CPQ, ADR 0055): configure items from the catalogue, price them with
the rating engine, approve discounts, send, and accept into a sales order.

A quote moves draft -> (pending_approval -> approved) -> sent -> accepted or
declined; it can be cancelled until it is accepted or declined. Changing a
quote takes it back to draft and clears any approval. A quote past its
"valid until" date reads as expired and can't be sent or accepted; copy it to
get a fresh one at today's prices.

ExaCarib staff build, discount and send quotes. An organisation's owners and
admins can also build one from the catalogue at list price (no discounts),
send it to themselves and accept it: self-service ordering.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit
from ..billing.core import BillingError, dec, money_text
from ..metering.core import money
from . import catalogue, rating

OPEN = ("draft", "pending_approval", "approved", "sent")
EDITABLE = ("draft", "pending_approval", "approved")
MAX_LINES = 50
UNDERLAYS = ("fibre", "broadband", "lte", "leo", "geo")
STATUS_LABELS = {
    "draft": "Draft",
    "pending_approval": "Waiting for approval",
    "approved": "Approved",
    "sent": "Sent",
    "accepted": "Accepted",
    "declined": "Declined",
    "cancelled": "Cancelled",
    "expired": "Expired",
}


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _customer(conn, customer_id) -> dict:
    row = conn.execute("SELECT id, name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    if row is None:
        raise BillingError("Customer not found.", 404)
    return row


# ---- configuring and pricing one line ----------------------------------------------------


def _text(v: Any, n: int) -> str:
    return str(v or "").strip()[:n]


def _clean_config(item: dict, raw: Any, qty: Decimal) -> dict:
    """What a line needs to be fulfilled: the locations a site item makes, the
    location, carrier and underlay of a link. Other items take a short note."""
    raw = raw if isinstance(raw, dict) else {}
    fam = item["fulfilment"]
    if fam == "subscription":
        if qty != 1:
            raise BillingError(f"{item['name']}: a plan is ordered once.")
        return {}
    if fam == "site":
        if qty != qty.to_integral_value() or not 1 <= qty <= 50:
            raise BillingError(f"{item['name']}: order between 1 and 50 sites.")
        names = []
        for x in raw.get("locations") or []:
            name = _text(x.get("name") if isinstance(x, dict) else x, 120)
            if name:
                names.append(name)
        if len({n.lower() for n in names}) != len(names):
            raise BillingError(f"{item['name']}: each location appears once.")
        if names and len(names) != qty:
            raise BillingError(f"{item['name']}: name {int(qty)} location(s), one for each site, or none.")
        return {"locations": names}
    if fam == "link":
        if not 1 <= qty <= 100_000:
            raise BillingError(f"{item['name']}: the commit is between 1 and 100,000 Mbps.")
        out = {
            "location": _text(raw.get("location"), 120),
            "carrier": _text(raw.get("carrier"), 120),
            "underlay_type": raw.get("underlay_type") or "fibre",
        }
        if out["underlay_type"] not in UNDERLAYS:
            raise BillingError(f"{item['name']}: the link type is fibre, broadband, lte, leo or geo.")
        return out
    note = _text(raw.get("note"), 300)
    return {"note": note} if note else {}


def units(qty: Decimal, unit: str) -> str:
    """'5 hours', '1 hour', '100 Mbps'."""
    q = f"{qty.normalize():f}"
    plural = unit if qty == 1 or unit in ("Mbps", "GB", "each") or unit.endswith("s") else unit + "s"
    return f"{q} {plural}"


def _line_description(item: dict, qty: Decimal, config: dict) -> str:
    q = f"{qty.normalize():f}"
    if item["fulfilment"] == "site":
        where = f": {', '.join(config['locations'])}" if config.get("locations") else ""
        return f"{item['name']} x {q}{where}"
    if item["fulfilment"] == "link":
        bits = [b for b in (config.get("carrier"), config.get("underlay_type"), config.get("location")) if b]
        return f"{item['name']}, {q} Mbps" + (f" ({', '.join(bits)})" if bits else "")
    if item["charge_type"] == "usage":
        return f"{item['name']} (by use; about {units(qty, item['unit'])} a month)"
    if qty == 1:
        return item["name"]
    if item["unit"] in ("each", "month", "box", "site", "seat"):
        return f"{item['name']} x {q}"
    return f"{item['name']}, {units(qty, item['unit'])}"


def price_line(
    conn: psycopg.Connection,
    customer_id: Any,
    currency: str,
    term_months: int,
    raw: dict,
    staff: bool,
    on: dt.date | None = None,
) -> dict:
    """One quote line priced: list amount, term and manual discounts, the amount,
    and the rating engine's working. Usage lines are priced at the expected
    use, shown as an estimate and left out of the totals."""
    on = on or _today()
    if not isinstance(raw, dict):
        raise BillingError("Each line needs an item and a quantity.")
    item = catalogue.get_item(conn, raw.get("item") or raw.get("code") or raw.get("item_id") or "")
    if not item["active"]:
        raise BillingError(f"{item['name']} is no longer sold.", 409)
    qty = dec(raw.get("quantity") if raw.get("quantity") not in (None, "") else 1)
    if qty <= 0 or qty > 1_000_000:
        raise BillingError(f"{item['name']}: the quantity must be above 0.")
    pct = dec(raw.get("discount_pct") or 0)
    if not 0 <= pct <= 100:
        raise BillingError(f"{item['name']}: a discount is between 0% and 100%.")
    if pct > 0 and not staff:
        raise BillingError("Only ExaCarib can give discounts. Ask your ExaCarib contact for a quote.", 403)
    config = _clean_config(item, raw.get("config"), qty)
    if item["price_source"] == "catalogue":
        row = catalogue.current_price(conn, item["id"], currency, on)
        if row is None:
            raise BillingError(f"{item['name']} has no price in {currency} yet.", 409)
        price = rating.validate_price(
            {k: row[k] for k in ("model", "unit_price", "tiers", "included_qty", "minimum", "term_discounts")}
        )
        price.update(source="catalogue", version=row["version"], currency=currency, example=row["example"])
        price_id = row["id"]
    else:
        price = catalogue.plan_price(conn, customer_id, item, on)
        if price["currency"] != currency:
            raise BillingError(
                f"{item['name']} is priced in {price['currency']} on the {price['plan']} price list. "
                f"Make the quote in {price['currency']}.",
                409,
            )
        price_id = None
    rated = rating.rate(price, qty)
    term_pct = rating.term_discount_pct(price, term_months) if item["charge_type"] == "recurring" else Decimal(0)
    amount = rating.discounted(rated["amount"], term_pct, pct)
    description = _text(raw.get("description"), 200) or _line_description(item, qty, config)
    return {
        "item_id": item["id"],
        "price_id": price_id,
        "code": item["code"],
        "name": item["name"],
        "product": item["product"],
        "charge_type": item["charge_type"],
        "fulfilment": item["fulfilment"],
        "unit": item["unit"],
        "quantity": qty,
        "config": config,
        "description": description,
        "list_amount": rated["amount"],
        "term_discount_pct": term_pct,
        "discount_pct": pct,
        "amount": amount,
        "price": price,
        "rating": rating_json(rated),
        "needs_approval": rating.needs_approval(pct, item["max_discount_pct"]),
        "max_discount_pct": item["max_discount_pct"],
        "example": bool(item["example"] or price.get("example")),
    }


def rating_json(rated: dict) -> dict:
    return {k: (f"{v:f}" if isinstance(v, Decimal) else v) for k, v in rated.items()}


def quote_totals(lines: list[dict], term_months: int) -> dict:
    one_off = sum((dec(x["amount"]) for x in lines if x["charge_type"] == "one_off"), Decimal(0))
    monthly = sum((dec(x["amount"]) for x in lines if x["charge_type"] == "recurring"), Decimal(0))
    usage = sum((dec(x["amount"]) for x in lines if x["charge_type"] == "usage"), Decimal(0))
    return {
        "one_off_total": money(one_off),
        "monthly_total": money(monthly),
        "usage_estimate": money(usage),
        "contract_total": money(one_off + monthly * term_months),
    }


def preview(conn: psycopg.Connection, customer_id: Any, values: dict, staff: bool) -> dict:
    """Price lines without saving anything (the quote builder's live total)."""
    head = _clean_head(values, None)
    lines = [price_line(conn, customer_id, head["currency"], head["term_months"], x, staff) for x in _raw_lines(values)]
    return {
        **head,
        "lines": lines,
        **quote_totals(lines, head["term_months"]),
        "needs_approval": any(x["needs_approval"] for x in lines),
        "example": any(x["example"] for x in lines),
    }


# ---- quotes ------------------------------------------------------------------------------


def _raw_lines(values: dict) -> list[dict]:
    raw = values.get("lines") or []
    if not isinstance(raw, list):
        raise BillingError("Lines must be a list.")
    if len(raw) > MAX_LINES:
        raise BillingError(f"Keep a quote to {MAX_LINES} lines or fewer.")
    return raw


def _clean_head(values: dict, old: dict | None) -> dict:
    cur = str(values.get("currency") or (old or {}).get("currency") or "USD").upper()
    if not re.match(r"^[A-Z]{3}$", cur):
        raise BillingError("Give the currency as three letters, such as USD.")
    term = int(values.get("term_months") or (old or {}).get("term_months") or 12)
    if not 1 <= term <= 120:
        raise BillingError("The contract term is between 1 and 120 months.")
    valid = values.get("valid_until") or (old or {}).get("valid_until")
    if valid in (None, ""):
        valid = _today() + dt.timedelta(days=30)
    try:
        valid = valid if isinstance(valid, dt.date) else dt.date.fromisoformat(str(valid))
    except ValueError as e:
        raise BillingError("Give 'valid until' as YYYY-MM-DD.") from e
    if valid < _today() and (old is None or valid != old.get("valid_until")):
        raise BillingError("'Valid until' can't be in the past.")
    if valid > _today() + dt.timedelta(days=366):
        raise BillingError("A quote can be valid for up to a year.")
    title = _text(values.get("title") if "title" in values else (old or {}).get("title"), 120)
    notes = str(values.get("notes") if "notes" in values else (old or {}).get("notes") or "")[:4000]
    return {"currency": cur, "term_months": term, "valid_until": valid, "title": title, "notes": notes}


def _number(conn, prefix: str, table: str) -> str:
    year = _today().year
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{table}_number",))
    last = conn.execute(
        f"""SELECT max(split_part(number, '-', 3)::int) AS n FROM {table}
            WHERE number LIKE %s""",
        (f"{prefix}-{year}-%",),
    ).fetchone()["n"]
    return f"{prefix}-{year}-{(last or 0) + 1:04d}"


def _write_lines(conn, quote_id: Any, lines: list[dict]) -> None:
    conn.execute("DELETE FROM quote_lines WHERE quote_id = %s", (quote_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO quote_lines (quote_id, position, item_id, price_id, code, name, product, charge_type, unit,
                 quantity, config, description, list_amount, term_discount_pct, discount_pct, amount, price, rating)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    quote_id,
                    i,
                    x["item_id"],
                    x["price_id"],
                    x["code"],
                    x["name"],
                    x["product"],
                    x["charge_type"],
                    x["unit"],
                    x["quantity"],
                    Jsonb(x["config"]),
                    x["description"],
                    x["list_amount"],
                    x["term_discount_pct"],
                    x["discount_pct"],
                    x["amount"],
                    Jsonb(_jsonable(x["price"])),
                    Jsonb(x["rating"]),
                )
                for i, x in enumerate(lines, 1)
            ],
        )


def _jsonable(v: Any) -> Any:
    from ..billing.service import jsonable

    return jsonable(v)


def create(conn: psycopg.Connection, customer_id: Any, values: dict, actor: str, staff: bool) -> dict:
    cust = _customer(conn, customer_id)
    head = _clean_head(values, None)
    lines = [price_line(conn, customer_id, head["currency"], head["term_months"], x, staff) for x in _raw_lines(values)]
    totals = quote_totals(lines, head["term_months"])
    row = conn.execute(
        """INSERT INTO quotes (number, customer_id, title, currency, term_months, valid_until, notes, one_off_total,
                               monthly_total, contract_total, needs_approval, example, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            _number(conn, "Q", "quotes"),
            customer_id,
            head["title"],
            head["currency"],
            head["term_months"],
            head["valid_until"],
            head["notes"],
            totals["one_off_total"],
            totals["monthly_total"],
            totals["contract_total"],
            any(x["needs_approval"] for x in lines),
            any(x["example"] for x in lines),
            actor,
        ),
    ).fetchone()
    _write_lines(conn, row["id"], lines)
    audit.record(
        conn,
        actor,
        "quote.create",
        f"{row['number']} {cust['name']}",
        customer_id,
        {"total": str(totals["contract_total"])},
    )
    return get(conn, row["id"])


def _row(conn, quote_id: Any, customer_id: Any | None = None, lock: bool = False) -> dict:
    try:
        row = conn.execute(
            "SELECT * FROM quotes WHERE id = %s" + (" FOR UPDATE" if lock else ""), (str(quote_id),)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None or (customer_id is not None and str(row["customer_id"]) != str(customer_id)):
        raise BillingError("Quote not found.", 404)
    return row


def state(row: dict) -> str:
    """The status a person sees: an open quote past its date reads as expired."""
    if row["status"] in OPEN and row["valid_until"] < _today():
        return "expired"
    return row["status"]


def get(conn: psycopg.Connection, quote_id: Any, customer_id: Any | None = None) -> dict:
    q = _row(conn, quote_id, customer_id)
    cust = _customer(conn, q["customer_id"])
    lines = conn.execute("SELECT * FROM quote_lines WHERE quote_id = %s ORDER BY position", (q["id"],)).fetchall()
    items = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, max_discount_pct, fulfilment, example FROM catalogue_items WHERE id = ANY(%s)",
            ([x["item_id"] for x in lines],),
        ).fetchall()
    }
    out_lines = []
    for x in lines:
        it = items.get(x["item_id"], {})
        out_lines.append(
            {
                **x,
                "fulfilment": it.get("fulfilment"),
                "max_discount_pct": it.get("max_discount_pct"),
                "needs_approval": bool(it) and rating.needs_approval(x["discount_pct"], it["max_discount_pct"]),
                "price_text": rating.describe(x["price"], x["unit"], q["currency"]) if x["price"] else "",
            }
        )
    order = conn.execute("SELECT id, number, status FROM sales_orders WHERE quote_id = %s", (q["id"],)).fetchone()
    usage = sum((dec(x["amount"]) for x in lines if x["charge_type"] == "usage"), Decimal(0))
    return {
        **q,
        "customer": cust["name"],
        "state": state(q),
        "state_label": STATUS_LABELS[state(q)],
        "usage_estimate": money(usage),
        "lines": out_lines,
        "order": order,
    }


def list_quotes(
    conn: psycopg.Connection, customer_id: Any | None, staff: bool, status: str | None = None
) -> list[dict]:
    """ExaCarib sees every quote; an organisation sees what was sent to it and
    the quotes its own people built."""
    rows = conn.execute(
        """SELECT q.*, c.name AS customer, o.number AS order_number, o.id AS order_id,
                  (SELECT count(*) FROM quote_lines l WHERE l.quote_id = q.id) AS line_count
           FROM quotes q JOIN customers c ON c.id = q.customer_id
           LEFT JOIN sales_orders o ON o.quote_id = q.id
           WHERE (%(c)s::uuid IS NULL OR q.customer_id = %(c)s)
           ORDER BY q.created_at DESC LIMIT 500""",
        {"c": customer_id},
    ).fetchall()
    out = []
    for r in rows:
        s = state(r)
        if status and s != status:
            continue
        if not visible(conn, r, staff):
            continue
        out.append({**r, "state": s, "state_label": STATUS_LABELS[s]})
    return out


def _customer_made(conn, q: dict) -> bool:
    """Built by someone in the organisation itself (self-service), not by ExaCarib."""
    if not q["created_by"].startswith("user:"):
        return False
    row = conn.execute("SELECT role FROM users WHERE email = %s", (q["created_by"][5:],)).fetchone()
    return row is not None and row["role"] == "customer"


def visible(conn, q: dict, staff: bool) -> bool:
    return staff or q["status"] in ("sent", "accepted", "declined") or _customer_made(conn, q)


def update(
    conn: psycopg.Connection, quote_id: Any, values: dict, actor: str, staff: bool, customer_id: Any = None
) -> dict:
    """Change a quote that hasn't been sent. It goes back to draft; any approval
    is cleared, and the lines are priced again at today's prices."""
    q = _row(conn, quote_id, customer_id, lock=True)
    if not staff and not _customer_made(conn, q):
        raise BillingError("Quote not found.", 404)
    if q["status"] not in EDITABLE:
        raise BillingError(f"This quote is {STATUS_LABELS[q['status']].lower()} and can't be changed. Copy it.", 409)
    head = _clean_head(values, q)
    raw = _raw_lines(values) if "lines" in values else _stored_raw(conn, q["id"])
    lines = [price_line(conn, q["customer_id"], head["currency"], head["term_months"], x, staff) for x in raw]
    totals = quote_totals(lines, head["term_months"])
    conn.execute(
        """UPDATE quotes SET title = %s, currency = %s, term_months = %s, valid_until = %s, notes = %s,
             one_off_total = %s, monthly_total = %s, contract_total = %s, needs_approval = %s, example = %s,
             status = 'draft', approved_by = NULL, approved_at = NULL, approval_note = NULL, updated_at = now()
           WHERE id = %s""",
        (
            head["title"],
            head["currency"],
            head["term_months"],
            head["valid_until"],
            head["notes"],
            totals["one_off_total"],
            totals["monthly_total"],
            totals["contract_total"],
            any(x["needs_approval"] for x in lines),
            any(x["example"] for x in lines),
            q["id"],
        ),
    )
    _write_lines(conn, q["id"], lines)
    audit.record(conn, actor, "quote.update", q["number"], q["customer_id"], {"total": str(totals["contract_total"])})
    return get(conn, q["id"])


def _stored_raw(conn, quote_id: Any) -> list[dict]:
    rows = conn.execute("SELECT * FROM quote_lines WHERE quote_id = %s ORDER BY position", (quote_id,)).fetchall()
    return [
        {
            "item": str(r["item_id"]),
            "quantity": r["quantity"],
            "discount_pct": r["discount_pct"],
            "config": r["config"],
            "description": r["description"],
        }
        for r in rows
    ]


def copy(conn: psycopg.Connection, quote_id: Any, actor: str, staff: bool, customer_id: Any = None) -> dict:
    """A new draft with the same lines at today's prices (for an expired or declined quote)."""
    q = _row(conn, quote_id, customer_id)
    if not visible(conn, q, staff):
        raise BillingError("Quote not found.", 404)
    raw = _stored_raw(conn, q["id"])
    if not staff:
        raw = [{**r, "discount_pct": 0} for r in raw]
    return create(
        conn,
        q["customer_id"],
        {
            "title": q["title"],
            "currency": q["currency"],
            "term_months": q["term_months"],
            "notes": q["notes"],
            "lines": raw,
        },
        actor,
        staff,
    )


def _open_check(q: dict, *allowed: str) -> None:
    s = state(q)
    if s == "expired":
        raise BillingError(f"{q['number']} expired on {q['valid_until']:%d %B %Y}. Copy it for today's prices.", 409)
    if q["status"] not in allowed:
        raise BillingError(f"{q['number']} is {STATUS_LABELS[q['status']].lower()}.", 409)


def submit(conn: psycopg.Connection, quote_id: Any, actor: str) -> dict:
    """Ask an ExaCarib admin to approve discounts above what the items allow."""
    q = _row(conn, quote_id, lock=True)
    _open_check(q, "draft")
    if not q["needs_approval"]:
        raise BillingError("This quote's discounts are within limits: send it straight away.", 409)
    conn.execute("UPDATE quotes SET status = 'pending_approval', updated_at = now() WHERE id = %s", (q["id"],))
    audit.record(conn, actor, "quote.submit", q["number"], q["customer_id"])
    return get(conn, q["id"])


def approve(conn: psycopg.Connection, quote_id: Any, approve_it: bool, note: str, actor: str) -> dict:
    q = _row(conn, quote_id, lock=True)
    _open_check(q, "pending_approval")
    note = _text(note, 500)
    if not note:
        raise BillingError("Say why, so the decision is on record.")
    if approve_it:
        conn.execute(
            """UPDATE quotes SET status = 'approved', approved_by = %s, approved_at = now(), approval_note = %s,
                 updated_at = now() WHERE id = %s""",
            (actor, note, q["id"]),
        )
    else:
        conn.execute(
            "UPDATE quotes SET status = 'draft', approval_note = %s, updated_at = now() WHERE id = %s", (note, q["id"])
        )
    audit.record(
        conn, actor, "quote.approve" if approve_it else "quote.reject", q["number"], q["customer_id"], {"note": note}
    )
    return get(conn, q["id"])


def send(conn: psycopg.Connection, quote_id: Any, actor: str, staff: bool, customer_id: Any = None) -> dict:
    q = _row(conn, quote_id, customer_id, lock=True)
    if not staff and not _customer_made(conn, q):
        raise BillingError("Quote not found.", 404)
    _open_check(q, "draft", "approved")
    if q["status"] == "draft" and q["needs_approval"]:
        raise BillingError("Some discounts are above what the items allow. Ask for approval first.", 409)
    if not conn.execute("SELECT 1 FROM quote_lines WHERE quote_id = %s", (q["id"],)).fetchone():
        raise BillingError("Add at least one item first.")
    conn.execute(
        "UPDATE quotes SET status = 'sent', sent_by = %s, sent_at = now(), updated_at = now() WHERE id = %s",
        (actor, q["id"]),
    )
    audit.record(conn, actor, "quote.send", q["number"], q["customer_id"])
    return get(conn, q["id"])


def accept(conn: psycopg.Connection, quote_id: Any, name: str, actor: str, customer_id: Any = None) -> dict:
    """Accept a sent quote: it becomes a sales order, whose automatic steps run
    straight away. The person types their name, which goes on record."""
    from . import orders

    q = _row(conn, quote_id, customer_id, lock=True)
    _open_check(q, "sent")
    name = _text(name, 120)
    if len(name) < 2:
        raise BillingError("Type your full name to accept the quote.")
    conn.execute(
        """UPDATE quotes SET status = 'accepted', accepted_by = %s, accepted_name = %s, accepted_at = now(),
             updated_at = now() WHERE id = %s""",
        (actor, name, q["id"]),
    )
    audit.record(conn, actor, "quote.accept", q["number"], q["customer_id"], {"name": name})
    order = orders.create_from_quote(conn, q["id"], actor)
    return {"quote": get(conn, q["id"]), "order": order}


def decline(conn: psycopg.Connection, quote_id: Any, reason: str, actor: str, customer_id: Any = None) -> dict:
    q = _row(conn, quote_id, customer_id, lock=True)
    _open_check(q, "sent")
    conn.execute(
        """UPDATE quotes SET status = 'declined', declined_by = %s, declined_at = now(), decline_reason = %s,
             updated_at = now() WHERE id = %s""",
        (actor, _text(reason, 500), q["id"]),
    )
    audit.record(conn, actor, "quote.decline", q["number"], q["customer_id"], {"reason": _text(reason, 500)})
    return get(conn, q["id"])


def cancel(conn: psycopg.Connection, quote_id: Any, actor: str, staff: bool, customer_id: Any = None) -> dict:
    q = _row(conn, quote_id, customer_id, lock=True)
    if not staff and not _customer_made(conn, q):
        raise BillingError("Only ExaCarib can withdraw a quote it sent. Decline it instead.", 403)
    if q["status"] not in OPEN:
        raise BillingError(f"{q['number']} is {STATUS_LABELS[q['status']].lower()}.", 409)
    conn.execute(
        "UPDATE quotes SET status = 'cancelled', cancelled_by = %s, cancelled_at = now(), updated_at = now() "
        "WHERE id = %s",
        (actor, q["id"]),
    )
    audit.record(conn, actor, "quote.cancel", q["number"], q["customer_id"])
    return get(conn, q["id"])


# ---- the quote document --------------------------------------------------------------------

DOC_CSS = """body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#10213D;
margin:32px;max-width:900px}
h1{font-weight:650;letter-spacing:-.01em}table{width:100%;border-collapse:collapse}td,th{
border-bottom:1px solid #DFE6EE;
padding:8px;text-align:left;vertical-align:top}.n{text-align:right;font-family:"IBM Plex Mono",monospace}
.m{color:#52647A;font-size:12px}.ex{color:#9A5B00;font-weight:600}.eyebrow{color:#155EEF;font-size:12px;
text-transform:uppercase;letter-spacing:.08em;font-weight:600}"""


def document_html(q: dict) -> str:
    """A printable quote, in the invoice's style."""
    e = html.escape
    cur = q["currency"]
    rows = []
    for x in q["lines"]:
        disc = []
        if dec(x["term_discount_pct"]) > 0:
            disc.append(f"{rating._text(dec(x['term_discount_pct']))}% term")
        if dec(x["discount_pct"]) > 0:
            disc.append(f"{rating._text(dec(x['discount_pct']))}%")
        when = {"recurring": "Monthly", "one_off": "Once", "usage": "By use (estimate)"}[x["charge_type"]]
        rows.append(
            f"<tr><td>{e(x['description'])}<div class=m>{e(x['price_text'])}</div></td><td>{when}</td>"
            f"<td class=n>{money_text(x['list_amount'])}</td><td class=n>{e(', '.join(disc) or '-')}</td>"
            f"<td class=n>{money_text(x['amount'])}</td></tr>"
        )
    sample = "<p class=ex>Example data: these are sample prices, not real ExaCarib prices.</p>" if q["example"] else ""
    accepted = (
        f"<p>Accepted by {e(q['accepted_name'])} on {q['accepted_at']:%d %B %Y}.</p>" if q.get("accepted_at") else ""
    )
    usage = f", about {e(cur)} {money_text(q['usage_estimate'])} a month" if dec(q["usage_estimate"]) > 0 else ""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><title>Quote {e(q["number"])}</title>
<style>{DOC_CSS}</style></head><body>
<div class=eyebrow>ExaCarib quote</div><h1>{e(q["number"])}{(": " + e(q["title"])) if q["title"] else ""}</h1>
{sample}<p>For {e(q["customer"])}. {q["term_months"]}-month term. Valid until {q["valid_until"]:%d %B %Y}.
Status: {e(q["state_label"])}.</p>
<table><thead><tr><th>Item</th><th>Charged</th><th class=n>List ({e(cur)})</th><th class=n>Discount</th>
<th class=n>Price ({e(cur)})</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<p>One-off: <b>{e(cur)} {money_text(q["one_off_total"])}</b>. Monthly: <b>{e(cur)} {money_text(q["monthly_total"])}</b>.
Contract value over {q["term_months"]} months: <b>{e(cur)} {money_text(q["contract_total"])}</b>
(usage is billed as used{usage}).
Tax is added on each invoice.</p>
{("<p>" + e(q["notes"]) + "</p>") if q["notes"] else ""}{accepted}</body></html>"""
