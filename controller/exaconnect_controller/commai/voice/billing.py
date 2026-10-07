"""Voice billing (ADR 0021): rate cards, rating, bundles, fraud limits,
invoices and credit notes, and the supplier side.

Money is Decimal end to end (numeric in Postgres). Charges keep 4 decimal
places; invoice totals are rounded to the cent.

Dudley has not given a price list yet. A business with no rate card gets the
example card below, marked example=true and labelled "Example" wherever it
is shown. ExaCarib replaces it with a new version when prices are agreed.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import events, usage
from . import provider as providers
from .common import VoiceError, digits, money, month_start, next_month, now, q2, q4, s

events.register(
    "voice.call_rated",
    "voice.call_blocked",
    "voice.fraud_alert",
    "voice.bundle_alert",
    "voice.bundle_used_up",
    "voice.invoice_issued",
    "voice.credit_note_issued",
    "voice.rate_card_changed",
)

EXAMPLE_CARD = {
    "label": "Example rate card (not a real price list)",
    "currency": "USD",
    "monthly_user": "12.00",
    "monthly_number": "3.00",
    "ai_minute": "0.12",
    "one_time": {"desk_phone": "25.00", "new_number": "0.00", "port_number": "10.00", "softphone": "0.00"},
    "destinations": [
        {"prefix": "1868", "name": "Trinidad and Tobago", "per_minute": "0.0150"},
        {"prefix": "1876", "name": "Jamaica", "per_minute": "0.0400"},
        {"prefix": "1246", "name": "Barbados", "per_minute": "0.0400"},
        {"prefix": "1758", "name": "Saint Lucia", "per_minute": "0.0450"},
        {"prefix": "1", "name": "North America", "per_minute": "0.0200"},
        {"prefix": "44", "name": "United Kingdom", "per_minute": "0.0300"},
        {"prefix": "", "name": "Rest of world", "per_minute": "0.2500"},
    ],
}

EMERGENCY = {"911", "999", "990", "112", "211"}
ONE_TIME_KEYS = ("desk_phone", "softphone", "new_number", "port_number")

# ---- rate cards ----------------------------------------------------------------------


def _card_out(row: dict) -> dict:
    out = dict(row)
    for k in ("monthly_user", "monthly_number", "ai_minute"):
        out[k] = s(row[k])
    return out


def _validate_card(values: dict) -> dict:
    try:
        card = {
            "label": str(values.get("label", ""))[:120],
            "currency": str(values.get("currency", "USD"))[:3].upper(),
            "monthly_user": money(values["monthly_user"]),
            "monthly_number": money(values["monthly_number"]),
            "ai_minute": money(values["ai_minute"]),
            "one_time": {k: s(money(v)) for k, v in (values.get("one_time") or {}).items() if k in ONE_TIME_KEYS},
            "destinations": [],
        }
        seen = set()
        for d in values.get("destinations") or []:
            prefix = digits(d.get("prefix", ""))
            if prefix in seen:
                raise VoiceError(f"The prefix {prefix or '(default)'} is listed twice.", 422)
            seen.add(prefix)
            rate = money(d["per_minute"])
            if rate < 0:
                raise VoiceError("Rates can't be negative.", 422)
            card["destinations"].append({"prefix": prefix, "name": str(d.get("name", ""))[:80], "per_minute": s(rate)})
    except (KeyError, InvalidOperation, TypeError) as e:
        raise VoiceError(f"The rate card is missing or has a bad value: {e}", 422) from e
    for k in ("monthly_user", "monthly_number", "ai_minute"):
        if card[k] < 0:
            raise VoiceError("Rates can't be negative.", 422)
    if "" not in seen:
        raise VoiceError("Add a default rate (empty prefix) for destinations not listed.", 422)
    return card


def _insert_card(conn, customer_id, card: dict, actor: str, example: bool, effective_from=None) -> dict:
    version = conn.execute(
        "SELECT coalesce(max(version), 0) + 1 AS v FROM voice_rate_cards WHERE customer_id = %s", (customer_id,)
    ).fetchone()["v"]
    row = conn.execute(
        """INSERT INTO voice_rate_cards (customer_id, version, label, example, currency, effective_from,
             monthly_user, monthly_number, ai_minute, one_time, destinations, created_by)
           VALUES (%s, %s, %s, %s, %s, coalesce(%s, now()), %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            version,
            card["label"],
            example,
            card["currency"],
            effective_from,
            card["monthly_user"],
            card["monthly_number"],
            card["ai_minute"],
            Jsonb(card["one_time"]),
            Jsonb(card["destinations"]),
            actor,
        ),
    ).fetchone()
    return row


