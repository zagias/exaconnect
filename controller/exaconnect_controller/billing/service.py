"""Billing (ADR 0022): price lists per plan, each subscription's charges for a
calendar month (UTC), SLA credits, and draft, issued and void invoices; the
supplier side (carrier and partner payables) and margin.

Charges are worked out on demand from inventory, metering and CommAI's own
usage and voice rating; nothing is stored until a draft invoice is generated.
Every line names its plan and keeps its inputs (quantities, prices, the
samples behind a 95th percentile, the SLA windows behind a credit) so a
person can see why it is there.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import re
import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit, fabric, sla
from ..metering import rollup
from ..metering.core import gigabytes, money, settle
from . import plans as plans_mod
from .core import (
    MONEY_FIELDS,
    BillingError,
    at_midnight,
    credit_for,
    days_active,
    dec,
    half_up,
    invoice_number,
    next_month,
    pct_text,
    period_label,
    totals,
    validate_credit_table,
)

SATELLITE = ("leo", "geo")
# CommAI meters rated by CommAI Voice itself (commai/voice/billing.py); their
# money comes onto the plan's invoice from voice_charges, never re-rated here.
VOICE_METERS = ("voice_minute", "ai_voice_minute")
METER_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}(:[a-z0-9_*]{1,40})?$")
VOICE_UNITS = {
    "call": "minute",
    "ai_minutes": "minute",
    "monthly_user": "user-month",
    "monthly_number": "number-month",
    "one_time": "item",
}
VOICE_LABELS = {
    "call": "Voice calls",
    "ai_minutes": "AI voice minutes",
    "monthly_user": "Voice users",
    "monthly_number": "Phone numbers",
    "one_time": "One-time voice items",
}


def jsonable(v: Any) -> Any:
    """Decimals as strings (never floats), dates as ISO 8601."""
    if isinstance(v, Decimal):
        return f"{v:f}"
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [jsonable(x) for x in v]
    return v


def _today(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(dt.UTC)


def _month_start(now: dt.datetime) -> dt.date:
    return now.astimezone(dt.UTC).date().replace(day=1)


def _is_uuid(v: Any) -> bool:
    try:
        uuid.UUID(str(v))
    except ValueError:
        return False
    return True


def _num(v: Any) -> str:
    """100 -> '100', 12.5 -> '12.5' for descriptions."""
    return f"{dec(v).normalize():f}"


# ---- price lists -----------------------------------------------------------------------


def price_list_for(conn: psycopg.Connection, plan_id: Any, customer_id: Any, at: dt.date) -> dict:
    """The plan's list in effect on a day: the organisation's own latest version on
    that plan that has started, otherwise the plan's own."""
    row = conn.execute(
        """SELECT * FROM price_lists WHERE plan_id = %(p)s AND (customer_id = %(c)s OR customer_id IS NULL)
             AND effective_from <= %(at)s
           ORDER BY (customer_id IS NULL), effective_from DESC, version DESC LIMIT 1""",
        {"p": plan_id, "c": customer_id, "at": at},
    ).fetchone()
    if row is None:
        raise BillingError("There is no price list in effect on this plan for this month.", 409)
    return row


def price_lists(conn: psycopg.Connection, plan_id: Any | None = None, customer_id: Any | None = None) -> list[dict]:
    """Every version, newest first: plans' own lists (customer_id None) or one
    organisation's own lists; optionally for one plan."""
    return conn.execute(
        """SELECT pl.*, p.name AS plan, p.product FROM price_lists pl JOIN plans p ON p.id = pl.plan_id
           WHERE (%(p)s::uuid IS NULL OR pl.plan_id = %(p)s)
             AND (CASE WHEN %(c)s::uuid IS NULL THEN pl.customer_id IS NULL ELSE pl.customer_id = %(c)s END)
           ORDER BY p.product, p.name, pl.version DESC""",
        {"p": plan_id, "c": customer_id},
    ).fetchall()


def _meter_prices(raw: Any) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise BillingError("Meter prices are a list of meter names and prices.")
    if len(raw) > 40:
        raise BillingError("Keep meter prices to 40 meters or fewer.")
    out = {}
    for k, v in raw.items():
        k = str(k).strip()
        if not METER_RE.match(k):
            raise BillingError(f"{k!r} is not a meter name (for example ai_reply or message_out:whatsapp).")
        if k in VOICE_METERS:
            raise BillingError(f"{k} is priced by the voice rate card, not here.")
        d = dec(v)
        if d < 0:
            raise BillingError("Prices can't be negative.")
        out[k] = f"{d.normalize():f}"
    return out


