"""Read APIs for the portal: overview, site detail, path metrics, events."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import db, sla
from ..sla import MOVE as _MOVE
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


# The share of windows a class should meet its SLA in. Shown as the target on the portal.
SLA_TARGET_PCT = 99.5


def _sla_24h(conn, site_ids: list) -> list[dict]:
    """Per site and class: 10 s windows in the last 24 h and how many met the class SLA
    (see sla.windows, shared with the monthly SLA credits on invoices)."""
    return sla.windows(conn, site_ids)


def _pct(met: int, windows: int) -> float | None:
    return round(100.0 * met / windows, 2) if windows else None


@router.get("/overview")
def overview(user: ViewerDep, customer_id: uuid.UUID | None = None) -> dict:
    scope = customer_scope(user)
    if scope is None and customer_id:
        scope = customer_id  # an admin looking at one customer
    with db.tx() as conn:
        sites = conn.execute(
            f"""SELECT s.id, s.customer_id, s.name, s.kind, s.location, s.timezone, s.storm_mode,
                      n.id AS node_id, n.last_seen, n.applied_version, n.apply_ok,
                      (n.last_seen > now() - interval '{ONLINE_SECONDS} seconds') AS online,
                      (SELECT max(version) FROM desired_states d WHERE d.node_id = n.id) AS desired_version
               FROM sites s LEFT JOIN nodes n ON n.site_id = s.id
               WHERE %(c)s::uuid IS NULL OR s.customer_id = %(c)s
               ORDER BY s.kind DESC, s.name""",
            {"c": scope},
        ).fetchall()
        site_ids = [s["id"] for s in sites]
        paths = _site_paths(conn, site_ids)
        slas = _voice_slas(conn)
        classes = conn.execute(
            """SELECT c.name AS class_name, min(c.ordinal) AS ordinal,
                      bool_or(c.priority = 'bulk' OR c.name = 'bulk') AS best_effort,
                      min(sp.max_latency_ms)::float8 AS max_latency_ms, min(sp.max_jitter_ms)::float8 AS max_jitter_ms,
                      min(sp.max_loss_pct)::float8 AS max_loss_pct
               FROM app_classes c
               LEFT JOIN sla_policies sp ON sp.customer_id = c.customer_id AND sp.class_name = c.name
               WHERE %(c)s::uuid IS NULL OR c.customer_id = %(c)s
               GROUP BY c.name ORDER BY min(c.ordinal), c.name""",
            {"c": scope},
        ).fetchall()
        sla_rows = _sla_24h(conn, site_ids)
        steering = conn.execute(
            f"""SELECT s.id AS site_id, c.name AS class_name, st.path AS intended, ip.label AS intended_label,
                       st.since, a.path AS actual, ap.label AS actual_label, a.paused, a.failover,
                       (SELECT count(*) FROM decisions d WHERE d.site_id = s.id AND d.class_name = c.name
                          AND d.time > now() - interval '24 hours' AND {_MOVE})::int AS moves_24h
                FROM sites s
                JOIN app_classes c ON c.customer_id = s.customer_id
                LEFT JOIN steering st ON st.site_id = s.id AND st.class_name = c.name
                LEFT JOIN paths ip ON ip.name = st.path
                LEFT JOIN nodes n ON n.site_id = s.id
                LEFT JOIN steering_actual a ON a.node_id = n.id AND a.class_name = c.name AND a.dst = ''
                    AND a.updated_at > now() - interval '60 seconds'
                LEFT JOIN paths ap ON ap.name = a.path
                WHERE s.id = ANY(%s) AND s.kind = 'site'
                ORDER BY c.ordinal, c.name""",
            (site_ids,),
        ).fetchall()
        recent = conn.execute(
            f"""SELECT d.id, d.time, d.site_id, s.name AS site, d.class_name, d.kind,
                       d.from_path, fp.label AS from_label, d.to_path, tp.label AS to_label, d.reason
                FROM decisions d JOIN sites s ON s.id = d.site_id
                LEFT JOIN paths fp ON fp.name = d.from_path LEFT JOIN paths tp ON tp.name = d.to_path
                WHERE d.site_id = ANY(%s) AND {_MOVE}
                ORDER BY d.time DESC, d.id DESC LIMIT 5""",
            (site_ids,),
        ).fetchall()

    attention = []
    for s in sites:
        sla = slas.get(s["customer_id"])
        s["paths"] = [{**p, "health": _health(p if p["sent"] else None, sla)} for p in paths.get(s["id"], [])]
        s["steering"] = []
        if s["node_id"] is None:
            continue
        if not s["online"]:
            attention.append(f"{s['name']} is offline")
        elif s["kind"] == "site":
            # The PoP only reflects probes; paths are measured from the sites.
            attention += [f"{p['label']} at {s['name']}" for p in s["paths"] if p["health"] != "ok"]

    by_site = {s["id"]: s for s in sites}
    for r in steering:
        site_id = r.pop("site_id")
        if r["actual"] == r["intended"]:
            r["actual"] = r["actual_label"] = None  # only shown when the agent reports something else
        by_site[site_id]["steering"].append(r)

    per_class: dict[str, dict] = {
        c["class_name"]: {**c, "windows": 0, "met": 0, "pct": None, "sites": []} for c in classes
    }
    for r in sla_rows:
        c = per_class.get(r["class_name"])
        if c is None:
            continue
        c["windows"] += r["windows"]
        c["met"] += r["met"]
        c["sites"].append(
            {
                "site_id": r["site_id"],
                "site": by_site[r["site_id"]]["name"],
                "windows": r["windows"],
                "met": r["met"],
                "pct": _pct(r["met"], r["windows"]),
            }
        )
    for c in per_class.values():
        c["pct"] = _pct(c["met"], c["windows"])
        c["sites"].sort(key=lambda x: x["site"])
        c.pop("ordinal")

    return {
        "sites": sites,
        "attention": attention,
        "sla_24h": list(per_class.values()),
        "sla_target_pct": SLA_TARGET_PCT,
        "moves_24h": sum(r["moves_24h"] for r in steering),
        "recent_decisions": recent,
    }


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
        s["steering"] = steering_view(conn, s["id"]) if s["kind"] == "site" else []
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
                """SELECT time, path, rtt_avg_ms, jitter_ms, loss_pct, sent, received FROM path_metrics
                   WHERE node_id = %s AND time > now() - make_interval(mins => %s) ORDER BY time""",
                (s["node_id"], minutes),
            ).fetchall()
        else:
            points = conn.execute(
                """SELECT date_trunc('minute', time) AS time, path, avg(rtt_avg_ms) AS rtt_avg_ms,
                          avg(jitter_ms) AS jitter_ms,
                          CASE WHEN sum(sent) > 0 THEN 100.0 * (sum(sent) - sum(received)) / sum(sent) END AS loss_pct,
                          sum(sent)::int AS sent, sum(received)::int AS received
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


