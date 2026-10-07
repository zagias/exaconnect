"""The single ExaCarib bill (ADR 0033).

Messaging, AI and workflow usage is priced on versioned rate cards, the same
way voice is (voice/billing.py): every usage record becomes one rated charge
priced on the card in force when it was used, and the charge keeps the card
version. Voice keeps its own rate card and charges (calls, AI minutes,
monthly fees); the bill picks those charges up too, so a business gets ONE
invoice per period with everything on it.

- A draft bill can be rebuilt any time; it takes every charge of the period
  not on an issued document.
- Issuing freezes it (a database trigger refuses any change). Corrections
  are credit notes: new issued documents with negative lines pointing at the
  lines they correct.
- A voice charge lands on exactly one document: the single bill, or an older
  voice-only invoice issued before the single bill existed.

Prices: Dudley has not given a price list. A business with no card gets the
example card (example=true, labelled "Example" wherever it is shown).
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import events

events.register("bill.issued", "bill.credit_note_issued", "bill.rate_card_changed")

EXAMPLE_PRICES = {
    "ai_reply": "0.0200",
    "ai_tokens": "0.000002",
    "copilot": "0.0100",
    "ai_voice_minute": "0.0600",
    "message_out:whatsapp": "0.0150",
    "message_out:sms": "0.0300",
    "message_out:email": "0.0010",
    "message_out:web": "0",
    "message_out:*": "0.0100",
    "workflow_run": "0.0020",
}
METER_LABELS = {
    "ai_reply": "AI replies",
    "ai_tokens": "AI model tokens",
    "copilot": "Copilot requests",
    "ai_voice_minute": "AI browser call minutes",
    "workflow_run": "Workflow runs",
}
# Rated on this module's card. Voice minutes are rated by voice billing on its own card.
VOICE_METERS = ("voice_minute",)


class BillError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def d(v: Any) -> Decimal:
    if isinstance(v, float):
        raise TypeError("money must not be a float")
    return v if isinstance(v, Decimal) else Decimal(str(v if v not in (None, "") else "0"))


def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def s(x: Decimal) -> str:
    return format(x, "f")


def family(meter: str) -> str:
    if meter.startswith("message_out:"):
        return "messaging"
    if meter in VOICE_METERS:
        return "voice"
    if meter == "workflow_run":
        return "automation"
    if meter.startswith(("ai", "copilot")):
        return "ai"
    return "other"


def label(meter: str) -> str:
    if meter.startswith("message_out:"):
        ch = meter.split(":", 1)[1]
        return {"whatsapp": "WhatsApp", "sms": "SMS", "email": "Email", "web": "Website chat"}.get(
            ch, ch.capitalize()
        ) + " messages sent"
    return METER_LABELS.get(meter, meter)


def month_start(at: dt.date | dt.datetime) -> dt.date:
    return dt.date(at.year, at.month, 1)


def next_month(x: dt.date) -> dt.date:
    return dt.date(x.year + (x.month == 12), x.month % 12 + 1, 1)


# ---- rate cards ----------------------------------------------------------------------


def _validate(prices: dict) -> dict:
    out = {}
    for meter, v in (prices or {}).items():
        meter = str(meter)[:60]
        if family(meter) in ("voice", "other") and meter != "message_out:*":
            raise BillError(f"{meter} is not priced on this card (voice has its own).", 422)
        try:
            p = d(v)
        except (InvalidOperation, TypeError) as e:
            raise BillError(f"The price for {meter} is not a number.", 422) from e
        if p < 0:
            raise BillError("Prices can't be negative.", 422)
        out[meter] = s(p)
    if not out:
        raise BillError("Give at least one price.", 422)
    return out


def ensure_card(conn: psycopg.Connection, customer_id: Any) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('commai_rate_card:' || %s))", (str(customer_id),))
    if conn.execute("SELECT 1 FROM commai_rate_cards WHERE customer_id = %s", (customer_id,)).fetchone():
        return
    conn.execute(
        """INSERT INTO commai_rate_cards (customer_id, version, label, example, prices, effective_from, created_by)
           VALUES (%s, 1, 'Example rate card (not a real price list)', true, %s, '2000-01-01', 'system:example')""",
        (customer_id, Jsonb(EXAMPLE_PRICES)),
    )


def card_at(conn: psycopg.Connection, customer_id: Any, at: dt.datetime | None = None) -> dict:
    ensure_card(conn, customer_id)
    row = conn.execute(
        """SELECT * FROM commai_rate_cards WHERE customer_id = %s AND effective_from <= coalesce(%s, now())
           ORDER BY version DESC LIMIT 1""",
        (customer_id, at),
    ).fetchone()
    return (
        row
        or conn.execute(
            "SELECT * FROM commai_rate_cards WHERE customer_id = %s ORDER BY version LIMIT 1", (customer_id,)
        ).fetchone()
    )


def cards(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    ensure_card(conn, customer_id)
    return conn.execute(
        "SELECT * FROM commai_rate_cards WHERE customer_id = %s ORDER BY version DESC", (customer_id,)
    ).fetchall()


def new_card(
    conn, customer_id: Any, prices: dict, actor: str, *, label_: str = "", effective_from: dt.datetime | None = None
) -> dict:
    """A new version. Usage before effective_from keeps the price it was rated at."""
    ensure_card(conn, customer_id)
    clean = _validate(prices)
    v = conn.execute(
        "SELECT max(version) + 1 AS v FROM commai_rate_cards WHERE customer_id = %s", (customer_id,)
    ).fetchone()["v"]
    row = conn.execute(
        """INSERT INTO commai_rate_cards (customer_id, version, label, example, prices, effective_from, created_by)
           VALUES (%s, %s, %s, false, %s, coalesce(%s, now()), %s) RETURNING *""",
        (customer_id, v, label_[:120], Jsonb(clean), effective_from, actor),
    ).fetchone()
    events.emit(conn, customer_id, "bill.rate_card_changed", {"version": v, "by": actor}, str(row["id"]))
    return row


def unit_price(card: dict, meter: str) -> Decimal | None:
    p = card["prices"]
    if meter in p:
        return d(p[meter])
    if meter.startswith("message_out:") and "message_out:*" in p:
        return d(p["message_out:*"])
    return None


def price_now(conn, customer_id: Any, meter: str) -> Decimal:
    """What one unit of `meter` costs today (0 when it isn't priced on this card)."""
    if family(meter) in ("voice", "other"):
        return Decimal(0)
    return unit_price(card_at(conn, customer_id), meter) or Decimal(0)


# ---- rating ----------------------------------------------------------------------------


def _rated_here(rec: dict) -> bool:
    if family(rec["meter"]) in ("voice", "other"):
        return False
    # AI minutes on phone calls are rated by voice billing; browser calls are rated here.
    if rec["meter"] == "ai_voice_minute" and (rec["detail"] or {}).get("speech") != "browser":
        return False
    return True


def rate_record(conn: psycopg.Connection, rec: dict) -> dict | None:
    """Turn one usage record into its charge (once). Returns the charge, or
    None when the record is not priced on this card."""
    if not _rated_here(rec):
        return None
    card = card_at(conn, rec["customer_id"], rec["at"])
    unit = unit_price(card, rec["meter"])
    if unit is None:
        return None
    detail = rec["detail"] or {}
    wf = detail.get("workflow_id")
    channel = rec["meter"].split(":", 1)[1] if rec["meter"].startswith("message_out:") else ""
    if channel and not wf and rec["ref"]:
        src = conn.execute(
            "SELECT workflow_id FROM commai_message_sources WHERE message_id::text = %s", (rec["ref"],)
        ).fetchone()
        wf = str(src["workflow_id"]) if src else None
    qty = d(rec["quantity"])
    return conn.execute(
        """INSERT INTO commai_charges (customer_id, usage_id, meter, family, channel, workflow_id, quantity, unit_price,
             amount, rate_card_id, rate_card_version, at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (usage_id) DO NOTHING RETURNING *""",
        (
            rec["customer_id"],
            rec["id"],
            rec["meter"],
            family(rec["meter"]),
            channel,
            wf,
            qty,
            unit,
            (qty * unit).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            card["id"],
            card["version"],
            rec["at"],
        ),
    ).fetchone()


def rate_pending(conn: psycopg.Connection, customer_id: Any, before: dt.datetime | None = None) -> int:
    """Rate any usage not rated yet (usage recorded before the single bill existed)."""
    n = 0
    for rec in conn.execute(
        """SELECT u.* FROM usage_records u LEFT JOIN commai_charges c ON c.usage_id = u.id
           WHERE u.customer_id = %s AND c.id IS NULL AND (%s::timestamptz IS NULL OR u.at < %s)
           ORDER BY u.id LIMIT 50000""",
        (customer_id, before, before),
    ).fetchall():
        if rate_record(conn, rec):
            n += 1
    return n


# ---- spend (for budgets and the customer view) ------------------------------------------


def spend(conn, customer_id: Any, scope: str, key: str = "", since: dt.datetime | None = None) -> Decimal:
    """Money used this month for a budget scope: total, channel (key), ai, workflow (key)."""
    since = since or dt.datetime.combine(month_start(dt.datetime.now(dt.UTC)), dt.time(), dt.UTC)
    where, args = {
        "total": ("", []),
        "channel": ("AND channel = %s", [key]),
        "ai": ("AND family = 'ai'", []),
        "workflow": ("AND workflow_id::text = %s", [key]),
    }.get(scope, ("AND false", []))
    total = Decimal(0)
    if not (scope == "channel" and key == "voice"):
        total += d(
            conn.execute(
                f"SELECT coalesce(sum(amount), 0) AS a FROM commai_charges WHERE customer_id = %s AND at >= %s {where}",
                (customer_id, since, *args),
            ).fetchone()["a"]
        )
    voice_where = {"total": "", "ai": "AND kind = 'ai_minutes'"}.get(scope)
    if scope == "channel" and key == "voice":
        voice_where = ""
    if voice_where is not None:
        total += d(
            conn.execute(
                "SELECT coalesce(sum(amount), 0) AS a FROM voice_charges WHERE customer_id = %s AND at >= %s "
                + voice_where,
                (customer_id, since),
            ).fetchone()["a"]
        )
    return total


def usage_summary(conn, customer_id: Any, period: dt.date) -> dict:
    """Charges for a month by family, channel and workflow, priced on the cards."""
    period = month_start(period)
    end = next_month(period)
    rate_pending(conn, customer_id, dt.datetime.combine(end, dt.time(), dt.UTC))
    rows = conn.execute(
        """SELECT family, meter, channel, rate_card_version, unit_price, sum(quantity) AS quantity,
                  sum(amount) AS amount, count(*) AS charges
           FROM commai_charges WHERE customer_id = %s AND at >= %s AND at < %s
           GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 4""",
        (customer_id, period, end),
    ).fetchall()
    voice = conn.execute(
        """SELECT kind, sum(amount) AS amount, count(*) AS charges FROM voice_charges
           WHERE customer_id = %s AND at >= %s AND at < %s GROUP BY 1 ORDER BY 1""",
        (customer_id, period, end),
    ).fetchall()
    card = card_at(conn, customer_id)
    total = sum((d(r["amount"]) for r in rows), Decimal(0)) + sum((d(v["amount"]) for v in voice), Decimal(0))
    return {
        "period": period.isoformat(),
        "currency": card["currency"],
        "example_prices": bool(card["example"]),
        "lines": [
            {
                **r,
                "label": label(r["meter"]),
                "quantity": s(d(r["quantity"]).normalize()),
                "unit_price": s(d(r["unit_price"])),
                "amount": s(q2(d(r["amount"]))),
            }
            for r in rows
        ],
        "voice": [{"kind": v["kind"], "charges": v["charges"], "amount": s(q2(d(v["amount"])))} for v in voice],
        "total": s(q2(total)),
    }


# ---- bills ---------------------------------------------------------------------------


def _out(conn, bill: dict) -> dict:
    lines = conn.execute(
        """SELECT l.id, l.family, l.meter, l.voice_charge_id, l.rate_card_version, l.credits_line, l.description,
                  l.quantity, l.unit_price, l.amount FROM commai_bill_lines l WHERE l.bill_id = %s ORDER BY l.id""",
        (bill["id"],),
    ).fetchall()
    for line in lines:
        line["quantity"] = s(d(line["quantity"]).normalize())
        line["amount"] = s(q2(d(line["amount"])))
        if line["unit_price"] is not None:
            line["unit_price"] = s(d(line["unit_price"]).normalize())
    by_family: dict[str, Decimal] = {}
    for line in lines:
        by_family[line["family"]] = by_family.get(line["family"], Decimal(0)) + d(line["amount"])
    return {
        **bill,
        "total": s(d(bill["total"])),
        "lines": lines,
        "by_family": {k: s(q2(v)) for k, v in sorted(by_family.items())},
    }


def draft(conn: psycopg.Connection, customer_id: Any, period: dt.date, actor: str) -> dict:
    """(Re)build the draft bill for a month from every charge not yet on an issued document."""
    from .voice import billing as voice_billing

    period = month_start(period)
    end = next_month(period)
    end_at = dt.datetime.combine(end, dt.time(), dt.UTC)
    rate_pending(conn, customer_id, end_at)
    voice_billing.monthly_charges(conn, customer_id, period)
    card = card_at(conn, customer_id)
    bill = conn.execute(
        """SELECT * FROM commai_bills WHERE customer_id = %s AND period_start = %s AND status = 'draft'
           AND kind = 'invoice' FOR UPDATE""",
        (customer_id, period),
    ).fetchone()
    if bill is None:
        bill = conn.execute(
            """INSERT INTO commai_bills (customer_id, period_start, period_end, currency, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (customer_id, period, end - dt.timedelta(days=1), card["currency"], actor),
        ).fetchone()
    conn.execute("DELETE FROM commai_bill_lines WHERE bill_id = %s", (bill["id"],))
    # Messaging, AI and automation: one line per meter and price, tracing to its charges.
    for r in conn.execute(
        """SELECT family, meter, rate_card_version, unit_price, sum(quantity) AS quantity, sum(amount) AS amount,
                  count(*) AS n FROM commai_charges
           WHERE customer_id = %s AND bill_id IS NULL AND at >= %s AND at < %s
           GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3""",
        (customer_id, period, end),
    ).fetchall():
        conn.execute(
            """INSERT INTO commai_bill_lines (bill_id, customer_id, family, meter, rate_card_version, description,
                 quantity, unit_price, amount) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                bill["id"],
                customer_id,
                r["family"],
                r["meter"],
                r["rate_card_version"],
                f"{label(r['meter'])} (rate card v{r['rate_card_version']})",
                r["quantity"],
                r["unit_price"],
                r["amount"],
            ),
        )
    # Voice: one line per charge (each call and fee), as on voice invoices.
    conn.execute(
        """INSERT INTO commai_bill_lines (bill_id, customer_id, family, meter, voice_charge_id, rate_card_version,
             description, quantity, unit_price, amount)
           SELECT %s, customer_id, 'voice', kind, id, rate_card_version, description, quantity, unit_price, amount
           FROM voice_charges WHERE customer_id = %s AND bill_id IS NULL AND invoice_id IS NULL
             AND at >= %s AND at < %s ORDER BY at, ref""",
        (bill["id"], customer_id, period, end),
    )
    total = conn.execute(
        "SELECT coalesce(sum(amount), 0) AS t FROM commai_bill_lines WHERE bill_id = %s", (bill["id"],)
    ).fetchone()["t"]
    bill = conn.execute(
        "UPDATE commai_bills SET total = %s WHERE id = %s RETURNING *", (q2(d(total)), bill["id"])
    ).fetchone()
    return _out(conn, bill)


