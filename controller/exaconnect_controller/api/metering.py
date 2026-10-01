"""Metering and settlement API (CLAUDE.md §4.5).

Admins see every link; customer users their own customer's links; carrier
users (read-only) only their own carrier's links. The CSV exports carry
exactly the samples behind each figure, so disputes are settled on the same
numbers.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .. import db
from ..metering.core import Sample, discarded, month_bounds, percentile95, settle
from .deps import UserDep

router = APIRouter(prefix="/metering", tags=["metering"])


def _period(start: dt.datetime | None, end: dt.datetime | None, hours: int | None) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now(dt.UTC)
    if hours:
        return now - dt.timedelta(hours=hours), now
    if start is None:
        start, month_end = month_bounds(now)
        return start, end or month_end
    end = end or now
    if end <= start:
        raise HTTPException(400, "The end of the period must be after its start.")
    if end - start > dt.timedelta(days=93):
        raise HTTPException(400, "Periods are limited to 93 days.")
    return start, end


def _scope(user) -> dict[str, Any]:
    if user.role == "admin":
        return {"c": None, "k": None}
    if user.role == "customer":
        return {"c": user.customer_id, "k": None}
    if user.role == "carrier" and user.carrier_id:
        return {"c": None, "k": user.carrier_id}
    raise HTTPException(403, "Not available for this account.")


LINKS_SQL = """
SELECT l.id, l.customer_id, cu.name AS customer, s.id AS site_id, s.name AS site, s.kind AS site_kind,
       l.path, p.label AS path_label, l.carrier_id, c.name AS carrier, l.underlay_type, l.underlay_interface,
       l.commit_mbps, l.cost_per_mbps, l.burst_price