def ensure_card(conn: psycopg.Connection, customer_id: Any) -> None:
    """Give a business the example card (version 1) if it has none."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('voice_rate_card:' || %s))", (str(customer_id),))
    if conn.execute("SELECT 1 FROM voice_rate_cards WHERE customer_id = %s", (customer_id,)).fetchone():
        return
    _insert_card(
        conn, customer_id, _validate_card(EXAMPLE_CARD), "system:example", True, dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
    )


def card_at(conn: psycopg.Connection, customer_id: Any, at: dt.datetime | None = None) -> dict:
    """The rate card version in force at `at` (now by default)."""
    ensure_card(conn, customer_id)
    row = conn.execute(
        """SELECT * FROM voice_rate_cards WHERE customer_id = %s AND effective_from <= coalesce(%s, now())
           ORDER BY version DESC LIMIT 1""",
        (customer_id, at),
    ).fetchone()
    if row is None:  # every version starts later than `at`: use the first
        row = conn.execute(
            "SELECT * FROM voice_rate_cards WHERE customer_id = %s ORDER BY version LIMIT 1", (customer_id,)
        ).fetchone()
    return row


def new_card(conn, customer_id, values: dict, actor: str, effective_from=None) -> dict:
    ensure_card(conn, customer_id)
    row = _insert_card(conn, customer_id, _validate_card(values), actor, bool(values.get("example")), effective_from)
    events.emit(conn, customer_id, "voice.rate_card_changed", {"version": row["version"]}, str(row["id"]))
    return _card_out(row)


def cards(conn, customer_id) -> list[dict]:
    ensure_card(conn, customer_id)
    rows = conn.execute(
        "SELECT * FROM voice_rate_cards WHERE customer_id = %s ORDER BY version DESC", (customer_id,)
    ).fetchall()
    return [_card_out(r) for r in rows]


def destination_rate(card: dict, number: str) -> dict:
    """Longest matching prefix. -> {"prefix", "name", "per_minute": Decimal}"""
    d = digits(number)
    best = None
    for line in card["destinations"]:
        if d.startswith(line["prefix"]) and (best is None or len(line["prefix"]) > len(best["prefix"])):
            best = line
    if best is None:
        raise VoiceError("The rate card has no default rate.", 500)
    return {**best, "per_minute": money(best["per_minute"])}


def one_time_fee(card: dict, key: str) -> Decimal:
    return money((card["one_time"] or {}).get(key, "0"))


def price_impact(card: dict, counts: dict) -> dict:
    """What a change does to the bill. counts: users, numbers (signed: + added,
    - removed), desk_phones, softphones, new_numbers, ported_numbers (one-time)."""
    lines = []
    monthly = Decimal(0)
    for key, label, unit in (
        ("users", "user", money(card["monthly_user"])),
        ("numbers", "number", money(card["monthly_number"])),
    ):
        n = int(counts.get(key, 0))
        if n:
            amount = unit * n
            monthly += amount
            lines.append(
                {
                    "label": f"{'+' if n > 0 else ''}{n} {label}{'s' if abs(n) != 1 else ''} a month",
                    "amount": s(q2(amount)),
                    "recurring": True,
                }
            )
    one_time = Decimal(0)
    for key, label, fee in (
        ("desk_phones", "desk phone set-up", "desk_phone"),
        ("softphones", "softphone set-up", "softphone"),
        ("new_numbers", "new number", "new_number"),
        ("ported_numbers", "number port", "port_number"),
    ):
        n = int(counts.get(key, 0))
        amount = one_time_fee(card, fee) * n
        if n and amount:
            one_time += amount
            lines.append({"label": f"{n} × {label}", "amount": s(q2(amount)), "recurring": False})
    return {
        "currency": card["currency"],
        "monthly_delta": s(q2(monthly)),
        "one_time": s(q2(one_time)),
        "changes_bill": monthly != 0 or one_time != 0,
        "lines": lines,
        "rate_card_version": card["version"],
        "example_prices": bool(card["example"]),
    }


# ---- fraud limits --------------------------------------------------------------------


def fraud_limits(conn, customer_id) -> dict:
    conn.execute("INSERT INTO voice_fraud_limits (customer_id) VALUES (%s) ON CONFLICT DO NOTHING", (customer_id,))
    row = conn.execute("SELECT * FROM voice_fraud_limits WHERE customer_id = %s", (customer_id,)).fetchone()
    return {**row, "daily_cap": s(row["daily_cap"]) if row["daily_cap"] is not None else None}


def set_fraud_limits(conn, customer_id, values: dict, actor: str) -> dict:
    cur = fraud_limits(conn, customer_id)
    cap = values.get("daily_cap", cur["daily_cap"])
    cap = money(cap) if cap not in (None, "") else None
    if cap is not None and cap < 0:
        raise VoiceError("The daily cap can't be negative.", 422)
    prefixes = values.get("blocked_prefixes", cur["blocked_prefixes"])
    prefixes = sorted({digits(p) for p in prefixes if digits(p)})
    conn.execute(
        """UPDATE voice_fraud_limits SET daily_cap = %s, blocked_prefixes = %s, international = %s,
             calls_per_hour_alert = %s, updated_by = %s, updated_at = now() WHERE customer_id = %s""",
        (
            cap,
            prefixes,
            bool(values.get("international", cur["international"])),
            int(values.get("calls_per_hour_alert", cur["calls_per_hour_alert"])),
            actor,
            customer_id,
        ),
    )
    return fraud_limits(conn, customer_id)


def spend_today(conn, customer_id) -> Decimal:
    row = conn.execute(
        """SELECT coalesce(sum(amount), 0) AS a FROM voice_charges
           WHERE customer_id = %s AND kind IN ('call', 'ai_minutes') AND at >= date_trunc('day', now())""",
        (customer_id,),
    ).fetchone()
    return money(row["a"])


def authorise(
    conn: psycopg.Connection,
    customer_id: Any,
    to_number: str,
    *,
    voice_user_id: Any = None,
    from_number: str | None = None,
) -> dict:
    """May this outbound call go ahead? -> {"allowed", "reason", "route"}.
    Emergency numbers (every island's, ADR 0027) are never stopped. Unusual
    patterns raise an alert; revenue share fraud rules (fraud.py) can stop a
    call or suspend international calling. "route" is the carriers to try, in
    order (None: the single provider)."""
    from . import carriers, emergency, fraud

    d = digits(to_number)
    if d in EMERGENCY or emergency.is_emergency(conn, customer_id, d, voice_user_id):
        return {"allowed": True, "reason": "Emergency call.", "emergency": True, "route": None}
    lim = fraud_limits(conn, customer_id)
    for p in lim["blocked_prefixes"]:
        if d.startswith(p):
            return {"allowed": False, "reason": f"Calls to numbers starting {p} are blocked (premium or high-risk)."}
    if not lim["international"] and not d.startswith("1"):
        return {"allowed": False, "reason": "International calls are switched off for your company."}
    if lim["daily_cap"] is not None and spend_today(conn, customer_id) >= money(lim["daily_cap"]):
        return {"allowed": False, "reason": f"Today's call spend has reached the daily cap of {lim['daily_cap']}."}
    recent = conn.execute(
        """SELECT count(*) AS n FROM voice_cdrs WHERE customer_id = %s AND direction = 'outbound'
           AND started_at > now() - interval '1 hour'""",
        (customer_id,),
    ).fetchone()["n"]
    if recent + 1 > lim["calls_per_hour_alert"]:
        already = conn.execute(
            """SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'voice.fraud_alert'
               AND at > now() - interval '1 hour'""",
            (customer_id,),
        ).fetchone()
        if not already:
            events.emit(
                conn,
                customer_id,
                "voice.fraud_alert",
                {"reason": "unusual_volume", "calls_last_hour": recent + 1, "threshold": lim["calls_per_hour_alert"]},
            )
    stop = fraud.check(conn, customer_id, d, voice_user_id=voice_user_id, from_number=from_number)
    if stop:
        return {**stop, "route": None}
    route = carriers.route_keys(conn, customer_id, d)
    if route == []:
        return {"allowed": False, "reason": "No carrier can take calls to this destination right now.", "route": []}
    return {"allowed": True, "reason": "", "route": route}


# ---- rating --------------------------------------------------------------------------


def _bundle_used(conn, bundle_id, period: dt.date) -> Decimal:
    row = conn.execute(
        """SELECT coalesce(sum(bundle_minutes), 0) AS m FROM voice_charges
           WHERE bundle_id = %s AND at >= %s AND at < %s""",
        (bundle_id, period, next_month(period)),
    ).fetchone()
    return money(row["m"])


def _bundle_alerts(conn, customer_id, bundle: dict, period: dt.date) -> None:
    used = _bundle_used(conn, bundle["id"], period)
    total = money(bundle["minutes"])
    if total <= 0:
        return
    pct = used * 100 / total
    for level, kind in ((bundle["alert_pct"], "voice.bundle_alert"), (100, "voice.bundle_used_up")):
        if pct >= level:
            row = conn.execute(
                """INSERT INTO voice_bundle_alerts (bundle_id, period, level) VALUES (%s, %s, %s)
                   ON CONFLICT DO NOTHING RETURNING level""",
                (bundle["id"], period, level),
            ).fetchone()
            if row:
                events.emit(
                    conn,
                    customer_id,
                    kind,
                    {"bundle": bundle["name"], "used_minutes": s(q2(used)), "minutes": s(total), "level": level},
                    str(bundle["id"]),
                )


def _bundle_for(conn, customer_id, number: str) -> list[dict]:
    d = digits(number)
    rows = conn.execute(
        "SELECT * FROM voice_bundles WHERE customer_id = %s AND active ORDER BY name", (customer_id,)
    ).fetchall()
    return [b for b in rows if not b["prefixes"] or any(d.startswith(p) for p in b["prefixes"])]


def record_call(conn: psycopg.Connection, customer_id: Any, call: dict) -> dict:
    """Store a call record when the call ends and rate it. Safe to repeat:
    the same call_id is stored and rated once."""
    started = call["started_at"]
    ended = call["ended_at"]
    seconds = int(call.get("seconds") if call.get("seconds") is not None else max(0, (ended - started).total_seconds()))
    vu = None
    if call.get("voice_user_id"):
        vu = conn.execute(
            "SELECT id, site_id, team_id FROM voice_users WHERE id = %s AND customer_id = %s",
            (call["voice_user_id"], customer_id),
        ).fetchone()
    from . import fraud

    intl = call.get("direction", "outbound") == "outbound" and fraud.is_international(
        call.get("to_number", ""), fraud.origin(conn, customer_id)["country"]
    )
    row = conn.execute(
        """INSERT INTO voice_cdrs (customer_id, call_id, direction, from_number, to_number, voice_user_id, site_id,
             team_id, started_at, ended_at, seconds, ai_seconds, status, block_reason, recording_ref, provider_ref,
             international)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, call_id) DO NOTHING RETURNING *""",
        (
            customer_id,
            call["call_id"],
            call.get("direction", "outbound"),
            call.get("from_number", ""),
            call.get("to_number", ""),
            vu["id"] if vu else None,
            vu["site_id"] if vu else None,
            vu["team_id"] if vu else None,
            started,
            ended,
            seconds,
            int(call.get("ai_seconds", 0)),
            call.get("status", "completed"),
            call.get("block_reason", ""),
            call.get("recording_ref", ""),
            call.get("provider_ref", ""),
            intl,
        ),
    ).fetchone()
    if row is None:
        return conn.execute(
            "SELECT * FROM voice_cdrs WHERE customer_id = %s AND call_id = %s", (customer_id, call["call_id"])
        ).fetchone()
    if row["status"] == "blocked":
        events.emit(
            conn,
            customer_id,
            "voice.call_blocked",
            {"to": row["to_number"], "reason": row["block_reason"]},
            row["call_id"],
        )
    rate_call(conn, customer_id, row)
    return row


def rate_call(conn: psycopg.Connection, customer_id: Any, cdr: dict) -> list[dict]:
    """Price one ended call with the rate card in force when it ended."""
    if cdr["status"] != "completed":
        return []
    card = card_at(conn, customer_id, cdr["ended_at"])
    out = []
    period = month_start(cdr["ended_at"])
    common = (cdr["site_id"], cdr["team_id"], cdr["voice_user_id"], cdr["ended_at"])
    if cdr["direction"] == "outbound" and cdr["seconds"] > 0 and digits(cdr["to_number"]) not in EMERGENCY:
        dest = destination_rate(card, cdr["to_number"])
        minutes = Decimal(cdr["seconds"]) / 60
        bundle_id, from_bundle = None, Decimal(0)
        for b in _bundle_for(conn, customer_id, cdr["to_number"]):
            left = money(b["minutes"]) - _bundle_used(conn, b["id"], period)
            if left > 0:
                bundle_id, from_bundle = b["id"], min(left, minutes)
                bundle = b
                break
        amount = q4((minutes - from_bundle) * dest["per_minute"])
        row = conn.execute(
            """INSERT INTO voice_charges (customer_id, ref, kind, description, cdr_id, rate_card_id,
                 rate_card_version, destination, quantity, bundle_id, bundle_minutes, unit_price, amount,
                 site_id, team_id, voice_user_id, at)
               VALUES (%s, %s, 'call', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, ref) DO NOTHING RETURNING *""",
            (
                customer_id,
                f"call:{cdr['call_id']}",
                f"Call to {cdr['to_number']} ({dest['name']})",
                cdr["id"],
                card["id"],
                card["version"],
                dest["name"],
                q4(minutes),
                bundle_id,
                q4(from_bundle),
                dest["per_minute"],
                amount,
                *common,
            ),
        ).fetchone()
        if row:
            out.append(row)
            usage.record(
                conn,
                customer_id,
                "voice_minute",
                q4(minutes),
                ref=cdr["call_id"],
                detail={"to": cdr["to_number"], "amount": s(amount)},
            )
            if bundle_id:
                _bundle_alerts(conn, customer_id, bundle, period)
    if cdr["ai_seconds"] > 0:
        minutes = Decimal(cdr["ai_seconds"]) / 60
        unit = money(card["ai_minute"])
        row = conn.execute(
            """INSERT INTO voice_charges (customer_id, ref, kind, description, cdr_id, rate_card_id,
                 rate_card_version, quantity, unit_price, amount, site_id, team_id, voice_user_id, at)
               VALUES (%s, %s, 'ai_minutes', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, ref) DO NOTHING RETURNING *""",
            (
                customer_id,
                f"ai:{cdr['call_id']}",
                "AI agent minutes",
                cdr["id"],
                card["id"],
                card["version"],
                q4(minutes),
                unit,
                q4(minutes * unit),
                *common,
            ),
        ).fetchone()
        if row:
            out.append(row)
            usage.record(conn, customer_id, "ai_voice_minute", q4(minutes), ref=cdr["call_id"])
    if out:
        events.emit(
            conn,
            customer_id,
            "voice.call_rated",
            {
                "call_id": cdr["call_id"],
                "amount": s(sum((r["amount"] for r in out), Decimal(0))),
                "rate_card_version": card["version"],
            },
            cdr["call_id"],
        )
    return out


def one_time_charge(conn, customer_id, ref: str, key: str, description: str, *, voice_user_id=None, site_id=None):
    """A one-time fee (desk phone, port...), once per ref, at the current card's price."""
    card = card_at(conn, customer_id)
    amount = one_time_fee(card, key)
    return conn.execute(
        """INSERT INTO voice_charges (customer_id, ref, kind, description, rate_card_id, rate_card_version,
             quantity, unit_price, amount, voice_user_id, site_id)
           VALUES (%s, %s, 'one_time', %s, %s, %s, 1, %s, %s, %s, %s)
           ON CONFLICT (customer_id, ref) DO NOTHING RETURNING *""",
        (customer_id, ref, description, card["id"], card["version"], amount, amount, voice_user_id, site_id),
    ).fetchone()


