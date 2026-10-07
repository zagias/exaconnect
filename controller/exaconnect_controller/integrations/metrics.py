"""Connect's metrics, for the Prometheus/OpenMetrics endpoint and OTLP export.

One collection, two renderings. Every series is labelled with the
organisation, so an API key only ever sees its own (a carrier key: only
its own links).
"""

from __future__ import annotations

import math
from typing import Any

import psycopg

from ..routing.engine import score

ONLINE_S = 30
FRESH_S = 30


def _fam(name: str, help_: str, unit: str = "", otel: str = "", kind: str = "gauge") -> dict:
    return {"name": name, "help": help_, "unit": unit, "otel": otel or name, "type": kind, "samples": []}


def collect(conn: psycopg.Connection, customer_id: Any = None, carrier_id: Any = None) -> list[dict]:
    """customer_id/carrier_id None means no limit (an admin)."""
    lat = _fam("exacarib_path_latency_ms", "Round-trip latency over the last 30 s.", "ms", "exacarib.path.latency")
    jit = _fam("exacarib_path_jitter_ms", "Jitter (RFC 3550) over the last 30 s.", "ms", "exacarib.path.jitter")
    loss = _fam("exacarib_path_loss_percent", "Probe loss over the last 30 s.", "%", "exacarib.path.loss")
    up = _fam("exacarib_path_up", "1 when BFD reports the path's tunnel up.", "", "exacarib.path.up")
    lin = _fam("exacarib_link_in_mbps", "Average inbound Mbps in the latest 5-minute sample.", "Mbit/s", "exacarib.link.in")
    lout = _fam(
        "exacarib_link_out_mbps", "Average outbound Mbps in the latest 5-minute sample.", "Mbit/s", "exacarib.link.out"
    )
    commit = _fam("exacarib_link_commit_mbps", "The link's committed rate.", "Mbit/s", "exacarib.link.commit")
    sla = _fam(
        "exacarib_sla_score",
        "How comfortably a class meets its SLA on its current path (1 comfortable, 0 breaching).",
        "1",
        "exacarib.sla.score",
    )
    node_up = _fam("exacarib_node_up", "1 when the edge agent reported in the last 30 s.", "", "exacarib.node.up")
    node_seen = _fam(
        "exacarib_node_last_seen_seconds", "Seconds since the edge agent last reported.", "s", "exacarib.node.last_seen"
    )
    node_ok = _fam(
        "exacarib_node_config_ok", "1 when the agent applied its latest desired state.", "", "exacarib.node.config_ok"
    )
    storm = _fam("exacarib_storm_mode", "1 while Storm Mode is on for the site.", "", "exacarib.site.storm_mode")

    paths = conn.execute(
        """SELECT cu.name AS organisation, s.id AS site_id, s.name AS site, l.id AS link_id, l.path,
                  c.name AS carrier, l.commit_mbps, l.underlay_type, ts.bfd,
                  m.rtt, m.jitter, m.loss
           FROM links l JOIN sites s ON s.id = l.site_id JOIN customers cu ON cu.id = l.customer_id
           JOIN carriers c ON c.id = l.carrier_id JOIN paths p ON p.name = l.path
           LEFT JOIN nodes n ON n.site_id = s.id
           LEFT JOIN tunnel_state ts ON ts.node_id = n.id AND ts.tunnel = p.tunnel
                AND ts.updated_at > now() - interval '60 seconds'
           LEFT JOIN LATERAL (
             SELECT avg(rtt_avg_ms) AS rtt, avg(jitter_ms) AS jitter,
                    CASE WHEN sum(sent) > 0 THEN 100.0 * (sum(sent) - sum(received)) / sum(sent) END AS loss
             FROM path_metrics pm WHERE pm.node_id = n.id AND pm.path = l.path
               AND pm.time > now() - make_interval(secs => %(fresh)s)) m ON true
           WHERE (%(c)s::uuid IS NULL OR l.customer_id = %(c)s) AND (%(k)s::uuid IS NULL OR l.carrier_id = %(k)s)
           ORDER BY cu.name, s.name, p.ordinal""",
        {"c": customer_id, "k": carrier_id, "fresh": FRESH_S},
    ).fetchall()
    usage = {
        r["link_id"]: r
        for r in conn.execute(
            """SELECT DISTINCT ON (link_id) link_id, in_mbps, out_mbps FROM usage_5m
               WHERE link_id = ANY(%s) AND bucket > now() - interval '1 hour' ORDER BY link_id, bucket DESC""",
            ([p["link_id"] for p in paths],),
        )
    }
    for p in paths:
        labels = {"organisation": p["organisation"], "site": p["site"], "path": p["path"], "carrier": p["carrier"]}
        if p["rtt"] is not None:
            lat["samples"].append((labels, float(p["rtt"])))
        if p["jitter"] is not None:
            jit["samples"].append((labels, float(p["jitter"])))
        if p["loss"] is not None:
            loss["samples"].append((labels, float(p["loss"])))
        if p["bfd"] is not None:
            up["samples"].append((labels, 1.0 if str(p["bfd"]).lower() == "up" else 0.0))
        commit["samples"].append((labels, float(p["commit_mbps"] or 0)))
        u = usage.get(p["link_id"])
        if u:
            lin["samples"].append((labels, float(u["in_mbps"])))
            lout["samples"].append((labels, float(u["out_mbps"])))
    out = [lat, jit, loss, up, lin, lout, commit]
    if carrier_id is not None:
        return out  # a carrier sees its links, not the customer's classes or nodes

    by_path = {(p["site_id"], p["path"]): p for p in paths}
    for r in conn.execute(
        """SELECT cu.name AS organisation, s.id AS site_id, s.name AS site, st.class_name, st.path,
                  sp.max_latency_ms, sp.max_jitter_ms, sp.max_loss_pct
           FROM steering st JOIN sites s ON s.id = st.site_id JOIN customers cu ON cu.id = st.customer_id
           LEFT JOIN sla_policies sp ON sp.customer_id = st.customer_id AND sp.class_name = st.class_name
           WHERE %(c)s::uuid IS NULL OR st.customer_id = %(c)s ORDER BY cu.name, s.name, st.class_name""",
        {"c": customer_id},
    ):
        m = by_path.get((r["site_id"], r["path"]))
        if m is None or (m["rtt"] is None and m["loss"] is None):
            continue
        scores = [
            score(float(v), float(lim))
            for v, lim in ((m["rtt"], r["max_latency_ms"]), (m["jitter"], r["max_jitter_ms"]), (m["loss"], r["max_loss_pct"]))
            if v is not None and lim is not None
        ]
        sla["samples"].append(
            (
                {"organisation": r["organisation"], "site": r["site"], "class": r["class_name"], "path": r["path"]},
                min(scores) if scores else 1.0,
            )
        )
    for n in conn.execute(
        """SELECT cu.name AS organisation, s.name AS site, n.name AS node, s.storm_mode, s.kind,
                  extract(epoch FROM now() - n.last_seen) AS age, n.apply_ok,
                  n.applied_version = (SELECT max(version) FROM desired_states d WHERE d.node_id = n.id) AS current
           FROM sites s JOIN customers cu ON cu.id = s.customer_id LEFT JOIN nodes n ON n.site_id = s.id
           WHERE %(c)s::uuid IS NULL OR s.customer_id = %(c)s ORDER BY cu.name, s.name""",
        {"c": customer_id},
    ):
        labels = {"organisation": n["organisation"], "site": n["site"]}
        if n["kind"] == "site":
            storm["samples"].append((labels, 1.0 if n["storm_mode"] else 0.0))
        if n["node"] is None:
            continue
        nl = {**labels, "node": n["node"]}
        age = float(n["age"]) if n["age"] is not None else math.inf
        node_up["samples"].append((nl, 1.0 if age <= ONLINE_S else 0.0))
        if n["age"] is not None:
            node_seen["samples"].append((nl, round(age, 1)))
        node_ok["samples"].append((nl, 1.0 if n["apply_ok"] and n["current"] else 0.0))
    return out + [sla, node_up, node_seen, node_ok, storm]


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(v: float) -> str:
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    if math.isnan(v):
        return "NaN"
    return repr(round(v, 6)) if not float(v).is_integer() else f"{v:.1f}"


def render(families: list[dict], openmetrics: bool = False) -> str:
    """Prometheus text format 0.0.4, or OpenMetrics 1.0 (with # EOF)."""
    lines: list[str] = []
    for f in families:
        lines.append(f"# HELP {f['name']} {_esc(f['help'])}")
        lines.append(f"# TYPE {f['name']} {f['type']}")
        if openmetrics and f.get("unit") and f["name"].endswith("_" + _om_unit(f["unit"])):
            lines.append(f"# UNIT {f['name']} {_om_unit(f['unit'])}")
        for labels, value in f["samples"]:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            lines.append(f"{f['name']}{{{lab}}} {_num(float(value))}")
    if openmetrics:
        lines.append("# EOF")
    return "\n".join(lines) + "\n"


def _om_unit(unit: str) -> str:
    return {"ms": "ms", "s": "seconds", "%": "percent", "Mbit/s": "mbps"}.get(unit, "")
