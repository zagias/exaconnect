"""Builds the 5-minute usage samples from interface counters.

Each pass recomputes the last hour of complete buckets for every link with an
enrolled node, so late telemetry (an agent catching up after a controller
outage) is folded in. Site links are metered on the site's underlay
interface; the PoP's links on its interface facing each carrier.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from .. import db
from .core import BUCKET_S, Counter, bucket_of, five_minute_samples

LOOKBACK = dt.timedelta(minutes=65)


def rollup_customer(conn: psycopg.Connection, customer_id: Any, now: dt.datetime) -> int:
    links = conn.execute(
        """SELECT l.id, l.carrier_id, l.underlay_interface, n.id AS node_id
           FROM links l JOIN nodes n ON n.site_id = l.site_id WHERE l.customer_id = %s""",
        (customer_id,),
    ).fetchall()
    done = bucket_of(now)  # buckets before this one are complete
    written = 0
    for link in links:
        rows = conn.execute(
            """SELECT time, rx_bytes, tx_bytes FROM iface_counters
               WHERE node_id = %s AND ifname = %s AND time > %s AND time <= %s ORDER BY time""",
            (link["node_id"], link["underlay_interface"], now - LOOKBACK - dt.timedelta(seconds=BUCKET_S), now),
        ).fetchall()
        samples = [
            s
            for s in five_minute_samples(Counter(r["time"], r["rx_bytes"], r["tx_bytes"]) for r in rows)
            if s.bucket < done and s.bucket >= now - LOOKBACK
        ]
        if not samples:
            continue
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO usage_5m (link_id, bucket, customer_id, carrier_id, in_mbps, out_mbps, seconds)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (link_id, bucket) DO UPDATE SET in_mbps = EXCLUDED.in_mbps,
                     out_mbps = EXCLUDED.out_mbps, seconds = EXCLUDED.seconds""",
                [
                    (link["id"], s.bucket, customer_id, link["carrier_id"], s.in_mbps, s.out_mbps, s.seconds)
                    for s in samples
                ],
            )
        written += len(samples)
    return written


def run_once(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    with db.tx() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(4244) AS ok").fetchone()["ok"]:
            return 0
        return sum(rollup_customer(conn, c["id"], now) for c in conn.execute("SELECT id FROM customers").fetchall())
