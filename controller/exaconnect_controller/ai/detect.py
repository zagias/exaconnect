"""Application detection (ADR 0007).

Agents report what leaves each site every minute, grouped by protocol,
destination and port, from the kernel's connection tracking. Every five
minutes this pass looks at the last half hour per site and labels what it
sees two ways:

1. Known applications from the catalogue (ai/apps.py), by port and the
   vendors' published address ranges. Confident (0.9).
2. Anything else with a clear behaviour: a steady stream of small UDP
   packets both ways is real-time media; a large one-way transfer of full
   packets is bulk. Less confident (0.6), and only on a fixed server port.

A detection suggests a class when the traffic is not already in it. The
customer applies it (which creates a traffic rule for that site) or
dismisses it. With auto-prioritise on, confident known applications are
applied without asking. Nothing here sees packet contents.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit, traffic
from ..ports import parse_ports
from . import apps

log = logging.getLogger("exaconnect.ai.detect")

WINDOW_S = 1800
MIN_PACKETS = 500
AUTO_CONFIDENCE = 0.8
EPHEMERAL_FROM = 32768  # client-side ports say nothing about the application
PRIORITY_WORD = {"realtime": "real-time", "interactive": "interactive", "bulk": "bulk", "normal": "normal"}
AUTO_ACTOR = "system:auto-prioritise"


def _profile(proto: str, s: dict) -> tuple[str, float, str] | None:
    """Behaviour profile of unrecognised traffic: (priority, confidence, why)."""
    pkts = s["pkts_out"] + s["pkts_in"]
    size = (s["bytes_out"] + s["bytes_in"]) / max(pkts, 1)
    pps = pkts / max(s["span_s"], 60) / max(s["flows"], 1)
    both_ways = min(s["pkts_out"], s["pkts_in"]) >= 0.3 * max(s["pkts_out"], s["pkts_in"])
    if proto == "udp" and size <= 300 and pps >= 20 and both_ways:
        why = f"a steady stream of small packets both ways (about {pps:.0f} a second of {size:.0f} bytes)"
        return "realtime", 0.6, why
    big = max(s["bytes_out"], s["bytes_in"])
    if size >= 900 and big >= 50e6 and big >= 10 * min(s["bytes_out"], s["bytes_in"]):
        return "bulk", 0.6, f"a large one-way transfer ({big / 1e6:.0f} MB of full-size packets)"
    return None


def _stats(rows: list[dict]) -> dict[str, Any]:
    out = {k: sum(r[k] for r in rows) for k in ("flows", "bytes_out", "bytes_in", "pkts_out", "pkts_in")}
    times = [r["time"] for r in rows]
    out["span_s"] = (max(times) - min(times)).total_seconds() + 60
    # Flows are reported per minute, so the busiest minute is the concurrent count.
    per_min: dict[Any, int] = defaultdict(int)
    for r in rows:
        per_min[r["time"]] += r["flows"]
    out["flows"] = max(per_min.values())
    classes: dict[str, int] = defaultdict(int)
    for r in rows:
        classes[r["class_name"]] += r["pkts_out"] + r["pkts_in"]
    out["current_class"] = max(classes, key=classes.get)
    out["destinations"] = sorted({str(r["dst"]) for r in rows})[:10]
    return out


def detect(conn: psycopg.Connection, customer: dict, now: dt.datetime) -> list[dict]:
    """Update this customer's detections from the last half hour. Returns the ones that changed."""
    cid = customer["id"]
    classes = {r["name"] for r in conn.execute("SELECT name FROM app_classes WHERE customer_id = %s", (cid,))}
    rows = conn.execute(
        """SELECT f.time, s.id AS site_id, s.name AS site, f.proto, f.dst, f.dport, f.class_name, f.flows,
                  f.bytes_out, f.bytes_in, f.pkts_out, f.pkts_in
           FROM flow_stats f JOIN nodes n ON n.id = f.node_id JOIN sites s ON s.id = n.site_id
           WHERE f.customer_id = %s AND s.kind = 'site' AND f.time > %s - make_interval(secs => %s)
             AND f.time <= %s""",
        (cid, now, WINDOW_S, now),
    ).fetchall()
    groups: dict[tuple, list[dict]] = defaultdict(list)
    meta: dict[tuple, dict] = {}
    for r in rows:
        app = apps.recognise(r["proto"], r["dport"], str(r["dst"]))
        if app is not None:
            key = (r["site_id"], f"app:{app.id}")
            meta[key] = {"app": app, "proto": r["proto"], "dport": r["dport"], "site": r["site"]}
        elif r["dport"] < EPHEMERAL_FROM:
            key = (r["site_id"], f"{r['proto']}:{r['dport']}")
            meta.setdefault(key, {"app": None, "proto": r["proto"], "dport": r["dport"], "site": r["site"]})
        else:
            continue
        groups[key].append(r)

    changed = []
    for key, grp in groups.items():
        site_id, k = key
        m = meta[key]
        s = _stats(grp)
        if s["pkts_out"] + s["pkts_in"] < MIN_PACKETS:
            continue
        app = m["app"]
        if app is not None:
            priority, confidence, label = app.priority, 0.9, app.name
            why = f"matches {app.name} ({app.category.lower()})"
        else:
            prof = _profile(m["proto"], s)
            if prof is None:
                continue
            priority, confidence, why = prof
            label = f"Unrecognised {m['proto'].upper()} {m['dport']}"
        suggested = apps.PRIORITY_CLASS.get(priority)
        if suggested not in classes:
            continue
        if s["current_class"] == suggested:
            # The traffic already lands in the class: an open suggestion is out of date.
            conn.execute(
                "DELETE FROM app_detections WHERE customer_id = %s AND site_id = %s AND key = %s"
                " AND status = 'suggested'",
                (cid, site_id, k),
            )
            continue
        now_in = f"currently in {s['current_class']}" if s["current_class"] else "currently unclassified"
        reason = (
            f"{label} seen at {m['site']}: {s['flows']} flow(s) to {m['proto'].upper()} {m['dport']}, {why}; "
            f"{now_in}. Suggest {suggested} ({PRIORITY_WORD[priority]} priority)."
        )
        stats = {k2: v for k2, v in s.items() if k2 != "current_class"}
        row = conn.execute(
            """INSERT INTO app_detections (customer_id, site_id, key, app_id, label, proto, dport, current_class,
                                           suggested_class, profile, confidence, reason, stats, first_seen, last_seen)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, site_id, key) DO UPDATE SET label = EXCLUDED.label,
                 current_class = EXCLUDED.current_class, suggested_class = EXCLUDED.suggested_class,
                 profile = EXCLUDED.profile, confidence = EXCLUDED.confidence, reason = EXCLUDED.reason,
                 stats = EXCLUDED.stats, last_seen = EXCLUDED.last_seen
               RETURNING *""",
            (
                cid,
                site_id,
                k,
                app.id if app else None,
                label,
                m["proto"],
                m["dport"],
                s["current_class"],
                suggested,
                priority,
                confidence,
                reason,
                Jsonb(stats),
                now,
                now,
            ),
        ).fetchone()
        changed.append(row)
        if customer["auto_prioritise"] and row["status"] == "suggested" and app and confidence >= AUTO_CONFIDENCE:
            try:
                with conn.transaction():
                    apply(conn, row, AUTO_ACTOR)
            except traffic.RuleError as e:
                log.warning("auto-prioritise %s: %s", label, e)
    reconcile(conn, cid)
    return changed


