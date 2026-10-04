"""Runs the decision engine every 10 seconds for every customer.

One pass reads the latest probe windows, BFD state and link use from the
database, evaluates every class at every online site, stores decisions and
engine state, and refreshes the agents' steering maps. A Postgres advisory
lock makes sure only one controller process runs a pass at a time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import asdict, replace
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import db
from . import maps
from .engine import STORM_POLICY, Decision, PathInput, Policy, Sla, State, Window, decide, evaluate
from .forecast import Forecaster, TrendForecaster

log = logging.getLogger("exaconnect.routing")

LOCK_ID = 4243
ONLINE_S = 30
BFD_FRESH_S = 60


def policy_for(site: dict) -> Policy:
    return STORM_POLICY if site.get("storm_mode") else Policy()


def _epoch(t: dt.datetime) -> float:
    return t.timestamp()


def _utilisation(conn: psycopg.Connection, node_id: Any, now: dt.datetime) -> dict[str, float]:
    """Mbps per interface from the two latest counter samples (the higher of in and out)."""
    rows = conn.execute(
        """SELECT ifname, time, rx_bytes, tx_bytes FROM (
             SELECT *, row_number() OVER (PARTITION BY ifname ORDER BY time DESC) AS rn
             FROM iface_counters WHERE node_id = %s AND time > %s - interval '5 minutes') x
           WHERE rn <= 2 ORDER BY ifname, time""",
        (node_id, now),
    ).fetchall()
    out: dict[str, float] = {}
    by_if: dict[str, list[dict]] = {}
    for r in rows:
        by_if.setdefault(r["ifname"], []).append(r)
    for ifname, (a, b, *_) in ((k, v) for k, v in by_if.items() if len(v) >= 2):
        secs = (b["time"] - a["time"]).total_seconds()
        if secs <= 0 or b["rx_bytes"] < a["rx_bytes"] or b["tx_bytes"] < a["tx_bytes"]:
            continue  # counter reset (interface recreated)
        out[ifname] = max(b["rx_bytes"] - a["rx_bytes"], b["tx_bytes"] - a["tx_bytes"]) * 8 / secs / 1e6
    return out


def site_inputs(
    conn: psycopg.Connection, site: dict, links: list[dict], policy: Policy, now: dt.datetime
) -> dict[str, PathInput]:
    node_id = site["node_id"]
    bfd = {
        r["path"]: r["bfd"]
        for r in conn.execute(
            "SELECT path, bfd FROM tunnel_state WHERE node_id = %s AND updated_at > %s - make_interval(secs => %s)",
            (node_id, now, BFD_FRESH_S),
        ).fetchall()
    }
    rows = conn.execute(
        """SELECT time, path, sent, received, rtt_avg_ms, jitter_ms FROM path_metrics
           WHERE node_id = %s AND time > %s - make_interval(secs => %s) AND time <= %s ORDER BY time""",
        (node_id, now, policy.history_s, now),
    ).fetchall()
    windows: dict[str, list[Window]] = {}
    for r in rows:
        windows.setdefault(r["path"], []).append(
            Window(_epoch(r["time"]), r["sent"], r["received"], r["rtt_avg_ms"], r["jitter_ms"])
        )
    use = _utilisation(conn, node_id, now)
    out = {}
    for link in links:
        commit = float(link["commit_mbps"] or 0)
        mbps = use.get(link["underlay_interface"], 0.0)
        out[link["name"]] = PathInput(
            name=link["name"],
            label=link["label"],
            ordinal=link["ordinal"],
            satellite=link["satellite"],
            bfd_down=bfd.get(link["name"]) == "down",
            over_commit=commit > 0 and mbps >= 0.9 * commit,
            windows=windows.get(link["name"], []),
        )
    return out


def run_customer(
    conn: psycopg.Connection, customer_id: Any, now: dt.datetime, forecaster: Forecaster | None = None
) -> list[dict]:
    forecaster = forecaster or TrendForecaster()
    inv = maps.load(conn, customer_id)
    customer = inv["customer"]
    t = _epoch(now)
    states = {
        (r["site_id"], r["class_name"]): r
        for r in conn.execute("SELECT * FROM steering WHERE customer_id = %s", (customer_id,)).fetchall()
    }
    online = {
        r["id"]
        for r in conn.execute(
            "SELECT s.id FROM sites s JOIN nodes n ON n.site_id = s.id"
            " WHERE s.customer_id = %s AND n.last_seen > %s - make_interval(secs => %s)",
            (customer_id, now, ONLINE_S),
        ).fetchall()
    }
    underlays = {
        (r["site_id"], r["path"]): r["underlay_interface"]
        for r in conn.execute(
            "SELECT site_id, path, underlay_interface FROM links WHERE customer_id = %s", (customer_id,)
        )
    }
    made: list[dict] = []
    for site in inv["sites"]:
        if site["kind"] != "site" or site["id"] not in online:
            continue
        policy = policy_for(site)
        links = [
            {**lk, "underlay_interface": underlays[(site["id"], lk["name"])]} for lk in inv["links"].get(site["id"], [])
        ]
        inputs = site_inputs(conn, site, links, policy, now)
        for cls in inv["classes"]:
            sla = Sla(
                latency_ms=_f(cls["max_latency_ms"]),
                jitter_ms=_f(cls["max_jitter_ms"]),
                loss_pct=_f(cls["max_loss_pct"]),
            )
            # The class's preferred path comes first among equals (ADR 0007).
            pref = cls["preferred_path"]
            evals = {
                k: evaluate(replace(p, ordinal=-1) if k == pref else p, sla, forecaster, policy, t)
                for k, p in inputs.items()
            }
            cands = maps.candidates(cls, customer, site, links)
            row = states.get((site["id"], cls["name"]))
            state = (
                State(
                    path=row["path"],
                    since=_epoch(row["since"]),
                    return_path=row["return_path"],
                    return_since=_epoch(row["return_since"]) if row["return_since"] else None,
                    breach_streak=row["breach_streak"],
                    note=row["note"],
                )
                if row
                else None
            )
            new, decision = decide(cls["name"], evals, cands, state, policy, t)
            if not new.path:
                continue
            _save_state(conn, site, cls["name"], customer_id, new)
            if decision is not None:
                made.append(_record(conn, customer, site, cls["name"], decision, forecaster.name, policy, evals))
    maps.refresh(conn, customer_id)
    return made


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _ts(x: float | None) -> dt.datetime | None:
    return None if x is None else dt.datetime.fromtimestamp(x, dt.UTC)


def _save_state(conn: psycopg.Connection, site: dict, cls: str, customer_id: Any, s: State) -> None:
    conn.execute(
        """INSERT INTO steering (site_id, class_name, customer_id, path, since, return_path, return_since,
                                breach_streak, note, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (site_id, class_name) DO UPDATE SET path = EXCLUDED.path, since = EXCLUDED.since,
             return_path = EXCLUDED.return_path, return_since = EXCLUDED.return_since,
             breach_streak = EXCLUDED.breach_streak, note = EXCLUDED.note, updated_at = now()""",
        (
            site["id"],
            cls,
            customer_id,
            s.path,
            _ts(s.since),
            s.return_path,
            _ts(s.return_since),
            s.breach_streak,
            s.note,
        ),
    )


def _record(
    conn: psycopg.Connection,
    customer: dict,
    site: dict,
    cls: str,
    d: Decision,
    engine: str,
    policy: Policy,
    evals: dict,
) -> dict:
    inputs = {
        "policy": {k: getattr(policy, k) for k in ("horizon_s", "hold_s", "return_after_s", "confidence")},
        "storm": bool(site.get("storm_mode")),
        "paths": {k: e.summary() for k, e in evals.items()},
    }
    row = conn.execute(
        """INSERT INTO decisions (time, customer_id, site_id, class_name, kind, from_path, to_path, shadow, engine,
                                  reason, inputs)
           VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, time""",
        (
            customer["id"],
            site["id"],
            cls,
            d.kind,
            d.from_path,
            d.to_path,
            bool(customer["shadow_mode"]),
            engine,
            ("[Shadow] " if customer["shadow_mode"] and d.kind != "hold" else "") + d.reason,
            Jsonb(inputs),
        ),
    ).fetchone()
    log.info("%s %s/%s: %s", d.kind, site["name"], cls, d.reason)
    return {"id": row["id"], "site": site["name"], "class": cls, **asdict(d)}


def run_once(now: dt.datetime | None = None, forecaster: Forecaster | None = None) -> list[dict] | None:
    """One pass for all customers. Returns None if another process holds the lock."""
    now = now or dt.datetime.now(dt.UTC)
    made: list[dict] = []
    with db.tx() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_ID,)).fetchone()["ok"]:
            return None
        for c in conn.execute("SELECT id FROM customers ORDER BY name").fetchall():
            made += run_customer(conn, c["id"], now, forecaster)
    return made


def lift_expired_blocks() -> int:
    """Take block list entries that have expired out of the PoPs' desired state (ADR 0012)."""
    from .. import desired

    with db.tx() as conn:
        lifted = conn.execute(
            "UPDATE blocked_sources SET lifted = true WHERE expires_at <= now() AND NOT lifted RETURNING id"
        ).fetchall()
        if lifted:
            for r in conn.execute("SELECT DISTINCT customer_id FROM sites WHERE kind = 'pop'").fetchall():
                desired.refresh(conn, r["customer_id"])
    return len(lifted)


async def loop(interval_s: float) -> None:
    """Background task started by the app: a routing pass every interval and a
    metering rollup about once a minute. Errors are logged, never fatal."""
    from ..metering import rollup

    last_rollup = 0.0
    while True:
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("routing pass failed")
        if time.monotonic() - last_rollup >= 60:
            last_rollup = time.monotonic()
            try:
                await asyncio.to_thread(rollup.run_once)
                await asyncio.to_thread(lift_expired_blocks)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("metering rollup failed")
        await asyncio.sleep(interval_s)