# ---- monthly fees and invoices -------------------------------------------------------


def _overlap_days(start: dt.date, end: dt.date, frm: dt.datetime | None, until: dt.datetime | None) -> int:
    if frm is None:
        return 0
    a = max(start, frm.date())
    b = min(end, until.date() if until else end)
    return max(0, (b - a).days)


def monthly_charges(conn, customer_id, period: dt.date) -> None:
    """Monthly fees for every user and number that was billable during the
    month, pro rata by day: charges start at activation and stop at removal."""
    end = next_month(period)
    card = card_at(conn, customer_id, min(now(), dt.datetime.combine(end, dt.time(), dt.UTC) - dt.timedelta(seconds=1)))
    days_in = (end - period).days
    for table, kind, unit, label in (
        ("voice_users", "monthly_user", money(card["monthly_user"]), "User"),
        ("voice_numbers", "monthly_number", money(card["monthly_number"]), "Number"),
    ):
        name_col = "name || ' (ext ' || extension || ')'" if table == "voice_users" else "e164"
        site_col = "site_id"
        rows = conn.execute(
            f"""SELECT id, {name_col} AS label, {site_col} AS site_id, billing_from, billing_until
                FROM {table} WHERE customer_id = %s AND billing_from IS NOT NULL AND billing_from < %s
                AND (billing_until IS NULL OR billing_until > %s)""",
            (customer_id, end, period),
        ).fetchall()
        for r in rows:
            days = _overlap_days(period, end, r["billing_from"], r["billing_until"])
            if days <= 0:
                continue
            qty = q4(Decimal(days) / days_in)
            amount = q4(unit * Decimal(days) / days_in)
            conn.execute(
                """INSERT INTO voice_charges (customer_id, ref, kind, description, rate_card_id, rate_card_version,
                     quantity, unit_price, amount, site_id, voice_user_id, at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (customer_id, ref) DO UPDATE SET quantity = EXCLUDED.quantity,
                     amount = EXCLUDED.amount, unit_price = EXCLUDED.unit_price, description = EXCLUDED.description,
                     rate_card_id = EXCLUDED.rate_card_id, rate_card_version = EXCLUDED.rate_card_version
                   WHERE voice_charges.invoice_id IS NULL""",
                (
                    customer_id,
                    f"{kind}:{r['id']}:{period.isoformat()}",
                    kind,
                    f"{label} {r['label']}, {days} of {days_in} days",
                    card["id"],
                    card["version"],
                    qty,
                    unit,
                    amount,
                    r["site_id"],
                    r["id"] if table == "voice_users" else None,
                    dt.datetime.combine(period, dt.time(), dt.UTC),
                ),
            )


