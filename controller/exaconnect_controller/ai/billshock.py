"""Bill-shock alerts: forecast each link's month-end 95th percentile.

The billable rate is the highest 5-minute sample left after discarding the
top 5% of the month's samples in each direction, then taking the higher
direction (CLAUDE.md §4.5). Two figures come from the samples so far:

- Locked in: the month's discard budget is fixed (5% of every 5-minute slot
  in the month). If more samples than that are already above some rate, the
  bill can no longer come in below it. This is a hard floor, not a guess.
- Forecast: assume the rest of the month looks like the samples so far,
  adjusted by the recent trend. Then the billable rate is the observed 95th
  percentile scaled by that trend. The forecast is never below the floor.

When the forecast is above commit, the customer (and ExaCarib) see the
burst charge coming while there is still time to act.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from ..metering.core import BUCKET_S, Sample, discarded, money, month_bounds, percentile95
from .insights import raise_insight, resolve_others

MIN_SAMPLES = 12  # an hour of data before saying anything


@dataclass(frozen=True)
class Forecast:
    samples: int
    month_slots: int
    budget: int  # samples the 95th percentile will discard this month
    over_commit: int  # samples above commit in the busiest direction; budgets are separate
    floor_mbps: float  # the bill can't come in below this
    observed_p95: float
    trend: float  # multiplier from the recent trend, 1.0 = flat
    forecast_mbps: float
    confidence: str  # low (< 1 day of data), medium (< 7 days), high
    direction: str | None = None  # the direction setting a link's forecast


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile, q in [0, 1]."""
    s = sorted(values)
    if not s:
        return 0.0
    pos = q * (len(s) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def trend_factor(points: Sequence[tuple[float, float]], until_s: float) -> float:
    """Least-squares line through (t, Mbps) points, extrapolated to the middle
    of the rest of the month, relative to the mean so far. Clipped to 0.8..1.5
    so a short spike or lull can't swing the forecast wildly."""
    if len(points) < 24:
        return 1.0
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    if mv <= 0:
        return 1.0
    sxx = sum((t - mt) ** 2 for t, _ in points)
    if sxx == 0:
        return 1.0
    slope = sum((t - mt) * (v - mv) for t, v in points) / sxx
    last = points[-1][0]
    mid_rest = (last + until_s) / 2
    projected = mv + slope * (mid_rest - mt)
    return max(0.8, min(1.5, projected / mv))


def forecast(
    samples: Sequence[tuple[dt.datetime, float]], commit_mbps: float, period: tuple[dt.datetime, dt.datetime]
) -> Forecast | None:
    """Forecast one direction from (bucket start, Mbps) samples for the month so far."""
    if len(samples) < MIN_SAMPLES:
        return None
    start, end = period
    slots = int((end - start).total_seconds() // BUCKET_S)
    budget = discarded(slots)
    values = [v for _, v in samples]
    desc = sorted(values, reverse=True)
    floor = desc[budget] if len(desc) > budget else 0.0
    # Match settlement's discrete discard rule, including at the 5% boundary.
    observed = percentile95(values)
    assert observed is not None
    # Recent trend: the last 7 days, in hours since the start of the month.
    last_t = samples[-1][0]
    recent = [((t - start).total_seconds() / 3600, v) for t, v in samples if t > last_t - dt.timedelta(days=7)]
    trend = trend_factor(recent, (end - start).total_seconds() / 3600)
    fc = max(floor, observed * trend)
    days = len(samples) * BUCKET_S / 86400
    return Forecast(
        samples=len(samples),
        month_slots=slots,
        budget=budget,
        over_commit=sum(1 for v in values if v > commit_mbps),
        floor_mbps=floor,
        observed_p95=observed,
        trend=trend,
        forecast_mbps=fc,
        confidence="low" if days < 1 else "medium" if days < 7 else "high",
    )


def forecast_link(
    samples: Sequence[Sample], commit_mbps: float, period: tuple[dt.datetime, dt.datetime]
) -> Forecast | None:
    """Apply the billing rule: compute each direction first, then take the higher.

    Combining in/out before taking the percentile incorrectly spends one
    shared discard budget on bursts that settlement discards separately.
    """
    inbound = forecast([(s.bucket, s.in_mbps) for s in samples], commit_mbps, period)
    outbound = forecast([(s.bucket, s.out_mbps) for s in samples], commit_mbps, period)
    if inbound is None or outbound is None:
        return None
    direction, dominant = max((("inbound", inbound), ("outbound", outbound)), key=lambda x: x[1].forecast_mbps)
    return replace(
        dominant,
        floor_mbps=max(inbound.floor_mbps, outbound.floor_mbps),
        observed_p95=max(inbound.observed_p95, outbound.observed_p95),
        over_commit=max(inbound.over_commit, outbound.over_commit),
        direction=direction,
    )


def burst_cost(rate_mbps: float, commit_mbps: float, burst_price: float) -> Decimal:
    over = max(0.0, rate_mbps - commit_mbps)
    return money(Decimal(str(round(over, 3))) * Decimal(str(burst_price)))


def assess(f: Forecast, label: str, commit: float, burst_price: float) -> dict | None:
    """The alert for one link, or None when the month looks fine."""
    budget_used = f.over_commit / f.budget if f.budget else 0.0
    if f.forecast_mbps <= commit and budget_used < 0.5:
        return None
    cost = burst_cost(f.forecast_mbps, commit, burst_price)
    if f.floor_mbps > commit:
        severity = "critical"
        head = f"{label} will bill at least {f.floor_mbps:.1f} Mbps this month against a {commit:g} Mbps commit"
    elif f.forecast_mbps > commit:
        severity = "warning"
        head = f"{label} is forecast to bill {f.forecast_mbps:.1f} Mbps this month against a {commit:g} Mbps commit"
    else:
        severity = "info"
        head = f"{label} has used {budget_used:.0%} of this month's burst allowance"
    detail = (
        f"{head}. {f.over_commit} of the {f.budget} five-minute samples the 95th percentile ignores this month are "
        f"already above commit{(' in the busiest direction' if f.direction else '')}."
    )
    if f.forecast_mbps > commit:
        detail += f" Forecast burst charge: about {cost} (rate {f.forecast_mbps:.1f} Mbps"
        if f.direction:
            detail += f", {f.direction}"
        detail += f", trend ×{f.trend:.2f})." if abs(f.trend - 1) > 0.01 else ")."
    if f.floor_mbps > commit:
        detail += " The excess is locked in: more samples are already above commit than the month can discard."
    detail += f" Based on {f.samples / 12:.0f} hours of samples ({f.confidence} confidence)."
    return {
        "severity": severity,
        "title": head,
        "detail": detail,
        "data": {
            "forecast_mbps": round(f.forecast_mbps, 3),
            "floor_mbps": round(f.floor_mbps, 3),
            "observed_p95_mbps": round(f.observed_p95, 3),
            "trend": round(f.trend, 3),
            "commit_mbps": commit,
            "burst_charge": str(cost),
            "over_commit_samples": f.over_commit,
            "budget": f.budget,
            "samples": f.samples,
            "confidence": f.confidence,
            "forecast_direction": f.direction,
        },
    }


def run_once(conn, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    period = month_bounds(now)
    links = conn.execute(
        """SELECT l.id, l.customer_id, l.site_id, l.carrier_id, l.path, l.commit_mbps, l.burst_price,
                  s.name AS site, c.name AS carrier
           FROM links l JOIN sites s ON s.id = l.site_id JOIN carriers c ON c.id = l.carrier_id"""
    ).fetchall()
    seen: dict[Any, set[str]] = {lk["customer_id"]: set() for lk in links}
    for lk in links:
        rows = conn.execute(
            """SELECT bucket, in_mbps, out_mbps, seconds FROM usage_5m
               WHERE link_id = %s AND bucket >= %s AND bucket < %s ORDER BY bucket""",
            (lk["id"], period[0], period[1]),
        ).fetchall()
        commit = float(lk["commit_mbps"] or 0)
        if commit <= 0:
            continue
        f = forecast_link(
            [Sample(r["bucket"], float(r["in_mbps"]), float(r["out_mbps"]), r["seconds"]) for r in rows],
            commit,
            period,
        )
        if f is None:
            continue
        a = assess(f, f"{lk['carrier']} at {lk['site']}", commit, float(lk["burst_price"] or 0))
        if a is None:
            continue
        key = f"bill:{lk['id']}:{period[0]:%Y-%m}"
        raise_insight(
            conn,
            lk["customer_id"],
            "bill_shock",
            key,
            a["severity"],
            a["title"],
            a["detail"],
            a["data"],
            site_id=lk["site_id"],
            link_id=lk["id"],
            carrier_id=lk["carrier_id"],
        )
        seen[lk["customer_id"]].add(key)
    for customer_id, keys in seen.items():
        resolve_others(conn, customer_id, "bill_shock", keys)
    return sum(len(k) for k in seen.values())
