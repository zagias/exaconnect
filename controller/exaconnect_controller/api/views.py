"""Read APIs for the portal: overview, site detail, path metrics, events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import db
from .deps import ViewerDep, customer_scope

router = APIRouter(tags=["portal"])

ONLINE_SECONDS = 30


def _health(m: dict | None, sla: dict | None) -> str:
    """ok / warn / bad for one path against the voice SLA (the strictest class)."""
    if m is None or m["received"] == 0:
        return "bad"
    if sla is None:
        return "ok"
    checks = [
        (m["rtt_avg_ms"], sla["max_latency_ms"]),
        (m["jitter_ms"], sla["max_jitter_ms"]),
        (m["loss_pct"], sla["max_loss_pct"]),
    ]
    worst = max((float(v) / float(lim) for v, lim in checks if v is not None and lim), default=0.0)
    if worst > 1:
        return "bad"
    if worst > 0.8:
        return "warn"
    return "ok"


def _voice_slas(conn) -> dict[Any, dict]:
    rows = conn.execute("SELECT * FROM sla_policies WHERE class_name = 'voice'").fetchall()
    return {r["customer_id"]: r for r in rows}


def _site_paths(conn, site_ids: list) -> dict[Any, list[dict]]:
    rows = conn.execute(
        """SELECT l.site_id, l.path, p.label, p.ordinal, c.name AS carrier, l.underlay_type, l.commit_mbps,
                  m.rtt_avg_ms, m.jitter_ms, m.loss_pct, m.received, m.sent, m.at
           FROM links l
           JOIN paths p ON p.name = l.path
           JOIN carriers c ON c.id = l.carrier_id
           LEFT JOIN nodes n ON n.site_id = l.site_id
           LEFT JOIN LATERAL (
             SELECT avg(rtt_avg_ms) AS rtt_avg_ms, avg(jitter_ms) AS jitter_ms,
                    CASE WHEN sum(sent) > 0 THEN 100.0 * (sum(sent) - sum(received)) / sum(sent) END AS loss_pct,
                    sum(received) AS received, sum(sent) AS sent, max(time) AS at
             FROM path_metrics pm
             WHERE pm.node_id = n.id AND pm.path = l.path AND pm.time > now() - interval '30 seconds'
           ) m ON true
           WHERE l.site_id = ANY(%s)
           ORDER BY p.ordinal""",
        (site_ids,),
    ).fetchall()
    out: dict[Any, list[dict]] = {}
    for r in rows:
        out.setdefault(r["site_id"], []).append(r)
    return out


@router.get("/overview")
def overview(user: ViewerDep) -> dict:
    scope = customer_scope(user)
    with db.tx() as conn:
        sites = conn.execute(
            f"""SELECT s.id, s.customer_id, s.name, s.kind, s.location, s.timezone,
                      n.id AS node_id, n.last_seen, n.applied_version, n.apply_ok,
                      (n.last_seen > now() - interval '{ONLINE_SECONDS} seconds') AS online,
                      (SELECT max(version) FROM desired_states d WHERE d.node_id = n.id) AS desired_version
               FROM sites s LEFT JOIN nodes n ON n.site_id = s.id
               WHERE %(c)s::uuid IS NULL OR s.customer_id = %(c)s
               ORDER BY s.kind DESC, s.name""",
            {"c": scope},
        ).fetchall()
        paths = _site_paths(conn, [s["id"] for s in sites])
        slas = _voice_slas(conn)
    attention = []
    for s in sites:
        sla = slas.get(s["customer_id"])
        s["paths"] = [{**p, "health": _health(p if p["sent"] else None, sla)} for p in paths.get(s["id"], [])]
        if s["node_id"] is None:
            continue
        if not s["online"]:
            attention.append(f"{s['name']} is offline")
        elif s["kind"] == "site":
            # The PoP only reflects probes; paths are measured from the sites.
            attention += [f"{p['label']} at {s['name']}" for p in s["paths"] if p["health"] != "ok"]
    return {"sites": sites, "attention": attention}


def _site(conn, site_id: str, user) -> dict:
    scope = customer_scope(user)
    s = conn.execute(
        """SELECT s.*, n.id AS node_id, n.name AS node_name, n.last_seen, n.applied_version, n.apply_ok,
                  n.apply_error, n.agent_version,
                  (n.last_seen > now() - interval '30 seconds') AS online,
                  (SELECT max(version) FROM desired_states d WHERE d.node_id = n.id) AS desired_version
           FROM sites s LEFT JOIN nodes n ON n.site_id = s.id
           WHERE s.id = %(id)s AND (%(c)s::uuid IS NULL OR s.customer_id = %(c)s)""",
        {"id": site_id, "c": scope},
    ).fetchone()
    if s is None:
        raise HTTPException(404, "Site not found.")
    return s


@router.get("/sites")
def list_sites(user: ViewerDep) -> list[dict]:
    scope = customer_scope(user)
    with db.tx() as conn:
        return conn.execute(
            """SELECT id, customer_id, name, kind, location, timezone, asn, lan_prefixes::text[] AS lan_prefixes
               FROM sites WHERE %(c)s::uuid IS NULL OR customer_id = %(c)s ORDER BY kind DESC, name""",
            {"c": scope},
        ).fetchall()


@router.get("/sites/{site_id}")
def site_detail(site_id: str, user: ViewerDep) -> dict:
    with db.tx() as conn:
        s = _site(conn, site_id, user)
        s["lan_prefixes"] = [str(p) for p in s["lan_prefixes"]]
        sla = _voice_slas(conn).get(s["customer_id"])
        s["paths"] = [
            {**p, "health": _health(p if p["sent"] else None, sla)}
            for p in _site_paths(conn, [s["id"]]).get(s["id"], [])
        ]
        s["tunnels"] = (
            conn.execute(
                "SELECT tunnel, path, handshake_age_s, bfd, updated_at FROM tunnel_state"
                " WHERE node_id = %s ORDER BY tunnel",
                (s["node_id"],),
            ).fetchall()
            if s["node_id"]
            else []
        )
        s["slas"] = conn.execute(
            "SELECT class_name, max_latency_ms, max_jitter_ms, max_loss_pct, allow_satellite FROM sla_policies"
            " WHERE customer_id = %s ORDER BY class_name",
            (s["customer_id"],),
        ).fetchall()
    return s


@router.get("/sites/{site_id}/metrics")
def site_metrics(site_id: str, user: ViewerDep, minutes: int = Query(default=15, ge=1, le=24 * 60)) -> dict:
    with db.tx() as conn:
        s = _site(conn, site_id, user)
        if s["node_id"] is None:
            return {"site_id": site_id, "points": []}
        # Raw 10 s windows up to an hour; one-minute averages beyond that.
        if minutes <= 60:
            points = conn.execute(
                """SELECT time, path, rtt_avg_ms, jitter_ms, loss_pct FROM path_metrics
                   WHERE node_id = %s AND time > now() - make_interval(mins => %s) ORDER BY time""",
                (s["node_id"], minutes),
            ).fetchall()
        else:
            points = conn.execute(
                """SELECT date_trunc('minute', time) AS time, path, avg(rtt_avg_ms) AS rtt_avg_ms,
                          avg(jitter_ms) AS jitter_ms,
                          CASE WHEN sum(sent) > 0 THEN 100.0 * (sum(sent) - sum(received)) / sum(sent) END AS loss_pct
                   FROM path_metrics WHERE node_id = %s AND time > now() - make_interval(mins => %s)
                   GROUP BY 1, 2 ORDER BY 1""",
                (s["node_id"], minutes),
            ).fetchall()
    return {"site_id": site_id, "points": points}


@router.get("/events")
def list_events(
    user: ViewerDep, site_id: str | None = None, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    scope = customer_scope(user)
    with db.tx() as conn:
        return conn.execute(
            """SELECT e.time, e.kind, e.detail, n.name AS node
               FROM events e LEFT JOIN nodes n ON n.id = e.node_id
               WHERE (%(c)s::uuid IS NULL OR e.customer_id = %(c)s)
                 AND (%(s)s::uuid IS NULL OR n.site_id = %(s)s)
               ORDER BY e.time DESC LIMIT %(l)s""",
            {"c": scope, "s": site_id, "l": limit},
        ).fetchall()