def _invoice_out(conn, inv: dict) -> dict:
    lines = conn.execute(
        """SELECT l.id, l.description, l.quantity, l.amount, l.charge_id, l.credits_line, c.kind, c.ref,
                  c.rate_card_version, c.unit_price, c.bundle_minutes, d.call_id, d.to_number, d.started_at, d.seconds
           FROM voice_invoice_lines l LEFT JOIN voice_charges c ON c.id = l.charge_id
           LEFT JOIN voice_cdrs d ON d.id = c.cdr_id WHERE l.invoice_id = %s ORDER BY l.id""",
        (inv["id"],),
    ).fetchall()
    for line in lines:
        for k in ("quantity", "amount", "unit_price", "bundle_minutes"):
            if line[k] is not None:
                line[k] = s(line[k])
    return {**inv, "total": s(inv["total"]), "lines": lines}


def draft_invoice(conn, customer_id, period: dt.date, actor: str) -> dict:
    """(Re)build the draft invoice for a month from charges not yet invoiced."""
    period = month_start(period)
    end = next_month(period)
    monthly_charges(conn, customer_id, period)
    card = card_at(conn, customer_id)
    inv = conn.execute(
        """SELECT * FROM voice_invoices WHERE customer_id = %s AND period_start = %s AND status = 'draft'
           AND kind = 'invoice' FOR UPDATE""",
        (customer_id, period),
    ).fetchone()
    if inv is None:
        inv = conn.execute(
            """INSERT INTO voice_invoices (customer_id, period_start, period_end, currency, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (customer_id, period, end - dt.timedelta(days=1), card["currency"], actor),
        ).fetchone()
    conn.execute("DELETE FROM voice_invoice_lines WHERE invoice_id = %s", (inv["id"],))
    conn.execute(
        """INSERT INTO voice_invoice_lines (invoice_id, customer_id, charge_id, description, quantity, amount)
           SELECT %s, customer_id, id, description, quantity, amount FROM voice_charges
           WHERE customer_id = %s AND invoice_id IS NULL AND at >= %s AND at < %s ORDER BY at, ref""",
        (inv["id"], customer_id, period, end),
    )
    total = conn.execute(
        "SELECT coalesce(sum(amount), 0) AS t FROM voice_invoice_lines WHERE invoice_id = %s", (inv["id"],)
    ).fetchone()["t"]
    inv = conn.execute(
        "UPDATE voice_invoices SET total = %s WHERE id = %s RETURNING *", (q2(money(total)), inv["id"])
    ).fetchone()
    return _invoice_out(conn, inv)


def _number(conn, customer_id, kind: str) -> str:
    prefix = "VI" if kind == "invoice" else "VC"
    year = now().year
    n = conn.execute(
        """SELECT count(*) AS n FROM voice_invoices WHERE customer_id = %s AND kind = %s AND status = 'issued'
           AND extract(year FROM issued_at) = %s""",
        (customer_id, kind, year),
    ).fetchone()["n"]
    return f"{prefix}-{year}-{n + 1:04d}"


def get_invoice(conn, customer_id, invoice_id) -> dict:
    inv = conn.execute(
        "SELECT * FROM voice_invoices WHERE id = %s AND customer_id = %s", (invoice_id, customer_id)
    ).fetchone()
    if inv is None:
        raise VoiceError("Invoice not found.", 404)
    return _invoice_out(conn, inv)


def issue_invoice(conn, customer_id, invoice_id, actor: str) -> dict:
    """Issue a draft. From here on it is frozen (a database trigger refuses
    any change); corrections become credit notes."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('voice_invoice_number:' || %s))", (str(customer_id),))
    inv = conn.execute(
        "SELECT * FROM voice_invoices WHERE id = %s AND customer_id = %s FOR UPDATE", (invoice_id, customer_id)
    ).fetchone()
    if inv is None:
        raise VoiceError("Invoice not found.", 404)
    if inv["status"] == "issued":
        raise VoiceError("This invoice is already issued. Corrections go on a credit note.", 409)
    taken = conn.execute(
        """SELECT 1 FROM voice_invoice_lines l JOIN voice_charges c ON c.id = l.charge_id
           WHERE l.invoice_id = %s AND c.invoice_id IS NOT NULL LIMIT 1""",
        (invoice_id,),
    ).fetchone()
    if taken:
        raise VoiceError("Some of these charges are on another invoice. Rebuild the draft first.", 409)
    conn.execute(
        """UPDATE voice_charges SET invoice_id = %s WHERE id IN
           (SELECT charge_id FROM voice_invoice_lines WHERE invoice_id = %s AND charge_id IS NOT NULL)""",
        (invoice_id, invoice_id),
    )
    inv = conn.execute(
        """UPDATE voice_invoices SET status = 'issued', number = %s, issued_by = %s, issued_at = now()
           WHERE id = %s RETURNING *""",
        (_number(conn, customer_id, "invoice"), actor, invoice_id),
    ).fetchone()
    events.emit(
        conn, customer_id, "voice.invoice_issued", {"number": inv["number"], "total": s(inv["total"])}, str(inv["id"])
    )
    return _invoice_out(conn, inv)