def _number(conn, customer_id: Any, kind: str) -> str:
    prefix = "EB" if kind == "invoice" else "EC"
    year = dt.datetime.now(dt.UTC).year
    n = conn.execute(
        """SELECT count(*) AS n FROM commai_bills WHERE customer_id = %s AND kind = %s AND status = 'issued'
           AND extract(year FROM issued_at) = %s""",
        (customer_id, kind, year),
    ).fetchone()["n"]
    return f"{prefix}-{year}-{n + 1:04d}"


def get(conn, customer_id: Any, bill_id: Any) -> dict:
    bill = conn.execute(
        "SELECT * FROM commai_bills WHERE id::text = %s AND customer_id = %s", (str(bill_id), customer_id)
    ).fetchone()
    if bill is None:
        raise BillError("Bill not found.", 404)
    return _out(conn, bill)


def issue(conn: psycopg.Connection, customer_id: Any, bill_id: Any, actor: str) -> dict:
    """Issue a draft: from now on it never changes; corrections are credit notes."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('commai_bill_number:' || %s))", (str(customer_id),))
    bill = conn.execute(
        "SELECT * FROM commai_bills WHERE id::text = %s AND customer_id = %s FOR UPDATE", (str(bill_id), customer_id)
    ).fetchone()
    if bill is None:
        raise BillError("Bill not found.", 404)
    if bill["status"] == "issued":
        raise BillError("This bill is already issued. Corrections go on a credit note.", 409)
    taken = conn.execute(
        """SELECT 1 FROM commai_bill_lines l JOIN voice_charges c ON c.id = l.voice_charge_id
           WHERE l.bill_id = %s AND (c.bill_id IS NOT NULL OR c.invoice_id IS NOT NULL) LIMIT 1""",
        (bill["id"],),
    ).fetchone()
    if taken:
        raise BillError("Some voice charges are already on another document. Rebuild the draft first.", 409)
    period, end = bill["period_start"], next_month(bill["period_start"])
    conn.execute(
        """UPDATE commai_charges SET bill_id = %s WHERE customer_id = %s AND bill_id IS NULL AND at >= %s AND at < %s
           AND (meter, rate_card_version, unit_price) IN
             (SELECT meter, rate_card_version, unit_price FROM commai_bill_lines
              WHERE bill_id = %s AND family <> 'voice')""",
        (bill["id"], customer_id, period, end, bill["id"]),
    )
    conn.execute(
        """UPDATE voice_charges SET bill_id = %s WHERE id IN
           (SELECT voice_charge_id FROM commai_bill_lines WHERE bill_id = %s AND voice_charge_id IS NOT NULL)""",
        (bill["id"], bill["id"]),
    )
    bill = conn.execute(
        """UPDATE commai_bills SET status = 'issued', number = %s, issued_by = %s, issued_at = now()
           WHERE id = %s RETURNING *""",
        (_number(conn, customer_id, "invoice"), actor, bill["id"]),
    ).fetchone()
    events.emit(
        conn, customer_id, "bill.issued", {"number": bill["number"], "total": s(d(bill["total"]))}, str(bill["id"])
    )
    return _out(conn, bill)


def credit_note(conn, customer_id: Any, bill_id: Any, lines: list[dict], reason: str, actor: str) -> dict:
    """Correct an issued bill with a credit note. The bill itself never changes."""
    bill = conn.execute(
        "SELECT * FROM commai_bills WHERE id::text = %s AND customer_id = %s", (str(bill_id), customer_id)
    ).fetchone()
    if bill is None:
        raise BillError("Bill not found.", 404)
    if bill["kind"] != "invoice" or bill["status"] != "issued":
        raise BillError("Only an issued bill can be credited. Change a draft by rebuilding it.", 409)
    if not lines:
        raise BillError("Choose at least one line to credit.", 422)
    if not reason.strip():
        raise BillError("Give a reason for the credit.", 422)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('commai_bill_number:' || %s))", (str(customer_id),))
    note = conn.execute(
        """INSERT INTO commai_bills (customer_id, kind, period_start, period_end, currency, credits_id, reason,
             created_by) VALUES (%s, 'credit_note', %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            bill["period_start"],
            bill["period_end"],
            bill["currency"],
            bill["id"],
            reason.strip()[:300],
            actor,
        ),
    ).fetchone()
    total = Decimal(0)
    for want in lines:
        line = conn.execute(
            "SELECT * FROM commai_bill_lines WHERE id = %s AND bill_id = %s", (want.get("line_id"), bill["id"])
        ).fetchone()
        if line is None:
            raise BillError(f"Line {want.get('line_id')} is not on this bill.", 422)
        credited = d(
            conn.execute(
                "SELECT coalesce(sum(-amount), 0) AS c FROM commai_bill_lines WHERE credits_line = %s", (line["id"],)
            ).fetchone()["c"]
        )
        left = d(line["amount"]) - credited
        try:
            amount = d(want["amount"]) if want.get("amount") not in (None, "") else left
        except (InvalidOperation, TypeError) as e:
            raise BillError("The amount is not a number.", 422) from e
        if amount <= 0 or amount > left:
            raise BillError(f"Line {line['id']} can be credited up to {s(q2(left))}.", 422)
        conn.execute(
            """INSERT INTO commai_bill_lines (bill_id, customer_id, family, meter, voice_charge_id, rate_card_version,
                 credits_line, description, quantity, unit_price, amount)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                note["id"],
                customer_id,
                line["family"],
                line["meter"],
                line["voice_charge_id"],
                line["rate_card_version"],
                line["id"],
                f"Credit: {line['description']}",
                line["quantity"],
                line["unit_price"],
                -amount,
            ),
        )
        total -= amount
    conn.execute("UPDATE commai_bills SET total = %s WHERE id = %s", (q2(total), note["id"]))
    note = conn.execute(
        """UPDATE commai_bills SET status = 'issued', number = %s, issued_by = %s, issued_at = now()
           WHERE id = %s RETURNING *""",
        (_number(conn, customer_id, "credit_note"), actor, note["id"]),
    ).fetchone()
    events.emit(
        conn,
        customer_id,
        "bill.credit_note_issued",
        {"number": note["number"], "bill": bill["number"], "total": s(d(note["total"]))},
        str(note["id"]),
    )
    return _out(conn, note)


def bills(conn, customer_id: Any, *, after: str | None = None, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT b.*, o.number AS credits_number FROM commai_bills b LEFT JOIN commai_bills o ON o.id = b.credits_id
           WHERE b.customer_id = %s AND (%s::timestamptz IS NULL OR b.created_at < %s::timestamptz)
           ORDER BY b.created_at DESC LIMIT %s""",
        (customer_id, after, after, limit + 1),
    ).fetchall()
    return [{**r, "total": s(d(r["total"]))} for r in rows]


