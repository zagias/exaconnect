"""SLA met per site and class, from the probe windows already stored.

The Overview's "SLA met in the last 24 hours" and the monthly SLA credits on
invoices (ADR 0024) both come from here, so they always agree.
"""

from __future__ import annotations

import datetime as dt

import psycopg

# A non-shadow decision that put a class on a path (holds and pauses carry no to_path).
MOVE = "NOT d.shadow AND d.kind <> 'hold' AND d.to_path IS NOT NULL"


def windows(
    conn: psycopg.Connection, site_ids: list, start: dt.datetime | None = None, end: dt.datetime | None = None
) -> list[dict]:
    """Per site and class: 10 s windows in (start, end] (default: the last 24 h), and
    how many met the class SLA on the path the class was actually on at that moment.

    The path a class was on is rebuilt from non-shadow moves: from each move until the
    next, the class sat on its to_path. Before the first move it sat on that move's
    from_path; with no moves at all, on its current steering path. Each path_metrics row
    is one window (its time is the window end). A window meets the SLA when probes came
    back and latency, jitter and loss are within the class limits; a null limit is ignored.
    Rows also say whether the class is best effort (bulk), which carries no SLA credit."""
    return conn.execute(
        f"""WITH sc AS (
              SELECT s.id AS site_id, n.id AS node_id, c.name AS class_name, st.path AS current_path,
                     (c.priority = 'bulk' OR c.name = 'bulk') AS best_effort,
                     sp.max_latency_ms, sp.max_jitter_ms, sp.max_loss_pct
              FROM sites s
              JOIN nodes n ON n.site_id = s.id
              JOIN app_classes c ON c.customer_id = s.customer_id
              LEFT JOIN sla_policies sp ON sp.customer_id = s.customer_id AND sp.class_name = c.name
              LEFT JOIN steering st ON st.site_id = s.id AND st.class_name = c.name
              WHERE s.id = ANY(%(sites)s) AND s.kind = 'site'
            ),
            mv AS (
              SELECT d.site_id, d.class_name, d.id, d.time, d.from_path, d.to_path
              FROM decisions d WHERE d.site_id = ANY(%(sites)s) AND {MOVE}
            ),
            seg AS (
              SELECT site_id, class_name, to_path AS path, time AS t0,
                     COALESCE(lead(time) OVER (PARTITION BY site_id, class_name ORDER BY time, id),
                              'infinity'::timestamptz) AS t1
              FROM mv
              UNION ALL
              SELECT sc.site_id, sc.class_name, COALESCE(f.from_path, sc.current_path),
                     '-infinity'::timestamptz, COALESCE(f.time, 'infinity'::timestamptz)
              FROM sc LEFT JOIN LATERAL (
                SELECT mv.time, mv.from_path FROM mv
                WHERE mv.site_id = sc.site_id AND mv.class_name = sc.class_name
                ORDER BY mv.time, mv.id LIMIT 1
              ) f ON true
            )
            SELECT sc.site_id, sc.class_name, bool_or(sc.best_effort) AS best_effort,
                   count(pm.time)::int AS windows,
                   (count(pm.time) FILTER (WHERE pm.received > 0
                      AND (sc.max_latency_ms IS NULL OR pm.rtt_avg_ms <= sc.max_latency_ms)
                      AND (sc.max_jitter_ms IS NULL OR pm.jitter_ms <= sc.max_jitter_ms)
                      AND (sc.max_loss_pct IS NULL OR pm.loss_pct <= sc.max_loss_pct)))::int AS met
            FROM sc
            JOIN seg ON seg.site_id = sc.site_id AND seg.class_name = sc.class_name
            JOIN path_metrics pm ON pm.node_id = sc.node_id AND pm.path = seg.path
                 AND pm.time >= seg.t0 AND pm.time < seg.t1
                 AND pm.time > COALESCE(%(start)s::timestamptz, now() - interval '24 hours')
                 AND (%(end)s::timestamptz IS NULL OR pm.time <= %(end)s::timestamptz)
            GROUP BY sc.site_id, sc.class_name""",
        {"sites": site_ids, "start": start, "end": end},
    ).fetchall()
