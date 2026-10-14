"""What an organisation has bought (ADR 0055): contract items, one-off charges and
rated usage, and the invoice lines they make on the plan invoice of their product.

Recurring items are charged for the days of the month they cover. One-off
charges land on the invoice for the month they were made. Usage events are
rated on the price agreed in the quote (kept on the contract item), so a later
catalogue price change never alters what was sold.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit
from ..billing.core import BillingError, at_midnight, days_active, dec, next_month
from . import rating
from .quotes import units

MAX_EVENTS = 1000
METER_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def items(conn: psycopg.Connection, customer_id: Any, include_ended: bool = True) -> list[dict]:
    rows = conn.execute(
        """SELECT ci.*, s.name AS site, o.number AS order_number, o.id AS order_id
           FROM contract_items ci
           LEFT JOIN sites s ON s.id = ci.site_id
           LEFT JOIN sales_order_items oi ON oi.id = ci.order_item_id
           LEFT JOIN sales_orders o ON o.id = oi.order_id
           WHERE ci.customer_id = %s AND (%s OR ci.status = 'active')
           ORDER BY ci.status, ci.product, ci.starts_on, ci.code""",
        (customer_id, include_ended),
    ).fetchall()
    out = []
    for r in rows:
        term_end = _add_months(r["starts_on"], r["term_months"])
        out.append(
            {
                **r,
                "term_ends_on": term_end,
                "price_text": rating.describe(r["price"], r["unit"], r["currency"]) if r["meter"] else "",
            }
        )
    return out


def _add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    first = dt.date(d.year + y, m + 1, 1)
    last_day = (next_month(first) - dt.timedelta(days=1)).day
    return first.replace(day=min(d.day, last_day))


def summary(conn: psycopg.Connection, customer_id: Any, start: dt.date | None = None) -> dict:
    """The contract, one-off charges and this month's rated usage, for the Billing page."""
    start = start or _today().replace(day=1)
    end = next_month(start)
    ones = conn.execute(
        """SELECT c.*, o.number AS order_number FROM one_off_charges c
           LEFT JOIN sales_order_items oi ON oi.id = c.order_item_id LEFT JOIN sales_orders o ON o.id = oi.order_id
           WHERE c.customer_id = %s ORDER BY c.charge_on DESC LIMIT 200""",
        (customer_id,),
    ).fetchall()
    contract = items(conn, customer_id)
    usage = []
    for ci in contract:
        if ci["charge_type"] != "usage" or ci["status"] != "active":
            continue
        qty = _usage(conn, customer_id, ci, start, end)
        rated = rating.rate(ci["price"], qty)
        usage.append(
            {
                "contract_item_id": ci["id"],
                "description": ci["description"],
                "meter": ci["meter"],
                "unit": ci["unit"],
                "quantity": qty,
                "included": rated["included"],
                "billable": rated["billable"],
                "amount_so_far": rating.discounted(rated["amount"], ci["discount_pct"]),
                "price_text": ci["price_text"],
            }
        )
    monthly = sum(
        (dec(c["monthly_amount"]) for c in contract if c["status"] == "active" and c["charge_type"] == "recurring"),
        Decimal(0),
    )
    return {
        "customer_id": customer_id,
        "period": f"{start:%Y-%m}",
        "items": contract,
        "one_off_charges": ones,
        "usage": usage,
        "monthly_total": rating.q4(monthly),
    }