def credit_note(conn, customer_id, invoice_id, lines: list[dict], reason: str, actor: str) -> dict:
    """Correct an issued invoice: a new, issued credit note with negative lines
    that point at the lines they correct. The invoice itself never changes."""
    inv = conn.execute(
        "SELECT * FROM voice_invoices WHERE id = %s AND customer_id = %s", (invoice_id, customer_id)
    ).fetchone()
    if inv is None:
        raise VoiceError("Invoice not found.", 404)
    if inv["kind"] != "invoice" or inv["status"] != "issued":
        raise VoiceError("Only an issued invoice can be credited. Change a draft by rebuilding it.", 409)
    if not lines:
        raise VoiceError("Choose at least one line to credit.", 422)
    if not reason.strip():
        raise VoiceError("Give a reason for the credit.", 422)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('voice_invoice_number:' || %s))", (str(customer_id),))
    note = conn.execute(
        """INSERT INTO voice_invoices (customer_id, kind, period_start, period_end, currency, credits_id, reason,
             created_by) VALUES (%s, 'credit_note', %s, %s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, inv["period_start"], inv["period_end"], inv["currency"], invoice_id, reason.strip()[:300], actor),
    ).fetchone()
    total = Decimal(0)
    for want in lines:
        line = conn.execute(
            "SELECT * FROM voice_invoice_lines WHERE id = %s AND invoice_id = %s", (want["line_id"], invoice_id)
        ).fetchone()
        if line is None:
            raise VoiceError(f"Line {want['line_id']} is not on this invoice.", 422)
        credited = money(
            conn.execute(
                "SELECT coalesce(sum(-amount), 0) AS c FROM voice_invoice_lines WHERE credits_line = %s", (line["id"],)
            ).fetchone()["c"]
        )
        left = money(line["amount"]) - credited
        amount = money(want["amount"]) if want.get("amount") not in (None, "") else left
        if amount <= 0 or amount > left:
            raise VoiceError(f"Line {line['id']} can be credited up to {s(q2(left))}.", 422)
        conn.execute(
            """INSERT INTO voice_invoice_lines (invoice_id, customer_id, charge_id, credits_line, description,
                 quantity, amount) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                note["id"],
                customer_id,
                line["charge_id"],
                line["id"],
                f"Credit: {line['description']}",
                line["quantity"],
                -amount,
            ),
        )
        total -= amount
    conn.execute("UPDATE voice_invoices SET total = %s WHERE id = %s", (q2(total), note["id"]))
    note = conn.execute(
        """UPDATE voice_invoices SET status = 'issued', number = %s, issued_by = %s, issued_at = now()
           WHERE id = %s RETURNING *""",
        (_number(conn, customer_id, "credit_note"), actor, note["id"]),
    ).fetchone()
    events.emit(
        conn,
        customer_id,
        "voice.credit_note_issued",
        {"number": note["number"], "invoice": inv["number"], "total": s(note["total"])},
        str(note["id"]),
    )
    return _invoice_out(conn, note)