def covering_rule(rules: list[dict], det: dict) -> dict | None:
    """The first enabled rule that already decides this detection's traffic at
    its site: one naming the application, or one whose own ports cover it with
    nothing else narrowing the match. The customer has chosen a class for it,
    so there is nothing left to suggest."""
    for r in rules:
        if not r["enabled"] or not traffic.applies_to(r, det["site_id"]):
            continue
        if det["app_id"] and det["app_id"] in (r["apps"] or []):
            return r
        narrowed = r["dst_subnets"] or r["src_subnets"] or r["vlans"] or r["domains"] or r["dscp"]
        if r["ports"] and not narrowed:
            try:
                ranges = parse_ports(r["ports"])
            except ValueError:
                continue
            if any(p["proto"] == det["proto"] and p["from"] <= det["dport"] <= p["to"] for p in ranges):
                return r
    return None


def reconcile(conn: psycopg.Connection, customer_id: Any) -> None:
    """Keep detections in step with the customer's rules: a suggestion that a
    rule already covers is closed as applied by that rule, and an applied
    detection whose rule no longer covers it (disabled or edited) is
    suggested again unless another rule covers it."""
    rules = traffic.load_rules(conn, customer_id)
    dets = conn.execute(
        """SELECT id, site_id, app_id, proto, dport, status, rule_id FROM app_detections
           WHERE customer_id = %s AND (status = 'suggested' OR (status = 'applied' AND rule_id IS NOT NULL))""",
        (customer_id,),
    ).fetchall()
    for d in dets:
        r = covering_rule(rules, d)
        if r is not None and (d["status"] != "applied" or d["rule_id"] != r["id"]):
            conn.execute("UPDATE app_detections SET status = 'applied', rule_id = %s WHERE id = %s", (r["id"], d["id"]))
        elif r is None and d["status"] == "applied":
            conn.execute("UPDATE app_detections SET status = 'suggested', rule_id = NULL WHERE id = %s", (d["id"],))


def apply(conn: psycopg.Connection, det: dict, actor: str, class_name: str | None = None) -> int:
    """Turn a detection into a traffic rule for its site. Returns the rule id."""
    site = conn.execute("SELECT name FROM sites WHERE id = %s", (det["site_id"],)).fetchone()
    rule: dict[str, Any] = {
        "name": f"{det['label']} at {site['name']}",
        "class_name": class_name or det["suggested_class"],
        "site_ids": [det["site_id"]],
    }
    if det["app_id"]:
        rule["apps"] = [det["app_id"]]
    else:
        rule["ports"] = f"{det['proto']}:{det['dport']}"
    rule_id = traffic.save_rule(conn, det["customer_id"], rule, actor, source="detected")
    conn.execute("UPDATE app_detections SET status = 'applied', rule_id = %s WHERE id = %s", (rule_id, det["id"]))
    audit.record(conn, actor, "application.apply", det["label"], det["customer_id"], {"detection": det["id"]})
    return rule_id


def dismiss(conn: psycopg.Connection, det: dict, actor: str) -> None:
    conn.execute("UPDATE app_detections SET status = 'dismissed' WHERE id = %s", (det["id"],))
    audit.record(conn, actor, "application.dismiss", det["label"], det["customer_id"], {"detection": det["id"]})


def run_once(conn: psycopg.Connection, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    n = 0
    for c in conn.execute("SELECT id, auto_prioritise FROM customers ORDER BY name").fetchall():
        n += len(detect(conn, c, now))
    # Flow detail is only needed for the detection window; keep a day for the portal.
    conn.execute("DELETE FROM flow_stats WHERE time < %s - interval '1 day'", (now,))
    return n
