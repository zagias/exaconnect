"""Carrier anomaly flags: a path that is worse than usual for the hour.

The SLA engine asks "is this path good enough for voice?". This asks a
different question: "is Carrier B behaving unusually?" A carrier can degrade
well inside every SLA (latency up from 25 to 45 ms), and that is worth
knowing before it becomes a breach, and worth showing the carrier.

For every site's path, the last 15 minutes of probes are compared with the
same hour of day over the last 14 days (in 15-minute blocks), or with the
last 24 hours while there is less than 3 days of history. The baseline is
the median, and the spread is the median absolute deviation, so past faults
don't drag the baseline. A flag needs both a large deviation (4 robust
standard deviations) and a material one (for latency, 10 ms and 20%).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .insights import raise_insight, resolve_others

RECENT = dt.timedelta(minutes=15)
HISTORY = dt.timedelta(days=14)
MIN_SAME_HOUR_BLOCKS = 12  # 3 days of this hour
MIN_ROLLING_BLOCKS = 8  # 2 hours
MIN_RECENT_PROBES = 100


@dataclass(frozen=True)
class Rule:
    label: str
    unit: str
    abs_margin: float
    rel_margin: float


RULES = {
    "latency": Rule("latency", "ms", 10.0, 0.2),
    "jitter": Rule("jitter", "ms", 5.0, 0.5),
    "loss": Rule("loss", "%", 0.5, 0.0),
}


@dataclass(frozen=True)
class Baseline:
    median: float
    spread: float  # robust standard deviation: 1.4826 x MAD
    blocks: int
    kind: str  # "same hour" or "last 24 h"


def baseline(values: Sequence[float], kind: str) -> Baseline | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    return Baseline(med, 1.4826 * mad, len(vals), kind)


def limit(metric: str, b: Baseline) -> float:
    r = RULES[metric]
    return b.median + max(4 * b.spread, r.abs_margin, r.rel_margin * b.median)


def check(metric: str, recent: float | None, b: Baseline | None) -> str | None:
    """None, "warning", or "critical" (more than twice the margin)."""
    if recent is None or b is None:
        return None
    lim = limit(metric, b)
    if recent <= lim:
        return None
    return "critical" if recent > b.median + 2 * (lim - b.median) else "warning"


BLOCKS_SQL = """
SELECT node_id, path,
       date_trunc('hour', time) + floor(extract(minute FROM time) / 15) * interval '15 minutes' AS block,
       sum(sent) AS sent, sum(received) AS received,
       sum(rtt_avg_ms * received) FILTER (WHERE rtt_avg_ms IS NOT NULL)
         / nullif(sum(received) FILTER (WHERE rtt_avg_ms IS NOT NULL), 0) AS latency,
       avg(jitter_ms) AS jitter
FROM path_metrics
WHERE time >= %(since)s AND time < %(until)s
GROUP BY 1, 2, 3
"""

RECENT_SQL = """
SELECT node_id, path, sum(sent) AS sent, sum(received) AS received,
       sum(rtt_avg_ms * received) FILTER (WHERE rtt_avg_ms IS NOT NULL)
         / nullif(sum(received) FILTER (WHERE rtt_avg_ms IS NOT NULL), 0) AS latency,
       avg(jitter_ms) AS jitter
FROM path_metrics WHERE time >= %(since)s AND time <= %(now)s
GROUP BY 1, 2
"""


def _metrics(row: dict) -> dict[str, float | None]:
    sent = row["sent"] or 0
    return {
        "latency": row["latency"],
        "jitter": row["jitter"],
        "loss": 100.0 * (sent - (row["received"] or 0)) / sent if sent else None,
    }


def run_once(conn, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    links = {
        (r["node_id"], r["path"]): r
        for r in conn.execute(
            """SELECT n.id AS node_id, l.path, l.id AS link_id, l.customer_id, l.site_id, l.carrier_id,
                      s.name AS site, c.name AS carrier
               FROM nodes n JOIN sites s ON s.id = n.site_id
               JOIN links l ON l.site_id = s.id JOIN carriers c ON c.id = l.carrier_id"""
        ).fetchall()
    }
    blocks: dict[tuple, list[dict]] = defaultdict(list)
    for r in conn.execute(BLOCKS_SQL, {"since": now - HISTORY, "until": now - dt.timedelta(hours=1)}).fetchall():
        blocks[(r["node_id"], r["path"])].append(r)
    seen: dict[Any, set[str]] = {lk["customer_id"]: set() for lk in links.values()}
    for r in conn.execute(RECENT_SQL, {"since": now - RECENT, "now": now}).fetchall():
        lk = links.get((r["node_id"], r["path"]))
        if lk is None or (r["sent"] or 0) < MIN_RECENT_PROBES or not r["received"]:
            continue  # unknown path, too little data, or down (outages are BFD events, not anomalies)
        recent = _metrics(r)
        hist = blocks.get((r["node_id"], r["path"]), [])
        same_hour = [b for b in hist if b["block"].hour == now.hour]
        if len(same_hour) >= MIN_SAME_HOUR_BLOCKS:
            pool, kind = same_hour, "same hour"
        else:
            pool = [b for b in hist if b["block"] >= now - dt.timedelta(hours=25)]
            kind = "last 24 h"
            if len(pool) < MIN_ROLLING_BLOCKS:
                continue
        hist_metrics = [_metrics(b) for b in pool if b["sent"]]
        for metric in RULES:
            b = baseline([m[metric] for m in hist_metrics], kind)
            severity = check(metric, recent[metric], b)
            if severity is None or b is None:
                continue
            rule = RULES[metric]
            fmt = (lambda v: f"{v:.2f}%") if metric == "loss" else (lambda v: f"{v:.1f} ms")
            where = f"{lk['carrier']} at {lk['site']}"
            usual = "at this hour" if kind == "same hour" else "over the last day"
            days = len(pool) / 4 if kind == "same hour" else None
            title = f"{where}: {rule.label} {fmt(recent[metric])}, usually {fmt(b.median)}"
            detail = (
                f"{where}: {rule.label} averaged {fmt(recent[metric])} over the last 15 minutes, against a usual "
                f"{fmt(b.median)} {usual}"
                + (f" (median of {days:.0f} days)" if days else f" (median of {b.blocks} quarter-hours)")
                + f". The flag is raised above {fmt(limit(metric, b))}."
            )
            key = f"anomaly:{lk['link_id']}:{metric}"
            raise_insight(
                conn,
                lk["customer_id"],
                "anomaly",
                key,
                severity,
                title,
                detail,
                {
                    "metric": metric,
                    "recent": recent[metric],
                    "median": b.median,
                    "spread": b.spread,
                    "limit": limit(metric, b),
                    "baseline": kind,
                    "blocks": b.blocks,
                    "carrier": lk["carrier"],
                    "path": lk["path"],
                },
                site_id=lk["site_id"],
                link_id=lk["link_id"],
                carrier_id=lk["carrier_id"],
            )
            seen[lk["customer_id"]].add(key)
    for customer_id, keys in seen.items():
        resolve_others(conn, customer_id, "anomaly", keys)
    return sum(len(k) for k in seen.values())