def invoices(conn, customer_id) -> list[dict]:
    rows = conn.execute(
        """SELECT i.*, o.number AS credits_number FROM voice_invoices i
           LEFT JOIN voice_invoices o ON o.id = i.credits_id
           WHERE i.customer_id = %s ORDER BY i.period_start DESC, i.created_at DESC""",
        (customer_id,),
    ).fetchall()
    return [{**r, "total": s(r["total"])} for r in rows]


# ---- customer view -------------------------------------------------------------------


def spend(conn, customer_id, period: dt.date) -> dict:
    """Usage and spend for a month, by site, team and user, with bundles."""
    period = month_start(period)
    end = next_month(period)
    args = (customer_id, period, end)

    def grouped(col: str, join: str, name: str) -> list[dict]:
        rows = conn.execute(
            f"""SELECT c.{col} AS id, {name} AS name, sum(c.amount) AS amount,
                   sum(CASE WHEN c.kind = 'call' THEN c.quantity ELSE 0 END) AS minutes, count(*) AS charges
                FROM voice_charges c {join}
                WHERE c.customer_id = %s AND c.at >= %s AND c.at < %s GROUP BY 1, 2 ORDER BY 3 DESC""",
            args,
        ).fetchall()
        return [{**r, "amount": s(q2(money(r["amount"]))), "minutes": s(q2(money(r["minutes"])))} for r in rows]

    total = conn.execute(
        """SELECT coalesce(sum(amount), 0) AS a, coalesce(sum(CASE WHEN kind = 'call' THEN quantity END), 0) AS m
           FROM voice_charges WHERE customer_id = %s AND at >= %s AND at < %s""",
        args,
    ).fetchone()
    by_kind = conn.execute(
        """SELECT kind, sum(amount) AS amount FROM voice_charges WHERE customer_id = %s AND at >= %s AND at < %s
           GROUP BY kind ORDER BY kind""",
        args,
    ).fetchall()
    bundle_rows = conn.execute(
        "SELECT * FROM voice_bundles WHERE customer_id = %s ORDER BY name", (customer_id,)
    ).fetchall()
    card = card_at(conn, customer_id)
    return {
        "period": period.isoformat(),
        "currency": card["currency"],
        "example_prices": bool(card["example"]),
        "total": s(q2(money(total["a"]))),
        "minutes": s(q2(money(total["m"]))),
        "by_kind": [{"kind": r["kind"], "amount": s(q2(money(r["amount"])))} for r in by_kind],
        "by_site": grouped("site_id", "LEFT JOIN voice_sites x ON x.id = c.site_id", "coalesce(x.name, 'No site')"),
        "by_team": grouped("team_id", "LEFT JOIN commai_teams x ON x.id = c.team_id", "coalesce(x.name, 'No team')"),
        "by_user": grouped(
            "voice_user_id", "LEFT JOIN voice_users x ON x.id = c.voice_user_id", "coalesce(x.name, 'Company-wide')"
        ),
        "bundles": [
            {
                "id": b["id"],
                "name": b["name"],
                "minutes": s(b["minutes"]),
                "prefixes": b["prefixes"],
                "alert_pct": b["alert_pct"],
                "active": b["active"],
                "used": s(q2(_bundle_used(conn, b["id"], period))),
            }
            for b in bundle_rows
        ],
        "spend_today": s(q2(spend_today(conn, customer_id))),
    }


