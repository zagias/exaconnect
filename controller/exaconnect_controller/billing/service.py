"""Billing for Connect (ADR 0024): price lists, the month's charges and SLA
credits, and draft, issued and void invoices.

Charges are worked out on demand from inventory and metering; nothing is
stored until a draft invoice is generated. Each line keeps its inputs
(quantities, prices, the samples behind a 95th percentile, the SLA windows
behind a credit) so a person can see why it is there.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit, fabric, sla
from ..metering import rollup
from ..metering.core import gigabytes, money, settle
from .core import (
    MONEY_FIELDS,
    BillingError,
    active_fraction,
    at_midnight,
    credit_for,
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


# ---- price lists -----------------------------------------------------------------------


def price_list_for(conn: psycopg.Connection, customer_id: Any, at: dt.date) -> dict:
    """The list in effect on a day: the customer's own latest version that has
    started, otherwise ExaCarib's default."""
    row = conn.execute(
        """SELECT * FROM price_lists WHERE (customer_id = %(c)s OR customer_id IS NULL) AND effective_from <= %(at)s
           ORDER BY (customer_id IS NULL), effective_from DESC, version DESC LIMIT 1""",
        {"c": customer_id, "at": at},
    ).fetchone()
    if row is None:
        raise BillingError("There is no price list in effect for this month.", 409)
    return row


def price_lists(conn: psycopg.Connection, customer_id: Any | None, default: bool) -> list[dict]:
    """Every version of a customer's lists (newest first), or of the default list."""
    if default:
        return conn.execute("SELECT * FROM price_lists WHERE customer_id IS NULL ORDER BY version DESC").fetchall()
    return conn.execute(
        "SELECT * FROM price_lists WHERE customer_id = %s ORDER BY version DESC", (customer_id,)
    ).fetchall()