def end_item(conn: psycopg.Connection, customer_id: Any, item_id: Any, on: Any, actor: str) -> dict:
    """Stop a contract item from a day (it is not charged from that day on)."""
    try:
        row = conn.execute(
            "SELECT * FROM contract_items WHERE id = %s AND customer_id = %s FOR UPDATE", (str(item_id), customer_id)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None:
        raise BillingError("Contract item not found.", 404)
    if row["status"] == "ended":
        raise BillingError("This item has already ended.", 409)
    try:
        day = dt.date.fromisoformat(str(on)) if on else _today()
    except ValueError as e:
        raise BillingError("Give the end date as YYYY-MM-DD.") from e
    if day <= row["starts_on"]:
        day = row["starts_on"] + dt.timedelta(days=1)
    conn.execute(
        """UPDATE contract_items SET ends_on = %s, status = 'ended', ended_by = %s, ended_at = now() WHERE id = %s""",
        (day, actor, row["id"]),
    )
    audit.record(conn, actor, "contract.item.end", row["description"], customer_id, {"ends_on": day.isoformat()})
    return {**row, "ends_on": day, "status": "ended"}


# ---- usage events ---------------------------------------------------------------------------


def record_usage(
    conn: psycopg.Connection, customer_id: Any, events: list[dict], actor: str, source: str = "api"
) -> dict:
    """Take usage events for an organisation's usage items. Each one needs the
    meter, a quantity, when it happened and the sender's own reference: a
    reference already seen is skipped, so sending again is safe."""
    if not isinstance(events, list) or not events:
        raise BillingError("Send at least one usage event.")
    if len(events) > MAX_EVENTS:
        raise BillingError(f"Send up to {MAX_EVENTS} events at a time.")
    meters = {
        r["meter"]
        for r in conn.execute(
            "SELECT DISTINCT meter FROM contract_items WHERE customer_id = %s AND charge_type = 'usage'",
            (customer_id,),
        )
    }
    now = dt.datetime.now(dt.UTC)
    rows = []
    for i, e in enumerate(events, 1):
        if not isinstance(e, dict):
            raise BillingError(f"Event {i}: needs meter, quantity, at and ref.")
        meter = str(e.get("meter") or "")
        if not METER_RE.match(meter):
            raise BillingError(f"Event {i}: the meter name isn't valid.")
        if meter not in meters:
            raise BillingError(f"Event {i}: this organisation has nothing billed on the meter {meter}.", 409)
        qty = dec(e.get("quantity"))
        if qty < 0 or qty > 10_000_000:
            raise BillingError(f"Event {i}: the quantity must be 0 or more.")
        try:
            at = dt.datetime.fromisoformat(str(e.get("at") or now.isoformat()).replace("Z", "+00:00"))
        except ValueError as err:
            raise BillingError(f"Event {i}: give 'at' as an ISO 8601 time.") from err
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.UTC)
        if at > now + dt.timedelta(minutes=5):
            raise BillingError(f"Event {i}: 'at' is in the future.")
        ref = str(e.get("ref") or "").strip()
        if not 1 <= len(ref) <= 120:
            raise BillingError(f"Event {i}: give each event your own reference (ref), so it counts once.")
        detail = e.get("detail") if isinstance(e.get("detail"), dict) else {}
        rows.append((customer_id, meter, qty, at, ref, source[:40], Jsonb(detail), actor))
    added = 0
    for r in rows:
        added += conn.execute(
            """INSERT INTO usage_events (customer_id, meter, quantity, at, ref, source, detail, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (customer_id, meter, ref) DO NOTHING""",
            r,
        ).rowcount
    if added:
        audit.record(conn, actor, "usage.record", f"{added} event(s)", customer_id, {"skipped": len(rows) - added})
    return {"received": len(rows), "added": added, "duplicates": len(rows) - added}


def usage_events(conn: psycopg.Connection, customer_id: Any, meter: str | None = None, limit: int = 200) -> list[dict]:
    return conn.execute(
        """SELECT id, meter, quantity, at, ref, source, detail, created_by, created_at FROM usage_events
           WHERE customer_id = %s AND (%s::text IS NULL OR meter = %s) ORDER BY at DESC LIMIT %s""",
        (customer_id, meter, meter, min(max(limit, 1), 1000)),
    ).fetchall()


def _usage(conn, customer_id, ci: dict, frm: dt.date, to: dt.date) -> Decimal:
    lo = max(frm, ci["starts_on"])
    hi = min(to, ci["ends_on"]) if ci["ends_on"] else to
    if hi <= lo:
        return Decimal(0)
    row = conn.execute(
        """SELECT coalesce(sum(quantity), 0) AS q FROM usage_events
           WHERE customer_id = %s AND meter = %s AND at >= %s AND at < %s""",
        (customer_id, ci["meter"], at_midnight(lo), at_midnight(hi)),
    ).fetchone()
    return dec(row["q"])


# ---- invoice lines ------------------------------------------------------------------------