def save_bundle(conn, customer_id, values: dict) -> dict:
    minutes = money(values["minutes"])
    if minutes <= 0:
        raise VoiceError("A bundle needs some minutes.", 422)
    pct = int(values.get("alert_pct", 80))
    if not 1 <= pct <= 99:
        raise VoiceError("Alert between 1% and 99% used.", 422)
    row = conn.execute(
        """INSERT INTO voice_bundles (customer_id, name, minutes, prefixes, alert_pct, active)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, name) DO UPDATE SET minutes = EXCLUDED.minutes, prefixes = EXCLUDED.prefixes,
             alert_pct = EXCLUDED.alert_pct, active = EXCLUDED.active RETURNING *""",
        (
            customer_id,
            values["name"].strip()[:80],
            minutes,
            sorted({digits(p) for p in values.get("prefixes") or [] if digits(p)}),
            pct,
            bool(values.get("active", True)),
        ),
    ).fetchone()
    return {**row, "minutes": s(row["minutes"])}


# ---- supplier side -------------------------------------------------------------------

SUPPLIER_COLUMNS = ("call_ref", "started_at", "destination", "seconds", "cost")


def import_supplier_csv(conn, supplier: str, text: str, filename: str, actor: str) -> dict:
    """Import the SIP provider's call charges. Every row is checked; bad rows
    are reported and skipped. A row already imported is never counted twice."""
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in SUPPLIER_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise VoiceError(
            f"The CSV needs the columns {', '.join(SUPPLIER_COLUMNS)} (missing {', '.join(missing)}).", 422
        )
    imp = conn.execute(
        "INSERT INTO voice_supplier_imports (supplier, filename, created_by) VALUES (%s, %s, %s) RETURNING id",
        (supplier, filename[:200], actor),
    ).fetchone()
    rejected, added, duplicates = [], 0, 0
    for n, row in enumerate(reader, start=2):
        try:
            ref = (row["call_ref"] or "").strip()
            if not ref:
                raise ValueError("call_ref is empty")
            cost = money((row["cost"] or "").strip())
            seconds = int((row["seconds"] or "0").strip())
            started = dt.datetime.fromisoformat(row["started_at"].strip()) if row["started_at"] else None
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=dt.UTC)
        except (ValueError, InvalidOperation, AttributeError) as e:
            rejected.append({"row": n, "error": str(e) or "bad value"})
            continue
        cdr = conn.execute(
            "SELECT id, customer_id FROM voice_cdrs WHERE provider_ref = %s OR call_id = %s LIMIT 1", (ref, ref)
        ).fetchone()
        r = conn.execute(
            """INSERT INTO voice_supplier_charges (import_id, supplier, call_ref, started_at, destination, seconds,
                 cost, customer_id, cdr_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (supplier, call_ref) DO NOTHING RETURNING id""",
            (
                imp["id"],
                supplier,
                ref,
                started,
                digits(row["destination"] or ""),
                seconds,
                cost,
                cdr["customer_id"] if cdr else None,
                cdr["id"] if cdr else None,
            ),
        ).fetchone()
        if r:
            added += 1
        else:
            duplicates += 1
    conn.execute(
        "UPDATE voice_supplier_imports SET rows = %s, rejected = %s WHERE id = %s",
        (added, Jsonb(rejected), imp["id"]),
    )
    return {"id": imp["id"], "added": added, "duplicates": duplicates, "rejected": rejected}