def create_price_list(
    conn: psycopg.Connection, plan_id: Any, customer_id: Any | None, values: dict, actor: str
) -> dict:
    """Save a new version. Earlier versions stay as they were, so invoices that
    used them can always be explained."""
    plan = plans_mod.get_plan(conn, plan_id)
    currency = str(values.get("currency") or "USD").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise BillingError("Currency is a three-letter code, such as USD or TTD.")
    prices = {}
    for k in (*MONEY_FIELDS, "monthly_fee"):
        v = dec(values.get(k) or 0)
        if v < 0:
            raise BillingError("Prices can't be negative.")
        prices[k] = v
    circuit = values.get("circuit_per_mbps_month")
    circuit = None if circuit in (None, "") else dec(circuit)
    if circuit is not None and circuit < 0:
        raise BillingError("Prices can't be negative.")
    tax = dec(values.get("tax_rate_pct") or 0)
    cap = dec(values.get("credit_cap_pct") if values.get("credit_cap_pct") not in (None, "") else 50)
    if not (0 <= tax <= 100) or not (0 <= cap <= 100):
        raise BillingError("Tax and the credit cap are percentages between 0 and 100.")
    table = validate_credit_table(values.get("sla_credits") or [])
    meters = _meter_prices(values.get("meter_prices"))
    if plan["product"] == "commai":
        if any(prices[k] for k in MONEY_FIELDS) or circuit is not None or table:
            raise BillingError("A CommAI price list has a monthly fee and meter prices only.")
    elif meters:
        raise BillingError("Meter prices belong on a CommAI price list.")
    eff = values.get("effective_from")
    if isinstance(eff, str):
        try:
            eff = dt.date.fromisoformat(eff)
        except ValueError as e:
            raise BillingError("Give the effective date as YYYY-MM-DD.") from e
    if not isinstance(eff, dt.date):
        raise BillingError("Give the date the price list takes effect.")
    key = f"{plan['id']}:{customer_id or 'plan'}"
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('price_list:' || %s))", (key,))
    last = conn.execute(
        "SELECT max(version) AS v FROM price_lists WHERE plan_id = %s AND customer_id IS NOT DISTINCT FROM %s",
        (plan["id"], customer_id),
    ).fetchone()["v"]
    row = conn.execute(
        """INSERT INTO price_lists (plan_id, customer_id, version, effective_from, label, currency, monthly_fee,
             site_monthly, commit_per_mbps, burst_per_mbps, satellite_per_gb, circuit_per_mbps_month, meter_prices,
             tax_rate_pct, sla_credits, credit_cap_pct, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            plan["id"],
            customer_id,
            (last or 0) + 1,
            eff,
            str(values.get("label") or "")[:120],
            currency,
            prices["monthly_fee"],
            prices["site_monthly"],
            prices["commit_per_mbps"],
            prices["burst_per_mbps"],
            prices["satellite_per_gb"],
            circuit,
            Jsonb(meters),
            tax,
            Jsonb(table),
            cap,
            actor,
        ),
    ).fetchone()
    audit.record(
        conn,
        actor,
        "billing.price_list.create",
        f"{plan['name']} {'' if customer_id is None else '(organisation) '}v{row['version']}",
        customer_id,
        jsonable(
            {
                "plan_id": plan["id"],
                **{
                    k: row[k]
                    for k in ("version", "effective_from", "currency", "monthly_fee", *MONEY_FIELDS, "tax_rate_pct")
                },
                "meter_prices": meters,
            }
        ),
    )
    return {**row, "plan": plan["name"], "product": plan["product"]}


# ---- charges ---------------------------------------------------------------------------


def _line(kind: str, description: str, quantity, unit: str, unit_price, amount, inputs: dict, **refs) -> dict:
    return {
        "kind": kind,
        "description": description,
        "site_id": refs.get("site_id"),
        "link_id": refs.get("link_id"),
        "circuit_id": refs.get("circuit_id"),
        "class_name": refs.get("class_name"),
        "quantity": half_up(dec(quantity), "0.000001"),
        "unit": unit,
        "unit_price": half_up(dec(unit_price), "0.000001"),
        "amount": money(dec(amount)),
        "inputs": jsonable(inputs),
    }


def window(sub: dict, start: dt.date) -> tuple[dt.date, dt.date]:
    """The part of a month a subscription covers: [from, to)."""
    end = next_month(start)
    frm = max(start, sub["starts_on"])
    to = min(end, sub["ends_on"]) if sub["ends_on"] else end
    if to <= frm:
        raise BillingError(f"This plan does not cover any of {period_label(start)}.", 422)
    return frm, to


def compute_subscription(conn: psycopg.Connection, sub: dict, start: dt.date, now: dt.datetime | None = None) -> dict:
    """Every charge and credit for one subscription in one calendar month (UTC)."""
    now = _today(now)
    end = next_month(start)
    frm, to = window(sub, start)
    customer = conn.execute("SELECT id, name FROM customers WHERE id = %s", (sub["customer_id"],)).fetchone()
    if customer is None:
        raise BillingError("Customer not found.", 404)
    plan = plans_mod.get_plan(conn, sub["plan_id"])
    pl = price_list_for(conn, plan["id"], sub["customer_id"], frm)
    complete = now >= at_midnight(to)
    lines: list[dict] = []
    fee = dec(pl["monthly_fee"])
    if fee > 0:
        days, dim = days_active(None, start, end, frm, to)
        part = f" ({days} of {dim} days)" if days < dim else ""
        lines.append(
            _line(
                "plan",
                f"{plan['name']}: monthly plan fee{part}",
                1,
                "month",
                fee,
                fee * days / dim,
                {"days_active": days, "days_in_month": dim, "price_list_version": pl["version"]},
            )
        )
    if plan["product"] == "connect":
        lines += _connect_lines(conn, sub["customer_id"], pl, start, end, frm, to, complete)
    else:
        lines += _commai_lines(conn, sub["customer_id"], pl, frm, to)
    for i, x in enumerate(lines, 1):
        x["position"] = i
        x["product"] = plan["product"]
        x["plan"] = plan["name"]
    t = totals(lines, pl["tax_rate_pct"])
    return {
        "customer_id": customer["id"],
        "customer": customer["name"],
        "subscription_id": sub["id"],
        "product": plan["product"],
        "plan_id": plan["id"],
        "plan": plan["name"],
        "period": f"{start:%Y-%m}",
        "period_start": start,
        "period_end": end,
        "covered_from": frm,
        "covered_to": to,
        "label": period_label(start),
        "complete": complete,
        "price_list": pl,
        "currency": pl["currency"],
        "example": bool(pl["example"] or plan["example"]),
        "lines": lines,
        **t,
    }


def compute(conn: psycopg.Connection, customer_id: Any, start: dt.date, now: dt.datetime | None = None) -> dict:
    """The month's running charges for an organisation, one block per plan it held."""
    cust = conn.execute("SELECT id, name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    if cust is None:
        raise BillingError("Customer not found.", 404)
    end = next_month(start)
    blocks = [compute_subscription(conn, s, start, now) for s in plans_mod.overlapping(conn, customer_id, start, end)]
    by_currency: dict[str, Decimal] = {}
    for b in blocks:
        by_currency[b["currency"]] = by_currency.get(b["currency"], Decimal(0)) + b["total"]
    return {
        "customer_id": cust["id"],
        "customer": cust["name"],
        "period": f"{start:%Y-%m}",
        "label": period_label(start),
        "products": sorted({b["product"] for b in blocks}, key=list(plans_mod.PRODUCTS).index),
        "plans": blocks,
        "totals": [{"currency": c, "total": money(v)} for c, v in sorted(by_currency.items())],
        "example": any(b["example"] for b in blocks),
    }


def _connect_lines(
    conn, customer_id, pl: dict, start: dt.date, end: dt.date, frm: dt.date, to: dt.date, complete: bool
) -> list[dict]:
    s_dt, e_dt = at_midnight(frm), at_midnight(to)
    sites = conn.execute(
        """SELECT id, name, created_at FROM sites WHERE customer_id = %s AND kind = 'site' AND created_at < %s
           ORDER BY name""",
        (customer_id, e_dt),
    ).fetchall()
    links = conn.execute(
        """SELECT l.id, l.site_id, l.path, p.label AS path_label, c.name AS carrier, l.underlay_type, l.commit_mbps
           FROM links l JOIN paths p ON p.name = l.path JOIN carriers c ON c.id = l.carrier_id
           WHERE l.site_id = ANY(%s) ORDER BY p.ordinal""",
        ([s["id"] for s in sites],),
    ).fetchall()
    by_site: dict[Any, list[dict]] = {}
    for link in links:
        by_site.setdefault(link["site_id"], []).append(link)

    lines: list[dict] = []
    site_fee: dict[Any, Decimal] = {}
    fee = dec(pl["site_monthly"])
    for s in sites:
        days, dim = days_active(s["created_at"], start, end, frm, to)
        if days <= 0:
            continue
        part = f" ({days} of {dim} days)" if days < dim else ""
        amount = money(fee * days / dim)
        site_fee[s["id"]] = amount
        lines.append(
            _line(
                "site",
                f"Site fee: {s['name']}{part}",
                1,
                "site a month",
                fee,
                amount,
                {"site": s["name"], "days_active": days, "days_in_month": dim, "price_list_version": pl["version"]},
                site_id=s["id"],
            )
        )
        for link in by_site.get(s["id"], []):
            # The same 5-minute samples and settlement code as the metering screen and carrier CSV.
            samples = rollup.samples(conn, link["id"], s_dt, e_dt)
            commit = dec(link["commit_mbps"])
            st = settle(samples, commit, pl["commit_per_mbps"], pl["burst_per_mbps"])
            where = f"{link['path_label']} at {s['name']}"
            if commit > 0:
                lines.append(
                    _line(
                        "commit",
                        f"{where}: {_num(commit)} Mbps commit{part}",
                        commit,
                        "Mbps a month",
                        pl["commit_per_mbps"],
                        st.commit_charge if days == dim else commit * dec(pl["commit_per_mbps"]) * days / dim,
                        {
                            "site": s["name"],
                            "path": link["path"],
                            "carrier": link["carrier"],
                            "underlay_type": link["underlay_type"],
                            "commit_mbps": commit,
                            "days_active": days,
                            "days_in_month": dim,
                        },
                        site_id=s["id"],
                        link_id=link["id"],
                    )
                )
            if st.burst_mbps > 0:
                lines.append(
                    _line(
                        "burst",
                        f"{where}: burst, 95th percentile {_num(half_up(dec(st.billable_mbps), '0.001'))} Mbps"
                        f" is {_num(st.burst_mbps)} Mbps over the {_num(commit)} Mbps commit",
                        st.burst_mbps,
                        "Mbps over commit",
                        pl["burst_per_mbps"],
                        st.burst_charge,
                        {
                            "site": s["name"],
                            "path": link["path"],
                            "carrier": link["carrier"],
                            "source": "usage_5m",
                            "samples": st.samples,
                            "discarded": st.discarded,
                            "p95_in_mbps": st.p95_in,
                            "p95_out_mbps": st.p95_out,
                            "billable_mbps": st.billable_mbps,
                            "commit_mbps": commit,
                            "samples_csv": f"/api/v1/metering/settlement.csv?link_id={link['id']}"
                            f"&start={s_dt.isoformat().replace('+00:00', 'Z')}"
                            f"&end={e_dt.isoformat().replace('+00:00', 'Z')}",
                        },
                        site_id=s["id"],
                        link_id=link["id"],
                    )
                )
            if link["underlay_type"] in SATELLITE and dec(pl["satellite_per_gb"]) > 0:
                gb = half_up(dec(gigabytes(samples)), "0.001")
                if gb > 0:
                    lines.append(
                        _line(
                            "satellite",
                            f"{where}: {_num(gb)} GB of satellite data (Storm Mode and failover)",
                            gb,
                            "GB",
                            pl["satellite_per_gb"],
                            gb * dec(pl["satellite_per_gb"]),
                            {
                                "site": s["name"],
                                "path": link["path"],
                                "carrier": link["carrier"],
                                "source": "usage_5m",
                                "samples": len(samples),
                            },
                            site_id=s["id"],
                            link_id=link["id"],
                        )
                    )

    lines += _circuit_lines(conn, customer_id, pl, s_dt, e_dt)
    lines += _credit_lines(conn, customer_id, sites, site_fee, pl, s_dt, e_dt, complete)
    return lines


def _circuit_lines(conn, customer_id, pl: dict, s_dt: dt.datetime, e_dt: dt.datetime) -> list[dict]:
    """Virtual circuits and elastic bandwidth, metered hourly: each speed for the
    hours it was set (fabric.charges), at the circuit's price or the price list's."""
    out = []
    circuits = conn.execute(
        """SELECT * FROM circuits WHERE customer_id = %s AND created_at < %s
           AND (deleted_at IS NULL OR deleted_at > %s) ORDER BY id""",
        (customer_id, e_dt, s_dt),
    ).fetchall()
    for c in circuits:
        priced = dict(c)
        if pl["circuit_per_mbps_month"] is not None:
            priced["price_per_mbps_month"] = pl["circuit_per_mbps_month"]
        ch = fabric.charges(conn, priced, s_dt, e_dt)
        amount = money(dec(ch["total"]))
        if amount <= 0:
            continue
        mbps_hours = sum((dec(seg["mbps"]) * dec(seg["hours"]) for seg in ch["segments"]), Decimal(0))
        price = dec(priced["price_per_mbps_month"])
        out.append(
            _line(
                "circuit",
                f"Fabric virtual circuit {c['name']}, elastic bandwidth: {_num(half_up(mbps_hours, '0.01'))}"
                " Mbps-hours",
                half_up(mbps_hours, "0.01"),
                "Mbps-hour",
                half_up(price / fabric.HOURS_PER_MONTH, "0.000001"),
                amount,
                {
                    "circuit": c["name"],
                    "metered": "hourly",
                    "price_per_mbps_month": price,
                    "hours_per_month": fabric.HOURS_PER_MONTH,
                    "segments": ch["segments"],
                },
                circuit_id=c["id"],
            )
        )
    return out


def _credit_lines(
    conn, customer_id, sites: list[dict], site_fee: dict, pl: dict, s_dt: dt.datetime, e_dt: dt.datetime, complete: bool
) -> list[dict]:
    """Automatic SLA credits: per site and class (best effort excluded), the share
    of the period's 10 s windows that met the class SLA, against the credit table.
    The same count as the Overview's 24-hour figure (sla.windows)."""
    table = validate_credit_table(pl["sla_credits"] or [])
    if not table or not site_fee:
        return []
    order = {
        r["name"]: (r["ordinal"], r["name"])
        for r in conn.execute("SELECT name, ordinal FROM app_classes WHERE customer_id = %s", (customer_id,))
    }
    rows = sla.windows(conn, [s["id"] for s in sites], s_dt, e_dt)
    by_site: dict[Any, list[dict]] = {}
    for r in rows:
        by_site.setdefault(r["site_id"], []).append(r)
    cap_pct = dec(pl["credit_cap_pct"])
    span = "of the month" if complete else "of the month so far"
    out = []
    for s in sites:
        fee = site_fee.get(s["id"], Decimal(0))
        if fee <= 0:
            continue
        cap = money(fee * cap_pct / 100)
        given = Decimal(0)
        for r in sorted(by_site.get(s["id"], []), key=lambda r: order.get(r["class_name"], (999, r["class_name"]))):
            if r["best_effort"] or not r["windows"]:
                continue
            met_pct = Decimal(r["met"]) * 100 / Decimal(r["windows"])
            tier = credit_for(met_pct, table)
            if tier is None:
                continue
            credit_pct = dec(tier["credit_pct"])
            amount = money(fee * credit_pct / 100)
            capped = given + amount > cap
            if capped:
                amount = cap - given
            if amount <= 0:
                continue
            given += amount
            reason = (
                f"SLA credit: {r['class_name'].capitalize()} at {s['name']} met its SLA {pct_text(met_pct)}% {span}"
                f" against {pct_text(tier['below_pct'])}%, so {pct_text(credit_pct)}% of the site fee"
            )
            if capped:
                reason += f", capped at {pct_text(cap_pct)}%"
            out.append(
                _line(
                    "credit",
                    reason,
                    1,
                    "credit",
                    -amount,
                    -amount,
                    {
                        "site": s["name"],
                        "class_name": r["class_name"],
                        "windows": r["windows"],
                        "met": r["met"],
                        "met_pct": half_up(met_pct, "0.0001"),
                        "below_pct": tier["below_pct"],
                        "credit_pct": credit_pct,
                        "site_fee": fee,
                        "cap": cap,
                        "capped": capped,
                        "source": "path_metrics",
                    },
                    site_id=s["id"],
                    class_name=r["class_name"],
                )
            )
    return out


def _meter_label(meter: str) -> str:
    base, _, channel = meter.partition(":")
    names = {
        "ai_reply": "AI replies",
        "ai_tokens": "AI tokens",
        "copilot": "Copilot suggestions",
        "message_out": "Outbound messages",
    }
    label = names.get(base, base.replace("_", " ").capitalize())
    return f"{label} ({channel})" if channel else label


def _price_for(meter: str, prices: dict) -> tuple[Decimal | None, str | None]:
    """A meter's price: exact (message_out:whatsapp), then the family (message_out:*)."""
    if meter in prices:
        return dec(prices[meter]), meter
    base = meter.partition(":")[0]
    for key in (f"{base}:*", base):
        if key in prices:
            return dec(prices[key]), key
    return None, None


def _commai_lines(conn, customer_id, pl: dict, frm: dt.date, to: dt.date) -> list[dict]:
    """CommAI usage (commai/usage.py's records) at the plan's meter prices, then
    CommAI Voice's own rated charges (commai/voice/billing.py) as they are."""
    s_dt, e_dt = at_midnight(frm), at_midnight(to)
    prices = pl["meter_prices"] or {}
    out = []
    for r in conn.execute(
        """SELECT meter, sum(quantity) AS quantity, count(*)::int AS records FROM usage_records
           WHERE customer_id = %s AND at >= %s AND at < %s AND meter <> ALL(%s)
           GROUP BY meter ORDER BY meter""",
        (customer_id, s_dt, e_dt, list(VOICE_METERS)),
    ).fetchall():
        qty = dec(r["quantity"])
        price, key = _price_for(r["meter"], prices)
        desc = f"{_meter_label(r['meter'])}: {_num(half_up(qty, '0.000001'))}"
        if price is None:
            desc += " (not priced on this plan)"
        out.append(
            _line(
                "usage",
                desc,
                qty,
                "each",
                price or 0,
                qty * (price or 0),
                {"meter": r["meter"], "records": r["records"], "price_key": key, "source": "usage_records"},
            )
        )

    # Voice: voice billing's monthly user and number fees are made the way it makes
    # them (idempotent), for organisations that have voice at all.
    if conn.execute(
        """SELECT 1 FROM voice_users WHERE customer_id = %(c)s AND billing_from IS NOT NULL
           UNION ALL SELECT 1 FROM voice_numbers WHERE customer_id = %(c)s AND billing_from IS NOT NULL LIMIT 1""",
        {"c": customer_id},
    ).fetchone():
        from ..commai.voice import billing as voice_billing

        voice_billing.monthly_charges(conn, customer_id, frm.replace(day=1))
    rows = conn.execute(
        """SELECT c.kind, count(*)::int AS charges, sum(c.quantity) AS quantity, sum(c.amount) AS amount,
                  array_agg(DISTINCT c.rate_card_version) FILTER (WHERE c.rate_card_version IS NOT NULL) AS versions,
                  array_agg(DISTINCT r.currency) FILTER (WHERE r.currency IS NOT NULL) AS currencies
           FROM voice_charges c LEFT JOIN voice_rate_cards r ON r.id = c.rate_card_id
           WHERE c.customer_id = %s AND c.at >= %s AND c.at < %s AND c.invoice_id IS NULL
           GROUP BY c.kind ORDER BY c.kind""",
        (customer_id, s_dt, e_dt),
    ).fetchall()
    on_voice_invoice = conn.execute(
        """SELECT count(*)::int AS n FROM voice_charges WHERE customer_id = %s AND at >= %s AND at < %s
           AND invoice_id IS NOT NULL""",
        (customer_id, s_dt, e_dt),
    ).fetchone()["n"]
    for r in rows:
        other = [c for c in (r["currencies"] or []) if c != pl["currency"]]
        if other:
            raise BillingError(
                f"Voice is rated in {', '.join(other)} but this plan bills in {pl['currency']}. "
                "Put both on the same currency first.",
                409,
            )
        qty, amount = dec(r["quantity"]), dec(r["amount"])
        unit = VOICE_UNITS.get(r["kind"], "item")
        out.append(
            _line(
                "voice",
                f"{VOICE_LABELS.get(r['kind'], r['kind'])}: {_num(half_up(qty, '0.01'))} {unit}s"
                f" ({r['charges']} rated charge{'s' if r['charges'] != 1 else ''})",
                qty,
                unit,
                amount / qty if qty else amount,
                amount,
                {
                    "voice_kind": r["kind"],
                    "charges": r["charges"],
                    "rate_card_versions": r["versions"] or [],
                    "rated_amount": amount,
                    "source": "voice_charges",
                    "already_on_voice_invoices": on_voice_invoice,
                },
            )
        )
    return out


# ---- invoices --------------------------------------------------------------------------


def _check_period(start: dt.date, now: dt.datetime) -> None:
    if start > _month_start(now):
        raise BillingError(f"{period_label(start)} hasn't started yet.", 422)


def generate_draft(
    conn: psycopg.Connection, sub: dict, start: dt.date, actor: str, now: dt.datetime | None = None
) -> dict:
    """Build, or rebuild, the draft invoice for a subscription and month. Running
    it again replaces the draft's lines with freshly worked-out ones on the same
    invoice; an issued invoice is never touched (void it first)."""
    now = _today(now)
    _check_period(start, now)
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('billing_invoice:' || %s || ':' || %s))",
        (str(sub["id"]), start.isoformat()),
    )
    live = conn.execute(
        """SELECT * FROM billing_invoices WHERE subscription_id = %s AND period_start = %s
           AND status IN ('draft', 'issued') FOR UPDATE""",
        (sub["id"], start),
    ).fetchone()
    if live and live["status"] == "issued":
        raise BillingError(
            f"The {sub['plan']} invoice for {period_label(start)} is already issued as {live['number']}. "
            "Void it first to make a new draft.",
            409,
        )
    calc = compute_subscription(conn, sub, start, now)
    head = {
        "price_list_id": calc["price_list"]["id"],
        "plan": calc["plan"],
        "covered_from": calc["covered_from"],
        "covered_to": calc["covered_to"],
        "currency": calc["currency"],
        "charges": calc["charges"],
        "credits": calc["credits"],
        "subtotal": calc["subtotal"],
        "tax_rate_pct": calc["tax_rate_pct"],
        "tax": calc["tax"],
        "total": calc["total"],
        "example": calc["example"],
    }
    if live:
        conn.execute("DELETE FROM billing_invoice_lines WHERE invoice_id = %s", (live["id"],))
        sets = ", ".join(f"{k} = %({k})s" for k in head)
        inv = conn.execute(
            f"UPDATE billing_invoices SET {sets}, generated_at = now() WHERE id = %(id)s RETURNING *",
            {**head, "id": live["id"]},
        ).fetchone()
    else:
        row = {
            **head,
            "customer_id": sub["customer_id"],
            "product": calc["product"],
            "subscription_id": sub["id"],
            "plan_id": calc["plan_id"],
            "period_start": start,
            "period_end": calc["period_end"],
            "created_by": actor,
        }
        cols = ", ".join(row)
        vals = ", ".join(f"%({k})s" for k in row)
        inv = conn.execute(f"INSERT INTO billing_invoices ({cols}) VALUES ({vals}) RETURNING *", row).fetchone()
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO billing_invoice_lines (invoice_id, customer_id, position, product, plan, kind, description,
                 site_id, link_id, circuit_id, class_name, quantity, unit, unit_price, amount, inputs)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    inv["id"],
                    sub["customer_id"],
                    x["position"],
                    x["product"],
                    x["plan"],
                    x["kind"],
                    x["description"],
                    x["site_id"],
                    x["link_id"],
                    x["circuit_id"],
                    x["class_name"],
                    x["quantity"],
                    x["unit"],
                    x["unit_price"],
                    x["amount"],
                    Jsonb(x["inputs"]),
                )
                for x in calc["lines"]
            ],
        )
    audit.record(
        conn,
        actor,
        "billing.invoice.draft",
        f"{calc['customer']} {calc['plan']} {calc['period']}",
        sub["customer_id"],
        {"invoice_id": str(inv["id"]), "plan": calc["plan"], "rebuilt": live is not None, "total": f"{inv['total']:f}"},
    )
    return get_invoice(conn, inv["id"], None, include_drafts=True)