def billing_lines(
    conn: psycopg.Connection, customer_id: Any, product: str, currency: str, start: dt.date, frm: dt.date, to: dt.date
) -> list[dict]:
    """Lines for a plan invoice: recurring items for the days they cover, one-off
    charges made in [frm, to), and usage rated on the agreed price. Items in
    another currency than the plan's price list are refused, never converted."""
    from ..billing.service import _line

    end = next_month(start)
    out: list[dict] = []
    rows = conn.execute(
        """SELECT ci.*, s.id AS site_alive, l.id AS link_alive FROM contract_items ci
           LEFT JOIN sites s ON s.id = ci.site_id LEFT JOIN links l ON l.id = ci.link_id
           WHERE ci.customer_id = %s AND ci.product = %s AND ci.starts_on < %s
             AND (ci.ends_on IS NULL OR ci.ends_on > %s)
           ORDER BY ci.charge_type, ci.starts_on, ci.created_at""",
        (customer_id, product, to, frm),
    ).fetchall()
    for ci in rows:
        # A credit for a site or link stops when that site or link is removed.
        if (ci["follows"] == "site" and ci["site_alive"] is None) or (
            ci["follows"] == "link" and ci["link_alive"] is None
        ):
            continue
        _same_currency(ci["currency"], currency, ci["description"])
        if ci["charge_type"] == "recurring":
            lo = max(frm, ci["starts_on"])
            hi = min(to, ci["ends_on"]) if ci["ends_on"] else to
            days, dim = days_active(None, start, end, lo, hi)
            if days <= 0:
                continue
            monthly = dec(ci["monthly_amount"])
            part = f" ({days} of {dim} days)" if days < dim else ""
            out.append(
                _line(
                    "credit" if monthly < 0 else "recurring",
                    f"{ci['description']}{part}",
                    ci["quantity"],
                    ci["unit"],
                    monthly / ci["quantity"] if ci["quantity"] else monthly,
                    monthly * days / dim,
                    {
                        "contract_item_id": ci["id"],
                        "monthly_amount": monthly,
                        "days_active": days,
                        "days_in_month": dim,
                        "starts_on": ci["starts_on"],
                        "term_months": ci["term_months"],
                    },
                    site_id=ci["site_id"],
                    link_id=ci["link_id"],
                )
            )
        else:
            qty = _usage(conn, customer_id, ci, frm, to)
            rated = rating.rate(ci["price"], qty)
            if rated["amount"] <= 0:
                continue
            amount = rating.discounted(rated["amount"], ci["discount_pct"])
            disc = f", {rating._text(dec(ci['discount_pct']))}% off" if dec(ci["discount_pct"]) > 0 else ""
            out.append(
                _line(
                    "rated",
                    f"{ci['description']}: {units(qty, ci['unit'])} ({rated['included']} included{disc})",
                    rated["billable"],
                    ci["unit"],
                    amount / dec(rated["billable"]),
                    amount,
                    {
                        "contract_item_id": ci["id"],
                        "meter": ci["meter"],
                        "used": qty,
                        "rating": _plain(rated),
                        "discount_pct": ci["discount_pct"],
                        "price": ci["price"],
                    },
                )
            )
    for c in conn.execute(
        """SELECT * FROM one_off_charges WHERE customer_id = %s AND product = %s AND charge_on >= %s AND charge_on < %s
           ORDER BY charge_on, created_at""",
        (customer_id, product, frm, to),
    ).fetchall():
        _same_currency(c["currency"], currency, c["description"])
        out.append(
            _line(
                "one_off",
                f"{c['description']} ({c['charge_on']:%d %b})",
                c["quantity"],
                c["unit"],
                dec(c["amount"]) / dec(c["quantity"]),
                c["amount"],
                {"one_off_charge_id": c["id"], "charge_on": c["charge_on"]},
            )
        )
    return out


def _plain(rated: dict) -> dict:
    return {k: (f"{v:f}" if isinstance(v, Decimal) else v) for k, v in rated.items()}


def _same_currency(a: str, b: str, what: str) -> None:
    if a != b:
        raise BillingError(f"{what} is in {a} but this plan bills in {b}. Fix the contract before invoicing.", 409)