def reconcile(conn, customer_id, period: dt.date) -> dict:
    """What ExaCarib billed this business against what the supplier charged
    for the same calls, with the margin, and the calls that don't line up."""
    period = month_start(period)
    end = next_month(period)
    calls = conn.execute(
        """SELECT d.call_id, d.to_number, d.seconds, d.ended_at, d.carrier, d.carrier_cost,
                  coalesce((SELECT sum(amount) FROM voice_charges c WHERE c.cdr_id = d.id), 0) AS billed,
                  sc.cost, sc.seconds AS supplier_seconds
           FROM voice_cdrs d LEFT JOIN voice_supplier_charges sc ON sc.cdr_id = d.id
           WHERE d.customer_id = %s AND d.direction = 'outbound' AND d.status = 'completed'
             AND d.ended_at >= %s AND d.ended_at < %s ORDER BY d.ended_at""",
        (customer_id, period, end),
    ).fetchall()
    billed = sum((money(c["billed"]) for c in calls), Decimal(0))
    cost = sum((money(c["cost"]) for c in calls if c["cost"] is not None), Decimal(0))
    issues = []
    for c in calls:
        if c["cost"] is None:
            issues.append({"call_id": c["call_id"], "issue": "No supplier charge yet"})
        elif c["supplier_seconds"] != c["seconds"]:
            issues.append(
                {
                    "call_id": c["call_id"],
                    "issue": f"Duration differs: we have {c['seconds']} s, supplier {c['supplier_seconds']} s",
                }
            )
        elif money(c["cost"]) > money(c["billed"]):
            issues.append({"call_id": c["call_id"], "issue": "Supplier cost is more than we billed"})
    fees = money(
        conn.execute(
            """SELECT coalesce(sum(amount), 0) AS a FROM voice_charges WHERE customer_id = %s AND kind <> 'call'
               AND at >= %s AND at < %s""",
            (customer_id, period, end),
        ).fetchone()["a"]
    )
    unmatched = conn.execute(
        """SELECT count(*) AS n, coalesce(sum(cost), 0) AS cost FROM voice_supplier_charges
           WHERE cdr_id IS NULL AND (started_at IS NULL OR (started_at >= %s AND started_at < %s))""",
        (period, end),
    ).fetchone()
    estimate = Decimal(0)
    prov = providers.get()
    if not prov.live:
        # A call carried by a named carrier is costed from that carrier's rate sheet (ADR 0027).
        estimate = sum(
            (
                money(c["carrier_cost"])
                if c["carrier_cost"] is not None
                else q4(Decimal(c["seconds"]) / 60 * prov.supplier_rate(c["to_number"]))
                for c in calls
            ),
            Decimal(0),
        )
    margin = billed - cost
    return {
        "period": period.isoformat(),
        "calls": len(calls),
        "billed_calls": s(q2(billed)),
        "supplier_cost": s(q2(cost)),
        "call_margin": s(q2(margin)),
        "call_margin_pct": s(q2(margin * 100 / billed)) if billed else None,
        "other_fees": s(q2(fees)),
        "simulated_cost_estimate": s(q2(estimate)) if not prov.live else None,
        "issues": issues[:200],
        "unmatched_supplier_rows": unmatched["n"],
        "unmatched_supplier_cost": s(q2(money(unmatched["cost"]))),
    }
