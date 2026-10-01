"""Metering arithmetic (CLAUDE.md §4.5). Pure functions, unit-tested by hand.

- Counter deltas become 5-minute average Mbps samples.
- The billable rate for a period is the 95th percentile: sort the 5-minute
  samples, discard the top 5% (rounded down), and take the highest remaining.
  In and out are computed separately and the higher one is billed.
- Settlement: commit charge (commit Mbps x cost per Mbps) plus burst charge
  ((billable - commit) x burst price, when billable is above commit).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

BUCKET_S = 300


@dataclass(frozen=True)
class Counter:
    t: dt.datetime
    rx: int
    tx: int


@dataclass(frozen=True)
class Sample:
    bucket: dt.datetime  # start of the 5-minute bucket
    in_mbps: float
    out_mbps: float
    seconds: float  # seconds of counter data behind the average


def bucket_of(t: dt.datetime) -> dt.datetime:
    epoch = int(t.timestamp())
    return dt.datetime.fromtimestamp(epoch - epoch % BUCKET_S, dt.UTC)


def five_minute_samples(counters: Iterable[Counter], rx_is_in: bool = True) -> list[Sample]:
    """Average Mbps per 5-minute bucket from cumulative byte counters.

    Each delta between consecutive readings is split across the buckets it
    spans in proportion to time. A counter that goes backwards (interface
    recreated) starts over and its delta is dropped. Gaps longer than 15
    minutes are not interpolated.
    """
    pts = sorted(counters, key=lambda c: c.t)
    acc: dict[dt.datetime, list[float]] = {}  # bucket -> [rx bytes, tx bytes, seconds]
    for a, b in zip(pts, pts[1:], strict=False):
        secs = (b.t - a.t).total_seconds()
        if secs <= 0 or secs > 900 or b.rx < a.rx or b.tx < a.tx:
            continue
        drx, dtx = b.rx - a.rx, b.tx - a.tx
        start = a.t
        while start < b.t:
            bk = bucket_of(start)
            end = min(b.t, bk + dt.timedelta(seconds=BUCKET_S))
            part = (end - start).total_seconds()
            frac = part / secs
            slot = acc.setdefault(bk, [0.0, 0.0, 0.0])
            slot[0] += drx * frac
            slot[1] += dtx * frac
            slot[2] += part
            start = end
    out = []
    for bk in sorted(acc):
        rx, tx, secs = acc[bk]
        rx_mbps, tx_mbps = rx * 8 / secs / 1e6, tx * 8 / secs / 1e6
        i, o = (rx_mbps, tx_mbps) if rx_is_in else (tx_mbps, rx_mbps)
        out.append(Sample(bk, round(i, 6), round(o, 6), secs))
    return out


def discarded(n: int) -> int:
    """How many top samples the 95th percentile discards: 5%, rounded down."""
    return math.floor(n * 0.05)


def percentile95(values: Sequence[float]) -> float | None:
    """Sort, discard the top 5% (rounded down), return the highest remaining."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) - 1 - discarded(len(ordered))]


@dataclass(frozen=True)
class Settlement:
    samples: int
    discarded: int
    p95_in: float | None
    p95_out: float | None
    billable_mbps: float | None
    commit_mbps: Decimal
    commit_charge: Decimal
    burst_mbps: Decimal
    burst_charge: Decimal
    total: Decimal


def money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def settle(samples: Sequence[Sample], commit_mbps, cost_per_mbps, burst_price) -> Settlement:
    commit = Decimal(str(commit_mbps))
    p_in = percentile95([s.in_mbps for s in samples])
    p_out = percentile95([s.out_mbps for s in samples])
    billable = None if p_in is None else max(p_in, p_out or 0.0)
    over = max(Decimal(0), Decimal(str(round(billable, 3))) - commit) if billable is not None else Decimal(0)
    over = over.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    commit_charge = money(commit * Decimal(str(cost_per_mbps)))
    burst_charge = money(over * Decimal(str(burst_price)))
    return Settlement(
        samples=len(samples),
        discarded=discarded(len(samples)),
        p95_in=p_in,
        p95_out=p_out,
        billable_mbps=billable,
        commit_mbps=commit,
        commit_charge=commit_charge,
        burst_mbps=over,
        burst_charge=burst_charge,
        total=commit_charge + burst_charge,
    )


def month_bounds(t: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    start = t.astimezone(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    return start, end