def create_price_list(conn: psycopg.Connection, customer_id: Any | None, values: dict, actor: str) -> dict:
    """Save a new version. Earlier versions stay as they were, so invoices that
    used them can always be explained."""
    currency = str(values.get("currency") or "USD").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise BillingError("Currency is a three-letter code, such as USD or TTD.")
    prices = {}
    for k in MONEY_FIELDS:
        v = dec(values.get(k, 0))
        if v < 0:
            raise BillingError("Prices can't be negative.")
        prices[k] = v
    circuit = values.get("circuit_per_mbps_month")
    circuit = None if circuit in (None, "") else dec(circuit)
    if circuit is not None and circuit < 0:
        raise BillingError("Prices can't be negative.")
    tax = dec(values.get("tax_rate_pct", 0))
    cap = dec(values.get("credit_cap_pct", 50))
    if not (0 <= tax <= 100) or not (0 <= cap <= 100):
        raise BillingError("Tax and the credit cap are percentages between 0 and 100.")
    table = validate_credit_table(values.get("sla_credits") or [])
    eff = values.get("effective_from")
    if isinstance(eff, str):
        try:
            eff = dt.date.fromisoformat(eff)
        except ValueError as e:
            raise BillingError("Give the effective date as YYYY-MM-DD.") from e
    if not isinstance(eff, dt.date):
        raise BillingError("Give the date the price list takes effect.")
    key = str(customer_id) if customer_id else "default"
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('price_list:' || %s))", (key,))
    last = conn.execute(
        "SELECT max(version) AS v FROM price_lists WHERE customer_id IS NOT DISTINCT FROM %s", (customer_id,)
    ).fetchone()["v"]
    row = conn.execute(
        """INSERT INTO price_lists (customer_id, version, effective_from, label, currency, site_monthly,
             commit_per_mbps, burst_per_mbps, satellite_per_gb, circuit_per_mbps_month, tax_rate_pct, sla_credits,
             credit_cap_pct, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            (last or 0) + 1,
            eff,
            str(values.get("label") or "")[:120],
            currency,
            prices["site_monthly"],
            prices["commit_per_mbps"],
            prices["burst_per_mbps"],
            prices["satellite_per_gb"],
            circuit,
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
        f"{'default' if customer_id is None else 'customer'} v{row['version']}",
        customer_id,
        jsonable({k: row[k] for k in ("version", "effective_from", "currency", *MONEY_FIELDS, "tax_rate_pct")}),
    )
    return row


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


def _num(v: Any) -> str:
    """100 -> '100', 12.5 -> '12.5' for descriptions."""
    s = f"{dec(v).normalize():f}"
    return s


def compute(conn: psycopg.Connection, customer_id: Any, start: dt.date, now: dt.datetime | None = None) -> dict:
    """Every charge and credit for one customer and calendar month (UTC)."""
    now = _today(now)
    end = next_month(start)
    s_dt, e_dt = at_midnight(start), at_midnight(end)
    complete = now >= e_dt
    customer = conn.execute("SELECT id, name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    if customer is None:
        raise BillingError("Customer not found.", 404)
    pl = price_list_for(conn, customer_id, start)
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
        days, dim = active_fraction(s["created_at"], start, end)
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
            samples = rollup.samples(conn, link["id"], s_dt, e_dt)
            commit = dec(link["commit_mbps"])
            st = settle(samples, commit, pl["commit_per_mbps"], pl["burst_per_mbps"])
            where = f"{link['path_label']} at {s['name']}"
            if commit > 0:
                commit_amount = (
                    st.commit_charge if days == dim else money(commit * dec(pl["commit_per_mbps"]) * days / dim)
                )
                lines.append(
                    _line(
                        "commit",
                        f"{where}: {_num(commit)} Mbps commit{part}",
                        commit,
                        "Mbps a month",
                        pl["commit_per_mbps"],
                        commit_amount,
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
    for i, x in enumerate(lines, 1):
        x["position"] = i
    t = totals(lines, pl["tax_rate_pct"])
    return {
        "customer_id": customer["id"],
        "customer": customer["name"],
        "period": f"{start:%Y-%m}",
        "period_start": start,
        "period_end": end,
        "label": period_label(start),
        "complete": complete,
        "price_list": pl,
        "currency": pl["currency"],
        "example": pl["example"],
        "lines": lines,
        **t,
    }


def _circuit_lines(conn, customer_id, pl: dict, s_dt: dt.datetime, e_dt: dt.datetime) -> list[dict]:
    """Virtual circuits and elastic bandwidth: each speed for the hours it was set
    (fabric.charges), at the circuit's price or the price list's when it has one."""
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
                f"Fabric elastic bandwidth, circuit {c['name']}: {_num(half_up(mbps_hours, '0.01'))} Mbps-hours",
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
    of the month's 10 s windows that met the class SLA, against the credit table.
    The same count as the Overview's 24-hour figure (sla.windows)."""
    table = validate_credit_table(pl["sla_credits"] or [])
    if not table or not sites:
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
                f"{r['class_name'].capitalize()} at {s['name']} met its SLA {pct_text(met_pct)}% {span} against "
                f"{pct_text(tier['below_pct'])}%: {pct_text(credit_pct)}% credit"
            )
            if capped:
                reason += f", capped at {pct_text(cap_pct)}% of the site fee"
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


# ---- invoices --------------------------------------------------------------------------


def _check_period(start: dt.date, now: dt.datetime) -> None:
    if start > _month_start(now):
        raise BillingError(f"{period_label(start)} hasn't started yet.", 422)