def generate(
    conn: psycopg.Connection,
    start: dt.date,
    actor: str,
    customer_id: Any | None = None,
    product: str | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Drafts for every subscription covering the month (one organisation's, or
    everyone's); months already issued are skipped with the reason."""
    now = _today(now)
    _check_period(start, now)
    if product is not None and product not in plans_mod.PRODUCTS:
        raise BillingError("Choose Connect or CommAI.")
    plans_mod.sync_all(conn)
    end = next_month(start)
    if customer_id is not None:
        if not conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone():
            raise BillingError("Customer not found.", 404)
        subs = plans_mod.overlapping(conn, customer_id, start, end, product)
    else:
        subs = conn.execute(
            """SELECT s.*, p.name AS plan FROM subscriptions s JOIN plans p ON p.id = s.plan_id
               JOIN customers c ON c.id = s.customer_id
               WHERE s.starts_on < %(e)s AND (s.ends_on IS NULL OR s.ends_on > %(s)s)
                 AND (%(p)s::text IS NULL OR s.product = %(p)s)
               ORDER BY c.name, s.product, s.starts_on""",
            {"s": start, "e": end, "p": product},
        ).fetchall()
    made, skipped = [], []
    for sub in subs:
        try:
            with conn.transaction():
                made.append(generate_draft(conn, sub, start, actor, now))
        except BillingError as e:
            name = conn.execute("SELECT name FROM customers WHERE id = %s", (sub["customer_id"],)).fetchone()["name"]
            skipped.append({"customer_id": sub["customer_id"], "customer": name, "plan": sub["plan"], "reason": str(e)})
    return {"drafts": made, "skipped": skipped}


def issue(conn: psycopg.Connection, invoice_id: Any, actor: str, now: dt.datetime | None = None) -> dict:
    """Give a draft its number (EXA-<year>-NNNN, in order across all organisations
    and plans) and freeze it. Only a period that has ended can be issued."""
    now = _today(now)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('billing_invoice_number'))")
    inv = _row(conn, invoice_id, lock=True)
    if inv["status"] != "draft":
        raise BillingError(f"Only a draft can be issued; this invoice is {inv['status']}.", 409)
    if at_midnight(inv["covered_to"]) > now:
        raise BillingError(
            f"{period_label(inv['period_start'])} hasn't ended yet for this plan. Issue its invoice from "
            f"{inv['covered_to'].day} {inv['covered_to']:%B %Y}.",
            409,
        )
    year = now.year
    last = conn.execute(
        "SELECT max(substring(number FROM 10)::int) AS n FROM billing_invoices WHERE number LIKE %s",
        (f"EXA-{year}-%",),
    ).fetchone()["n"]
    number = invoice_number(year, last)
    conn.execute(
        "UPDATE billing_invoices SET status = 'issued', number = %s, issued_by = %s, issued_at = %s WHERE id = %s",
        (number, actor, now, inv["id"]),
    )
    audit.record(
        conn,
        actor,
        "billing.invoice.issue",
        number,
        inv["customer_id"],
        {"invoice_id": str(inv["id"]), "plan": inv["plan"], "total": f"{inv['total']:f}"},
    )
    return get_invoice(conn, inv["id"], None, include_drafts=True)


def void(conn: psycopg.Connection, invoice_id: Any, reason: str, actor: str) -> dict:
    """Void a draft or an issued invoice. The invoice and its number are kept;
    the month can then be drafted again. A paid invoice can't be voided."""
    inv = _row(conn, invoice_id, lock=True)
    if inv["status"] == "void":
        raise BillingError("This invoice is already void.", 409)
    if conn.execute(
        "SELECT 1 FROM billing_payments WHERE invoice_id = %s AND status = 'paid'", (inv["id"],)
    ).fetchone():
        raise BillingError("This invoice has been paid, so it can't be voided. Refund it with the provider.", 409)
    reason = (reason or "").strip()
    if inv["status"] == "issued" and not reason:
        raise BillingError("Give a reason for voiding an issued invoice.")
    conn.execute(
        """UPDATE billing_invoices SET status = 'void', voided_by = %s, voided_at = now(), void_reason = %s
           WHERE id = %s""",
        (actor, reason[:300], inv["id"]),
    )
    audit.record(
        conn,
        actor,
        "billing.invoice.void",
        inv["number"] or f"draft {inv['plan']} {inv['period_start']:%Y-%m}",
        inv["customer_id"],
        {"invoice_id": str(inv["id"]), "reason": reason[:300]},
    )
    return get_invoice(conn, inv["id"], None, include_drafts=True)


def _row(conn, invoice_id: Any, lock: bool = False) -> dict:
    if not _is_uuid(invoice_id):
        raise BillingError("Invoice not found.", 404)
    inv = conn.execute(
        "SELECT * FROM billing_invoices WHERE id = %s" + (" FOR UPDATE" if lock else ""), (str(invoice_id),)
    ).fetchone()
    if inv is None:
        raise BillingError("Invoice not found.", 404)
    return inv


HEAD_SQL = """SELECT i.*, c.name AS customer, to_char(i.period_start, 'YYYY-MM') AS period,
                     p.version AS price_list_version, p.label AS price_list_label,
                     (p.customer_id IS NOT NULL) AS own_list,
                     (SELECT max(bp.paid_at) FROM billing_payments bp
                      WHERE bp.invoice_id = i.id AND bp.status = 'paid') AS paid_at
              FROM billing_invoices i JOIN customers c ON c.id = i.customer_id
              JOIN price_lists p ON p.id = i.price_list_id"""


def list_invoices(
    conn: psycopg.Connection,
    customer_id: Any | None,
    include_drafts: bool,
    period: dt.date | None = None,
    product: str | None = None,
) -> list[dict]:
    rows = conn.execute(
        HEAD_SQL
        + """ WHERE (%(c)s::uuid IS NULL OR i.customer_id = %(c)s) AND (%(d)s OR i.status <> 'draft')
              AND (%(p)s::date IS NULL OR i.period_start = %(p)s) AND (%(pr)s::text IS NULL OR i.product = %(pr)s)
              ORDER BY i.period_start DESC, c.name, i.product, i.covered_from, i.created_at DESC""",
        {"c": customer_id, "d": include_drafts, "p": period, "pr": product},
    ).fetchall()
    for r in rows:
        r["label"] = period_label(r["period_start"])
    return rows


def get_invoice(conn: psycopg.Connection, invoice_id: Any, customer_id: Any | None, include_drafts: bool) -> dict:
    """One invoice with its lines. Customer users see only their own, and never drafts."""
    _row(conn, invoice_id)
    inv = conn.execute(
        HEAD_SQL + " WHERE i.id = %(id)s AND (%(c)s::uuid IS NULL OR i.customer_id = %(c)s)",
        {"id": str(invoice_id), "c": customer_id},
    ).fetchone()
    if inv is None or (inv["status"] == "draft" and not include_drafts):
        raise BillingError("Invoice not found.", 404)
    inv["label"] = period_label(inv["period_start"])
    inv["lines"] = conn.execute(
        """SELECT id, position, product, plan, kind, description, site_id, link_id, circuit_id, class_name, quantity,
                  unit, unit_price, amount, inputs FROM billing_invoice_lines WHERE invoice_id = %s
           ORDER BY position""",
        (str(invoice_id),),
    ).fetchall()
    return inv


def invoice_csv(inv: dict) -> str:
    """The invoice as CSV: a header block, one row per line (naming its plan), then the totals."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice", inv["number"] or "draft"])
    w.writerow(["status", inv["status"]])
    w.writerow(["customer", inv["customer"]])
    w.writerow(["plan", inv["plan"], inv["product"]])
    w.writerow(["period", inv["period"], inv["covered_from"].isoformat(), inv["covered_to"].isoformat()])
    w.writerow(["currency", inv["currency"]])
    if inv["example"]:
        w.writerow(["note", "Example data: priced with example prices, not real prices"])
    w.writerow([])
    w.writerow(["line", "plan", "kind", "description", "class", "quantity", "unit", "unit_price", "amount"])
    for x in inv["lines"]:
        w.writerow(
            [
                x["position"],
                x["plan"],
                x["kind"],
                x["description"],
                x["class_name"] or "",
                f"{dec(x['quantity']).normalize():f}",
                x["unit"],
                f"{dec(x['unit_price']).normalize():f}",
                f"{dec(x['amount']):.2f}",
            ]
        )
    w.writerow([])
    for k in ("charges", "credits", "subtotal"):
        w.writerow([k, f"{dec(inv[k]):.2f}"])
    w.writerow(["tax_rate_pct", f"{dec(inv['tax_rate_pct']).normalize():f}"])
    for k in ("tax", "total"):
        w.writerow([k, f"{dec(inv[k]):.2f}"])
    return buf.getvalue()


def invoice_html(inv: dict) -> str:
    """A printable page for one invoice, in ExaCarib's brand, standing on its own."""
    e = html.escape
    title = inv["number"] or f"Draft invoice, {inv['label']}"
    rows = "".join(
        f"<tr><td>{x['position']}</td><td>{e(x['description'])}<div class=muted>{e(x['plan'])}</div></td>"
        f"<td class=n>{e(_num(x['quantity']))} {e(x['unit'])}</td><td class=n>{e(_num(x['unit_price']))}</td>"
        f"<td class=n>{dec(x['amount']):,.2f}</td></tr>"
        for x in inv["lines"]
    )
    banner = ""
    if inv["example"]:
        banner += "<p class=tag>Example data: priced with example prices, not real prices.</p>"
    if inv["status"] == "draft":
        banner += "<p class=tag>Draft: not yet issued.</p>"
    if inv["status"] == "void":
        banner += f"<p class='tag danger'>Void: {e(inv.get('void_reason') or '')}</p>"
    paid = "<p class=ok>Paid</p>" if inv.get("paid_at") else ""
    totals_rows = "".join(
        f"<tr><td colspan=4 class=n>{label}</td><td class=n>{dec(inv[k]):,.2f}</td></tr>"
        for label, k in (
            ("Charges", "charges"),
            ("SLA credits", "credits"),
            ("Subtotal", "subtotal"),
            (f"Tax at {_num(inv['tax_rate_pct'])}%", "tax"),
        )
    )
    last_day = inv["covered_to"] - dt.timedelta(days=1)
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#10213D;background:#fff;
margin:32px auto;max-width:860px;padding:0 16px}}
h1{{font-weight:650;letter-spacing:-.01em;margin:0 0 4px}}
.eyebrow{{color:#155EEF;text-transform:uppercase;font-size:12px;letter-spacing:.06em;font-weight:600}}
.muted{{color:#52647A;font-size:12px}} .n{{text-align:right;font-family:"IBM Plex Mono",ui-monospace,monospace}}
table{{width:100%;border-collapse:collapse;margin-top:16px}} td,th{{border-bottom:1px solid #DFE6EE;padding:6px 8px;
vertical-align:top;text-align:left}} th{{color:#52647A;font-weight:600;font-size:12px}}
.tag{{display:inline-block;border:1px solid #DFE6EE;border-radius:5px;padding:4px 8px;margin:4px 0;color:#9A5B00}}
.danger{{color:#B42318}} .ok{{color:#18704B;font-weight:600}}
.total td{{font-weight:650;border-bottom:2px solid #10213D}}
.logo{{font-weight:700;font-size:20px}} .logo b{{color:#155EEF}} .logo i{{color:#07182E;font-style:normal}}
@media print{{body{{margin:0}}}}
</style></head><body>
<div class=logo><b>X</b><i>X</i> ExaCarib</div>
<p class=eyebrow>Invoice</p>
<h1>{e(title)}</h1>
<p>{e(inv["customer"])}<br><span class=muted>{e(inv["plan"])}, {e(inv["label"])}
({inv["covered_from"]:%d %b %Y} to {last_day:%d %b %Y}). Amounts in {e(inv["currency"])}.</span></p>
{banner}{paid}
<table><thead><tr><th>#</th><th>Description</th><th class=n>Quantity</th><th class=n>Unit price</th>
<th class=n>Amount</th></tr></thead><tbody>{rows}{totals_rows}
<tr class=total><td colspan=4 class=n>Total</td><td class=n>{dec(inv["total"]):,.2f}</td></tr></tbody></table>
<p class=muted>Every line keeps the figures it was worked out from; they are shown in the portal.</p>
</body></html>"""


# ---- supplier side: carrier and partner payables, and margin ---------------------------
#
# Kept apart from customer price lists: what ExaCarib owes a carrier comes from the
# link's carrier prices (links.cost_per_mbps, links.burst_price) through the metering
# settlement, exactly as the carrier view and its CSV show it; what it owes a Fabric
# partner comes from partners.cost_per_mbps_month, hour by hour like the customer side.

SERVICES = {
    "plan": "plan_fees",
    "site": "sites",
    "commit": "connectivity",
    "burst": "connectivity",
    "satellite": "connectivity",
    "circuit": "fabric",
    "credit": "credits",
    "usage": "commai_usage",
    "voice": "voice",
}
SERVICE_LABELS = {
    "plan_fees": "Plan fees",
    "sites": "Site fees",
    "connectivity": "Carrier links (commit, burst at the 95th percentile, satellite data)",
    "fabric": "Fabric virtual circuits (hourly)",
    "credits": "SLA credits",
    "commai_usage": "CommAI usage",
    "voice": "CommAI Voice",
}


def link_payables(conn: psycopg.Connection, customer_id: Any | None, s_dt: dt.datetime, e_dt: dt.datetime) -> list:
    """Per carrier link (PoP links included): commit plus burst at the 95th percentile,
    from the same settlement code and samples as the metering screen."""
    links = conn.execute(
        """SELECT l.id, l.customer_id, cu.name AS customer, s.name AS site, l.path, p.label AS path_label,
                  ca.id AS carrier_id, ca.name AS carrier, l.commit_mbps, l.cost_per_mbps, l.burst_price
           FROM links l JOIN sites s ON s.id = l.site_id JOIN paths p ON p.name = l.path
           JOIN carriers ca ON ca.id = l.carrier_id JOIN customers cu ON cu.id = l.customer_id
           WHERE %(c)s::uuid IS NULL OR l.customer_id = %(c)s
           ORDER BY ca.name, cu.name, s.name, p.ordinal""",
        {"c": customer_id},
    ).fetchall()
    out = []
    for link in links:
        st = settle(
            rollup.samples(conn, link["id"], s_dt, e_dt),
            link["commit_mbps"],
            link["cost_per_mbps"],
            link["burst_price"],
        )
        out.append(
            {
                "link_id": link["id"],
                "customer_id": link["customer_id"],
                "customer": link["customer"],
                "carrier_id": link["carrier_id"],
                "carrier": link["carrier"],
                "site": link["site"],
                "path_label": link["path_label"],
                "samples": st.samples,
                "billable_mbps": st.billable_mbps,
                "commit_mbps": dec(link["commit_mbps"]),
                "commit_charge": st.commit_charge,
                "burst_mbps": st.burst_mbps,
                "burst_charge": st.burst_charge,
                "total": st.total,
            }
        )
    return out


def partner_payables(conn: psycopg.Connection, customer_id: Any | None, s_dt: dt.datetime, e_dt: dt.datetime) -> list:
    """Per Fabric circuit ordered through a partner: the hours at each speed, at the
    partner's cost per Mbps a month (unknown until it is set)."""
    rows = conn.execute(
        """SELECT c.*, cu.name AS customer, p.name AS partner, p.cost_per_mbps_month AS partner_cost
           FROM circuits c JOIN partners p ON p.id = c.partner_id JOIN customers cu ON cu.id = c.customer_id
           WHERE (%(c)s::uuid IS NULL OR c.customer_id = %(c)s) AND c.created_at < %(e)s
             AND (c.deleted_at IS NULL OR c.deleted_at > %(s)s)
           ORDER BY p.name, cu.name, c.id""",
        {"c": customer_id, "s": s_dt, "e": e_dt},
    ).fetchall()
    out = []
    for c in rows:
        cost = c["partner_cost"]
        ch = fabric.charges(conn, {**c, "price_per_mbps_month": cost or 0}, s_dt, e_dt)
        if not ch["segments"]:
            continue
        hours = sum((dec(seg["mbps"]) * dec(seg["hours"]) for seg in ch["segments"]), Decimal(0))
        out.append(
            {
                "circuit_id": c["id"],
                "circuit": c["name"],
                "customer_id": c["customer_id"],
                "customer": c["customer"],
                "partner_id": c["partner_id"],
                "partner": c["partner"],
                "mbps_hours": half_up(hours, "0.01"),
                "cost_per_mbps_month": None if cost is None else dec(cost),
                "total": None if cost is None else money(dec(ch["total"])),
            }
        )
    return out


def payables(conn: psycopg.Connection, start: dt.date) -> dict:
    """What ExaCarib owes each carrier and partner for a month. Read only."""
    s_dt, e_dt = at_midnight(start), at_midnight(next_month(start))
    carriers: dict[str, dict] = {}
    for x in link_payables(conn, None, s_dt, e_dt):
        c = carriers.setdefault(
            x["carrier"],
            {
                "carrier": x["carrier"],
                "carrier_id": x["carrier_id"],
                "commit": Decimal(0),
                "burst": Decimal(0),
                "total": Decimal(0),
                "links": [],
            },
        )
        c["commit"] += x["commit_charge"]
        c["burst"] += x["burst_charge"]
        c["total"] += x["total"]
        c["links"].append(x)
    partners: dict[str, dict] = {}
    for x in partner_payables(conn, None, s_dt, e_dt):
        p = partners.setdefault(
            x["partner"],
            {
                "partner": x["partner"],
                "partner_id": x["partner_id"],
                "total": Decimal(0),
                "unpriced": 0,
                "circuits": [],
            },
        )
        if x["total"] is None:
            p["unpriced"] += 1
        else:
            p["total"] += x["total"]
        p["circuits"].append(x)
    return {
        "period": f"{start:%Y-%m}",
        "label": period_label(start),
        "currency": "USD",
        "carriers": list(carriers.values()),
        "partners": list(partners.values()),
        "total": money(
            sum((c["total"] for c in carriers.values()), Decimal(0))
            + sum((p["total"] for p in partners.values()), Decimal(0))
        ),
    }


def set_partner_cost(conn: psycopg.Connection, partner_id: int, cost: Any, actor: str) -> dict:
    value = None if cost in (None, "") else dec(cost)
    if value is not None and value < 0:
        raise BillingError("Costs can't be negative.")
    row = conn.execute(
        "UPDATE partners SET cost_per_mbps_month = %s, updated_at = now() WHERE id = %s"
        " RETURNING id, name, cost_per_mbps_month",
        (value, partner_id),
    ).fetchone()
    if row is None:
        raise BillingError("Partner not found.", 404)
    audit.record(
        conn,
        actor,
        "billing.partner_cost.set",
        row["name"],
        None,
        {"partner_id": partner_id, "cost_per_mbps_month": None if value is None else f"{value:f}"},
    )
    return row


def margin(conn: psycopg.Connection, start: dt.date, now: dt.datetime | None = None) -> dict:
    """Per organisation and per service: what ExaCarib bills (the live invoices, or
    the month worked out now for plans not yet drafted), what it owes carriers and
    partners for that organisation, and the difference. Read only. Margin is only
    worked out in USD, the currency carriers are settled in."""
    end = next_month(start)
    s_dt, e_dt = at_midnight(start), at_midnight(end)
    out = []
    grand = {k: {"revenue": Decimal(0), "cost": Decimal(0)} for k in SERVICE_LABELS}
    all_usd = True
    for c in conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall():
        links = link_payables(conn, c["id"], s_dt, e_dt)
        circuits = partner_payables(conn, c["id"], s_dt, e_dt)
        subs = plans_mod.overlapping(conn, c["id"], start, end)
        if not subs and not links and not circuits:
            continue
        sources: list[dict] = []
        lines: list[dict] = []
        currencies: set[str] = set()
        example, revenue = False, Decimal(0)
        for sub in subs:
            live = conn.execute(
                """SELECT id, number, status, currency, subtotal, example, plan FROM billing_invoices
                   WHERE subscription_id = %s AND period_start = %s AND status IN ('draft', 'issued')""",
                (sub["id"], start),
            ).fetchone()
            if live:
                ls = conn.execute(
                    "SELECT kind, amount FROM billing_invoice_lines WHERE invoice_id = %s", (live["id"],)
                ).fetchall()
                sources.append(
                    {"plan": live["plan"], "source": live["status"], "invoice_id": live["id"], "number": live["number"]}
                )
                sub_rev, cur, ex = live["subtotal"], live["currency"], live["example"]
            else:
                try:
                    with conn.transaction():
                        calc = compute_subscription(conn, sub, start, now)
                except BillingError as e:
                    sources.append({"plan": sub["plan"], "source": "unavailable", "reason": str(e)})
                    continue
                ls = calc["lines"]
                sources.append({"plan": sub["plan"], "source": "estimate", "invoice_id": None, "number": None})
                sub_rev, cur, ex = calc["subtotal"], calc["currency"], calc["example"]
            lines += ls
            currencies.add(cur)
            example = example or bool(ex)
            revenue += dec(sub_rev)
        same = currencies <= {"USD"}
        all_usd = all_usd and same
        link_cost = sum((x["total"] for x in links), Decimal(0))
        partner_cost = sum((x["total"] for x in circuits if x["total"] is not None), Decimal(0))
        cost = link_cost + partner_cost
        by_service = {k: Decimal(0) for k in SERVICE_LABELS}
        for x in lines:
            by_service[SERVICES[x["kind"]]] += dec(x["amount"])
        service_cost = {k: Decimal(0) for k in SERVICE_LABELS}
        service_cost["connectivity"], service_cost["fabric"] = link_cost, partner_cost
        services = []
        for k, label in SERVICE_LABELS.items():
            if not by_service[k] and not service_cost[k]:
                continue
            if same:
                grand[k]["revenue"] += by_service[k]
                grand[k]["cost"] += service_cost[k]
            services.append(
                {
                    "service": k,
                    "label": label,
                    "revenue": money(by_service[k]),
                    "cost": money(service_cost[k]),
                    "margin": money(by_service[k] - service_cost[k]) if same else None,
                }
            )
        out.append(
            {
                "customer_id": c["id"],
                "customer": c["name"],
                "plans": sources,
                "currency": "USD" if same else ", ".join(sorted(currencies)),
                "example": example,
                "revenue": money(revenue),
                "carrier_cost": money(link_cost),
                "partner_cost": money(partner_cost),
                "unpriced_circuits": sum(1 for x in circuits if x["total"] is None),
                "cost": money(cost),
                "margin": money(revenue - cost) if same else None,
                "margin_pct": half_up((revenue - cost) * 100 / revenue, "0.1") if same and revenue > 0 else None,
                "services": services,
                "carrier_lines": links,
                "partner_lines": circuits,
            }
        )
    by_service_total = [
        {
            "service": k,
            "label": SERVICE_LABELS[k],
            "revenue": money(v["revenue"]),
            "cost": money(v["cost"]),
            "margin": money(v["revenue"] - v["cost"]),
        }
        for k, v in grand.items()
        if v["revenue"] or v["cost"]
    ]
    return {
        "period": f"{start:%Y-%m}",
        "label": period_label(start),
        "cost_currency": "USD",
        "all_usd": all_usd,
        "customers": out,
        "services": by_service_total,
    }
