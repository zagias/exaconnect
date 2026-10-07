"""Billing arithmetic (ADR 0024). Pure functions, unit-tested by hand.

Money is Decimal end to end and every line is rounded to the cent (half up)
before it is added up, so the invoice total is always the sum of its lines.
The 95th percentile and burst come from metering.core, never from here.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..metering.core import money

PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class BillingError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def dec(v: Any) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError) as e:
        raise BillingError(f"{v!r} is not a number.") from e
    if not d.is_finite():
        raise BillingError(f"{v!r} is not a number.")
    return d


# ---- periods ---------------------------------------------------------------------------


def parse_period(period: str) -> dt.date:
    """'2026-09' -> the first day of that month."""
    m = PERIOD_RE.match(period or "")
    if not m:
        raise BillingError("Give the month as YYYY-MM, for example 2026-09.")
    return dt.date(int(m.group(1)), int(m.group(2)), 1)


def next_month(d: dt.date) -> dt.date:
    return (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def period_label(start: dt.date) -> str:
    return f"{calendar.month_name[start.month]} {start.year}"


def at_midnight(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC)


def active_fraction(created_at: dt.datetime | None, start: dt.date, end: dt.date) -> tuple[int, int]:
    """Days of the month a site was billable, and the days in the month. A site
    added during the month pays from the day it was added (UTC)."""
    days = (end - start).days
    if created_at is None:
        return days, days
    first = max(created_at.astimezone(dt.UTC).date(), start)
    return max(0, (end - first).days), days


# ---- price lists -----------------------------------------------------------------------

MONEY_FIELDS = ("site_monthly", "commit_per_mbps", "burst_per_mbps", "satellite_per_gb")


def validate_credit_table(rows: Any) -> list[dict]:
    """[{below_pct, credit_pct}], highest threshold first. A threshold is the SLA-met
    percentage a class must reach; below it, the credit applies."""
    if not isinstance(rows, list):
        raise BillingError("The SLA credit table must be a list of thresholds.")
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            raise BillingError("Each SLA credit row needs below_pct and credit_pct.")
        below, credit = dec(r.get("below_pct")), dec(r.get("credit_pct"))
        if not (0 < below <= 100):
            raise BillingError("An SLA threshold must be above 0% and at most 100%.")
        if not (0 < credit <= 100):
            raise BillingError("An SLA credit must be above 0% and at most 100% of the site fee.")
        if below in seen:
            raise BillingError(f"The SLA threshold {pct_text(below)}% appears twice.")
        seen.add(below)
        out.append({"below_pct": float(below), "credit_pct": float(credit)})
    out.sort(key=lambda r: -r["below_pct"])
    # A lower SLA must never earn a smaller credit.
    for hi, lo in zip(out, out[1:], strict=False):
        if lo["credit_pct"] < hi["credit_pct"]:
            raise BillingError("Credits must grow as the SLA threshold falls.")
    if len(out) > 10:
        raise BillingError("Keep the SLA credit table to 10 rows or fewer.")
    return out


def credit_for(met_pct: Decimal, table: list[dict]) -> dict | None:
    """The tier a month's SLA-met percentage falls into: the largest credit whose
    threshold it is below. None when it met every threshold."""
    hits = [t for t in table if met_pct < dec(t["below_pct"])]
    if not hits:
        return None
    return max(hits, key=lambda t: (dec(t["credit_pct"]), -dec(t["below_pct"])))


def pct_text(v: Any) -> str:
    """98.7 -> '98.7', 99.499 -> '99.49' (rounded down, so it never reads as the threshold)."""
    d = dec(v).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    s = f"{d:f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def money_text(v: Any) -> str:
    return f"{money(dec(v)):,.2f}"


# ---- totals ----------------------------------------------------------------------------


def totals(lines: list[dict], tax_rate_pct: Any) -> dict[str, Decimal]:
    """Charges, credits (zero or negative), subtotal, tax on the subtotal and total."""
    charges = sum((dec(x["amount"]) for x in lines if x["kind"] != "credit"), Decimal(0))
    credits = sum((dec(x["amount"]) for x in lines if x["kind"] == "credit"), Decimal(0))
    subtotal = charges + credits
    rate = dec(tax_rate_pct)
    tax = money(subtotal * rate / 100) if subtotal > 0 else Decimal("0.00")
    return {
        "charges": money(charges),
        "credits": money(credits),
        "subtotal": money(subtotal),
        "tax_rate_pct": rate,
        "tax": tax,
        "total": money(subtotal + tax),
    }


def invoice_number(year: int, last: int | None) -> str:
    return f"EXA-{year}-{(last or 0) + 1:04d}"


def half_up(v: Decimal, places: str) -> Decimal:
    return v.quantize(Decimal(places), rounding=ROUND_HALF_UP)