def charge_trace(conn, customer_id: Any, line_id: int, limit: int = 500) -> list[dict]:
    """The charges (and usage records) behind one bill line."""
    line = conn.execute(
        "SELECT l.*, b.status FROM commai_bill_lines l JOIN commai_bills b ON b.id = l.bill_id"
        " WHERE l.id = %s AND l.customer_id = %s",
        (line_id, customer_id),
    ).fetchone()
    if line is None:
        raise BillError("Line not found.", 404)
    if line["family"] == "voice":
        return conn.execute(
            "SELECT id, kind, description, quantity, amount, at, cdr_id FROM voice_charges WHERE id = %s",
            (line["voice_charge_id"],),
        ).fetchall()
    bill = conn.execute("SELECT period_start FROM commai_bills WHERE id = %s", (line["bill_id"],)).fetchone()
    where = "c.bill_id = %s" if line["status"] == "issued" else "c.bill_id IS NULL"
    first = line["bill_id"] if line["status"] == "issued" else None
    args = [first] if first else []
    return conn.execute(
        f"""SELECT c.id, c.usage_id, c.meter, c.quantity, c.unit_price, c.amount, c.at, c.workflow_id, u.ref
            FROM commai_charges c JOIN usage_records u ON u.id = c.usage_id
            WHERE {where} AND c.customer_id = %s AND c.meter = %s AND c.rate_card_version = %s
              AND c.at >= %s AND c.at < %s ORDER BY c.at LIMIT %s""",
        (
            *args,
            customer_id,
            line["meter"],
            line["rate_card_version"],
            bill["period_start"],
            next_month(bill["period_start"]),
            limit,
        ),
    ).fetchall()