@router.get("/decisions")
def list_decisions(
    user: ViewerDep,
    site_id: str | None = None,
    class_name: str | None = None,
    include_holds: bool = True,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    """Routing decisions, newest first, each with its reason and inputs."""
    scope = customer_scope(user)
    with db.tx() as conn:
        return conn.execute(
            """SELECT d.id, d.time, d.customer_id, d.site_id, s.name AS site, d.class_name, d.kind,
                      d.from_path, fp.label AS from_label, d.to_path, tp.label AS to_label,
                      d.shadow, d.engine, d.reason, d.inputs
               FROM decisions d JOIN sites s ON s.id = d.site_id
               LEFT JOIN paths fp ON fp.name = d.from_path LEFT JOIN paths tp ON tp.name = d.to_path
               WHERE (%(c)s::uuid IS NULL OR d.customer_id = %(c)s)
                 AND (%(s)s::uuid IS NULL OR d.site_id = %(s)s)
                 AND (%(k)s::text IS NULL OR d.class_name = %(k)s)
                 AND (%(h)s OR d.kind <> 'hold')
               ORDER BY d.time DESC, d.id DESC LIMIT %(l)s""",
            {"c": scope, "s": site_id, "k": class_name, "h": include_holds, "l": limit},
        ).fetchall()


def steering_view(conn, site_id) -> list[dict]:
    """Per class: where the engine wants it, where the agent says it is, and why."""
    return conn.execute(
        """SELECT c.name AS class_name, st.path AS intended, ip.label AS intended_label, st.since,
                  a.path AS actual, ap.label AS actual_label, a.paused, a.failover, a.updated_at AS reported_at,
                  (SELECT d.reason FROM decisions d WHERE d.site_id = s.id AND d.class_name = c.name
                   ORDER BY d.time DESC, d.id DESC LIMIT 1) AS last_reason
           FROM sites s
           JOIN app_classes c ON c.customer_id = s.customer_id
           LEFT JOIN steering st ON st.site_id = s.id AND st.class_name = c.name
           LEFT JOIN paths ip ON ip.name = st.path
           LEFT JOIN nodes n ON n.site_id = s.id
           LEFT JOIN steering_actual a ON a.node_id = n.id AND a.class_name = c.name AND a.dst = ''
                    AND a.updated_at > now() - interval '60 seconds'
           LEFT JOIN paths ap ON ap.name = a.path
           WHERE s.id = %s ORDER BY c.ordinal, c.name""",
        (site_id,),
    ).fetchall()