FROM links l
JOIN sites s ON s.id = l.site_id
JOIN carriers c ON c.id = l.carrier_id
JOIN customers cu ON cu.id = l.customer_id
JOIN paths p ON p.name = l.path
WHERE (%(c)s::uuid IS NULL OR l.customer_id = %(c)s) AND (%(k)s::uuid IS NULL OR l.carrier_id = %(k)s)
"""


def _samples(conn, link_id: Any, start: dt.datetime, end: dt.datetime) -> list[Sample]:
    rows = conn.execute(
        """SELECT bucket, in_mbps, out_mbps, seconds FROM usage_5m
           WHERE link_id = %s AND bucket >= %s AND bucket < %s ORDER BY bucket""",
        (link_id, start, end),
    ).fetchall()
    return [Sample(r["bucket"], r["in_mbps"], r["out_mbps"], r["seconds"]) for r in rows]


def _settlement_row(link: dict, samples: list[Sample]) -> dict:
    st = settle(samples, link["commit_mbps"], link["cost_per_mbps"], link["burst_price"])
    return {
        **link,
        "samples": st.samples,
        "discarded": st.discarded,
        "p95_in_mbps": st.p95_in,
        "p95_out_mbps": st.p95_out,
        "billable_mbps": st.billable_mbps,
        "commit_charge": str(st.commit_charge),
        "burst_mbps": str(st.burst_mbps),
        "burst_charge": str(st.burst_charge),
        "total": str(st.total),
    }


@router.get("/links")
def links(
    user: UserDep,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    hours: int | None = Query(default=None, ge=1, le=24 * 93),
) -> dict:
    """Settlement per link for a period (default: this calendar month, UTC)."""
    start, end = _period(start, end, hours)
    with db.tx() as conn:
        rows = conn.execute(LINKS_SQL + " ORDER BY c.name, s.kind DESC, s.name, p.ordinal", _scope(user)).fetchall()
        out = [_settlement_row(r, _samples(conn, r["id"], start, end)) for r in rows]
    return {"start": start, "end": end, "links": out}


def _link(conn, user, link_id: str) -> dict:
    row = conn.execute(LINKS_SQL + " AND l.id = %(id)s", {**_scope(user), "id": link_id}).fetchone()
    if row is None:
        raise HTTPException(404, "Link not found.")
    return row


@router.get("/links/{link_id}/samples")
def link_samples(
    link_id: str,
    user: UserDep,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    hours: int | None = Query(default=None, ge=1, le=24 * 93),
) -> dict:
    start, end = _period(start, end, hours)
    with db.tx() as conn:
        link = _link(conn, user, link_id)
        samples = _samples(conn, link_id, start, end)
    row = _settlement_row(link, samples)
    # Which samples the 95th percentile discarded, per direction.
    cut_in = sorted(s.in_mbps for s in samples)[len(samples) - discarded(len(samples)) :] if samples else []
    return {
        "start": start,
        "end": end,
        "settlement": row,
        "points": [
            {"bucket": s.bucket, "in_mbps": s.in_mbps, "out_mbps": s.out_mbps, "seconds": s.seconds} for s in samples
        ],
        "discarded_in": cut_in,
    }


CSV_HEADER = [
    "carrier",
    "customer",
    "site",
    "path",
    "link_id",
    "bucket_start_utc",
    "in_mbps",
    "out_mbps",
    "seconds_measured",
]


def _csv(rows: list[list[Any]], name: str, summary: list[list[Any]]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    w.writerows(rows)
    if summary:
        w.writerow([])
        w.writerows(summary)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _summary(r: dict) -> list[Any]:
    return [
        "settlement",
        r["carrier"],
        r["site"],
        r["path"],
        r["id"],
        f"samples={r['samples']}",
        f"discarded={r['discarded']}",
        f"p95_in={r['p95_in_mbps']}",
        f"p95_out={r['p95_out_mbps']}",
        f"billable={r['billable_mbps']}",
        f"commit={r['commit_mbps']}",
        f"commit_charge={r['commit_charge']}",
        f"burst_mbps={r['burst_mbps']}",
        f"burst_charge={r['burst_charge']}",
        f"total={r['total']}",
    ]


@router.get("/settlement.csv")
def settlement_csv(
    user: UserDep,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    hours: int | None = Query(default=None, ge=1, le=24 * 93),
    link_id: str | None = None,
) -> Response:
    """Every 5-minute sample behind the settlement, then one summary row per link."""
    start, end = _period(start, end, hours)
    with db.tx() as conn:
        links = conn.execute(
            LINKS_SQL + " AND (%(id)s::uuid IS NULL OR l.id = %(id)s) ORDER BY c.name, s.name, p.ordinal",
            {**_scope(user), "id": link_id},
        ).fetchall()
        rows, summary = [], []
        for link in links:
            samples = _samples(conn, link["id"], start, end)
            for s in samples:
                rows.append(
                    [
                        link["carrier"],
                        link["customer"],
                        link["site"],
                        link["path"],
                        link["id"],
                        s.bucket.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        f"{s.in_mbps:.6f}",
                        f"{s.out_mbps:.6f}",
                        f"{s.seconds:.0f}",
                    ]
                )
            summary.append(_summary(_settlement_row(link, samples)))
    return _csv(rows, f"settlement-{start:%Y%m%d%H%M}-{end:%Y%m%d%H%M}.csv", summary)


@router.get("/usage")
def usage(
    user: UserDep,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    hours: int | None = Query(default=None, ge=1, le=24 * 93),
) -> dict:
    """Customer view: data per site and path, the share above commit and on
    satellite, and the routing decisions that put traffic there."""
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")
    start, end = _period(start, end, hours)
    scope = _scope(user)
    with db.tx() as conn:
        rows = conn.execute(
            LINKS_SQL + " AND s.kind = 'site' ORDER BY s.name, p.ordinal",
            scope,
        ).fetchall()
        out = []
        totals = {"gb": 0.0, "over_commit_gb": 0.0, "satellite_gb": 0.0}
        for r in rows:
            samples = _samples(conn, r["id"], start, end)
            commit = float(r["commit_mbps"] or 0)
            gb = sum((s.in_mbps + s.out_mbps) * s.seconds / 8 / 1000 for s in samples)
            over = sum(
                (max(0.0, s.in_mbps - commit) + max(0.0, s.out_mbps - commit)) * s.seconds / 8 / 1000 for s in samples
            )
            sat = r["underlay_type"] in ("leo", "geo")
            totals["gb"] += gb
            totals["over_commit_gb"] += over if commit else 0.0
            totals["satellite_gb"] += gb if sat else 0.0
            p95 = percentile95([max(s.in_mbps, s.out_mbps) for s in samples])
            reasons = conn.execute(
                """SELECT time, class_name, reason FROM decisions
                   WHERE site_id = %s AND to_path = %s AND kind <> 'hold' AND time >= %s AND time < %s
                   ORDER BY time DESC LIMIT 5""",
                (r["site_id"], r["path"], start, end),
            ).fetchall()
            out.append(
                {
                    "site": r["site"],
                    "site_id": r["site_id"],
                    "path": r["path"],
                    "path_label": r["path_label"],
                    "carrier": r["carrier"],
                    "satellite": sat,
                    "commit_mbps": commit,
                    "gb": round(gb, 3),
                    "over_commit_gb": round(over, 3) if commit else 0.0,
                    "p95_mbps": p95,
                    "reasons": reasons,
                }
            )
    return {"start": start, "end": end, "totals": {k: round(v, 3) for k, v in totals.items()}, "paths": out}