def generate_draft(
    conn: psycopg.Connection, customer_id: Any, start: dt.date, actor: str, now: dt.datetime | None = None
) -> dict:
    """Build, or rebuild, the draft invoice for a customer and month. Running it
    again replaces the draft's lines with freshly worked-out ones; an issued
    invoice is never touched (void it first)."""
    now = _today(now)
    _check_period(start, now)
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('billing_invoice:' || %s || ':' || %s))",
        (str(customer_id), start.isoformat()),
    )
    live = conn.execute(
        """SELECT * FROM billing_invoices WHERE customer_id = %s AND period_start = %s
           AND status IN ('draft', 'issued') FOR UPDATE""",
        (customer_id, start),
    ).fetchone()
    if live and live["status"] == "issued":
        raise BillingError(
            f"The invoice for {period_label(start)} is already issued as {live['number']}. "
            "Void it first to make a new draft.",
            409,
        )
    calc = compute(conn, customer_id, start, now)
    head = (
        calc["price_list"]["id"],
        calc["currency"],
        calc["charges"],
        calc["credits"],
        calc["subtotal"],
        calc["tax_rate_pct"],
        calc["tax"],
        calc["total"],
        calc["example"],
    )
    if live:
        conn.execute("DELETE FROM billing_invoice_lines WHERE invoice_id = %s", (live["id"],))
        inv = conn.execute(
            """UPDATE billing_invoices SET price_list_id = %s, currency = %s, charges = %s, credits = %s,
                 subtotal = %s, tax_rate_pct = %s, tax = %s, total = %s, example = %s, generated_at = now()
               WHERE id = %s RETURNING *""",
            (*head, live["id"]),
        ).fetchone()
    else:
        inv = conn.execute(
            """INSERT INTO billing_invoices (price_list_id, currency, charges, credits, subtotal, tax_rate_pct, tax,
                 total, example, customer_id, period_start, period_end, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (*head, customer_id, start, calc["period_end"], actor),
        ).fetchone()
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO billing_invoice_lines (invoice_id, customer_id, position, kind, description, site_id,
                 link_id, circuit_id, class_name, quantity, unit, unit_price, amount, inputs)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    inv["id"],
                    customer_id,
                    x["position"],
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
        f"{calc['customer']} {calc['period']}",
        customer_id,
        {"invoice_id": str(inv["id"]), "rebuilt": live is not None, "total": f"{inv['total']:f}"},
    )
    return get_invoice(conn, inv["id"], None, include_drafts=True)


def generate_all(conn: psycopg.Connection, start: dt.date, actor: str, now: dt.datetime | None = None) -> dict:
    """Drafts for every customer with sites; months already issued are skipped."""
    made, skipped = [], []
    for c in conn.execute(
        "SELECT id, name FROM customers c WHERE EXISTS (SELECT 1 FROM sites s WHERE s.customer_id = c.id"
        " AND s.kind = 'site') ORDER BY name"
    ).fetchall():
        try:
            with conn.transaction():
                made.append(generate_draft(conn, c["id"], start, actor, now))
        except BillingError as e:
            skipped.append({"customer_id": c["id"], "customer": c["name"], "reason": str(e)})
    return {"drafts": made, "skipped": skipped}


def issue(conn: psycopg.Connection, invoice_id: Any, actor: str, now: dt.datetime | None = None) -> dict:
    """Give a draft its number (EXA-<year>-NNNN, in order across all customers) and
    freeze it. Only a month that has ended can be issued."""
    now = _today(now)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('billing_invoice_number'))")
    inv = _row(conn, invoice_id, lock=True)
    if inv["status"] != "draft":
        raise BillingError(f"Only a draft can be issued; this invoice is {inv['status']}.", 409)
    if at_midnight(inv["period_end"]) > now:
        raise BillingError(
            f"{period_label(inv['period_start'])} hasn't ended yet. Issue its invoice from "
            f"{inv['period_end'].day} {inv['period_end']:%B %Y}.",
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
        {"invoice_id": str(inv["id"]), "total": f"{inv['total']:f}"},
    )
    return get_invoice(conn, inv["id"], None, include_drafts=True)


def void(conn: psycopg.Connection, invoice_id: Any, reason: str, actor: str) -> dict:
    """Void a draft or an issued invoice. The invoice and its number are kept;
    the month can then be drafted again."""
    inv = _row(conn, invoice_id, lock=True)
    if inv["status"] == "void":
        raise BillingError("This invoice is already void.", 409)
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
        inv["number"] or f"draft {inv['period_start']:%Y-%m}",
        inv["customer_id"],
        {"invoice_id": str(inv["id"]), "reason": reason[:300]},
    )
    return get_invoice(conn, inv["id"], None, include_drafts=True)


def _row(conn, invoice_id: Any, lock: bool = False) -> dict:
    try:
        uuid.UUID(str(invoice_id))
    except ValueError as e:
        raise BillingError("Invoice not found.", 404) from e
    inv = conn.execute(
        "SELECT * FROM billing_invoices WHERE id = %s" + (" FOR UPDATE" if lock else ""), (invoice_id,)
    ).fetchone()
    if inv is None:
        raise BillingError("Invoice not found.", 404)
    return inv


HEAD_SQL = """SELECT i.*, c.name AS customer, to_char(i.period_start, 'YYYY-MM') AS period,
                     p.version AS price_list_version, p.label AS price_list_label,
                     (p.customer_id IS NULL) AS default_list,
                     (SELECT max(bp.paid_at) FROM billing_payments bp
                      WHERE bp.invoice_id = i.id AND bp.status = 'paid') AS paid_at
              FROM billing_invoices i JOIN customers c ON c.id = i.customer_id
              JOIN price_lists p ON p.id = i.price_list_id"""


def list_invoices(
    conn: psycopg.Connection, customer_id: Any | None, include_drafts: bool, period: dt.date | None = None
) -> list[dict]:
    rows = conn.execute(
        HEAD_SQL
        + """ WHERE (%(c)s::uuid IS NULL OR i.customer_id = %(c)s) AND (%(d)s OR i.status <> 'draft')
              AND (%(p)s::date IS NULL OR i.period_start = %(p)s)
              ORDER BY i.period_start DESC, c.name, i.created_at DESC""",
        {"c": customer_id, "d": include_drafts, "p": period},
    ).fetchall()
    for r in rows:
        r["label"] = period_label(r["period_start"])
    return rows


def get_invoice(conn: psycopg.Connection, invoice_id: Any, customer_id: Any | None, include_drafts: bool) -> dict:
    """One invoice with its lines. Customer users see only their own, and never drafts."""
    _row(conn, invoice_id)
    inv = conn.execute(
        HEAD_SQL + " WHERE i.id = %(id)s AND (%(c)s::uuid IS NULL OR i.customer_id = %(c)s)",
        {"id": invoice_id, "c": customer_id},
    ).fetchone()
    if inv is None or (inv["status"] == "draft" and not include_drafts):
        raise BillingError("Invoice not found.", 404)
    inv["label"] = period_label(inv["period_start"])
    inv["lines"] = conn.execute(
        """SELECT id, position, kind, description, site_id, link_id, circuit_id, class_name, quantity, unit,
                  unit_price, amount, inputs FROM billing_invoice_lines WHERE invoice_id = %s ORDER BY position""",
        (invoice_id,),
    ).fetchall()
    return inv


def invoice_csv(inv: dict) -> str:
    """The invoice as CSV: a header block, one row per line, then the totals."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice", inv["number"] or "draft"])
    w.writerow(["status", inv["status"]])
    w.writerow(["customer", inv["customer"]])
    w.writerow(["period", inv["period"], inv["period_start"].isoformat(), inv["period_end"].isoformat()])
    w.writerow(["currency", inv["currency"]])
    if inv["example"]:
        w.writerow(["note", "Example data: priced with the example price list, not real prices"])
    w.writerow([])
    w.writerow(["line", "kind", "description", "class", "quantity", "unit", "unit_price", "amount"])
    for x in inv["lines"]:
        w.writerow(
            [
                x["position"],
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


# ---- supplier side: carrier and partner payables, and margin ---------------------------
#
# Kept apart from customer price lists: what ExaCarib owes a carrier comes from the
# link's carrier prices (links.cost_per_mbps, links.burst_price) through the metering
# settlement, exactly as the carrier view and its CSV show it; what it owes a Fabric
# partner comes from partners.cost_per_mbps_month, hour by hour like the customer side.

SERVICES = {
    "site": "sites",
    "commit": "connectivity",
    "burst": "connectivity",
    "satellite": "connectivity",
    "circuit": "fabric",
    "credit": "credits",
}
SERVICE_LABELS = {
    "sites": "Site fees",
    "connectivity": "Carrier links (commit, burst at the 95th percentile, satellite data)",
    "fabric": "Fabric elastic bandwidth (hourly)",
    "credits": "SLA credits",
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
        hours = sum((dec(seg["mbps"]) * dec(seg["hours"]) for seg in ch["segments"]), Decimal(0))
        if not ch["segments"]:
            continue
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
    """Per customer and per service: what ExaCarib bills (the live invoice, or the
    month worked out now), what it owes carriers and partners for that customer,
    and the difference. Read only."""
    end = next_month(start)
    s_dt, e_dt = at_midnight(start), at_midnight(end)
    out = []
    for c in conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall():
        links = link_payables(conn, c["id"], s_dt, e_dt)
        circuits = partner_payables(conn, c["id"], s_dt, e_dt)
        live = conn.execute(
            """SELECT id, number, status, currency, subtotal, example FROM billing_invoices
               WHERE customer_id = %s AND period_start = %s AND status IN ('draft', 'issued')""",
            (c["id"], start),
        ).fetchone()
        if live:
            lines = conn.execute(
                "SELECT kind, amount FROM billing_invoice_lines WHERE invoice_id = %s", (live["id"],)
            ).fetchall()
            revenue, currency, source, example = live["subtotal"], live["currency"], live["status"], live["example"]
        else:
            has_sites = conn.execute(
                "SELECT 1 FROM sites WHERE customer_id = %s AND kind = 'site' LIMIT 1", (c["id"],)
            ).fetchone()
            if not has_sites and not links:
                continue
            calc = compute(conn, c["id"], start, now)
            lines = calc["lines"]
            revenue, currency, source, example = calc["subtotal"], calc["currency"], "estimate", calc["example"]
        same = currency == "USD"
        link_cost = sum((x["total"] for x in links), Decimal(0))
        partner_cost = sum((x["total"] for x in circuits if x["total"] is not None), Decimal(0))
        cost = link_cost + partner_cost
        by_service = {k: Decimal(0) for k in SERVICE_LABELS}
        for x in lines:
            by_service[SERVICES[x["kind"]]] += dec(x["amount"])
        service_cost = {"sites": Decimal(0), "connectivity": link_cost, "fabric": partner_cost, "credits": Decimal(0)}
        services = [
            {
                "service": k,
                "label": label,
                "revenue": money(by_service[k]),
                "cost": money(service_cost[k]),
                "margin": money(by_service[k] - service_cost[k]) if same else None,
            }
            for k, label in SERVICE_LABELS.items()
        ]
        out.append(
            {
                "customer_id": c["id"],
                "customer": c["name"],
                "invoice_id": live["id"] if live else None,
                "invoice_number": live["number"] if live else None,
                "revenue_source": source,
                "currency": currency,
                "example": example,
                "revenue": money(dec(revenue)),
                "carrier_cost": money(link_cost),
                "partner_cost": money(partner_cost),
                "unpriced_circuits": sum(1 for x in circuits if x["total"] is None),
                "cost": money(cost),
                "margin": money(dec(revenue) - cost) if same else None,
                "margin_pct": (
                    half_up((dec(revenue) - cost) * 100 / dec(revenue), "0.1") if same and dec(revenue) > 0 else None
                ),
                "services": services,
                "carrier_lines": links,
                "partner_lines": circuits,
            }
        )
    return {"period": f"{start:%Y-%m}", "label": period_label(start), "cost_currency": "USD", "customers": out}


__all__ = [
    "BillingError",
    "compute",
    "create_price_list",
    "generate_all",
    "generate_draft",
    "get_invoice",
    "invoice_csv",
    "issue",
    "jsonable",
    "list_invoices",
    "margin",
    "payables",
    "set_partner_cost",
    "price_list_for",
    "price_lists",
    "void",
]
