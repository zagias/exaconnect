"""RESTCONF (RFC 8040), read-only, over the exacarib-connect YANG module.

Root: /api/v1/restconf (discovered at /.well-known/host-meta). Data:
/api/v1/restconf/data/exacarib-connect:connect, and subtrees by key:
.../connect/site=kingston, .../site=kingston/link=carrier-a,
.../site=kingston/class=voice. JSON only (application/yang-data+json).

NETCONF (RFC 6241) and gNMI are not served: the same module and the same
`tree()` would back them; see docs/integrations.md.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from importlib import resources
from typing import Any

import psycopg

from ..routing.engine import score
from . import notices

MODULE = "exacarib-connect"
REVISION = "2026-10-07"
MEDIA = "application/yang-data+json"


def yang_text() -> str:
    return resources.files(__package__).joinpath("yang", f"{MODULE}.yang").read_text()


def _iso(t: Any) -> str | None:
    if t is None:
        return None
    return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _dec(v: Any, digits: int = 2) -> str | None:
    # RFC 7951: decimal64 is a JSON string.
    return None if v is None else f"{float(v):.{digits}f}"


def tree(conn: psycopg.Connection, customer_id: Any = None) -> dict:
    now = dt.datetime.now(dt.UTC)
    sites = conn.execute(
        """SELECT s.*, cu.name AS organisation, n.name AS node, n.last_seen, n.applied_version, n.apply_ok
           FROM sites s JOIN customers cu ON cu.id = s.customer_id LEFT JOIN nodes n ON n.site_id = s.id
           WHERE %(c)s::uuid IS NULL OR s.customer_id = %(c)s ORDER BY cu.name, s.kind DESC, s.name""",
        {"c": customer_id},
    ).fetchall()
    links = conn.execute(
        """SELECT l.*, c.name AS carrier, ts.bfd, m.rtt, m.jitter, m.loss
           FROM links l JOIN carriers c ON c.id = l.carrier_id JOIN paths p ON p.name = l.path
           LEFT JOIN nodes n ON n.site_id = l.site_id
           LEFT JOIN tunnel_state ts ON ts.node_id = n.id AND ts.tunnel = p.tunnel
           LEFT JOIN LATERAL (
             SELECT avg(rtt_avg_ms) AS rtt, avg(jitter_ms) AS jitter,
                    CASE WHEN sum(sent) > 0 THEN 100.0 * (sum(sent) - sum(received)) / sum(sent) END AS loss
             FROM path_metrics pm WHERE pm.node_id = n.id AND pm.path = l.path
               AND pm.time > now() - interval '30 seconds') m ON true
           WHERE %(c)s::uuid IS NULL OR l.customer_id = %(c)s ORDER BY p.ordinal""",
        {"c": customer_id},
    ).fetchall()
    maint = notices.under_maintenance(conn, [lk["id"] for lk in links], now)
    classes = conn.execute(
        """SELECT s.id AS site_id, ac.name AS class_name, st.path, st.since, sp.max_latency_ms, sp.max_jitter_ms,
                  sp.max_loss_pct, coalesce(sp.allow_satellite, true) AS allow_satellite,
                  (SELECT d.reason FROM decisions d WHERE d.site_id = s.id AND d.class_name = ac.name
                   ORDER BY d.time DESC, d.id DESC LIMIT 1) AS last_reason
           FROM sites s JOIN app_classes ac ON ac.customer_id = s.customer_id
           LEFT JOIN steering st ON st.site_id = s.id AND st.class_name = ac.name
           LEFT JOIN sla_policies sp ON sp.customer_id = s.customer_id AND sp.class_name = ac.name
           WHERE s.kind = 'site' AND (%(c)s::uuid IS NULL OR s.customer_id = %(c)s)
           ORDER BY ac.ordinal, ac.name""",
        {"c": customer_id},
    ).fetchall()
    by_site_path = {(lk["site_id"], lk["path"]): lk for lk in links}
    out = []
    for s in sites:
        site: dict[str, Any] = {
            "name": s["name"],
            "id": str(s["id"]),
            "organisation": s["organisation"],
            "kind": s["kind"],
            "location": s["location"],
            "storm-mode": bool(s["storm_mode"]),
            "lan-prefix": [str(p) for p in s["lan_prefixes"]],
        }
        if s["node"]:
            site["node"] = {
                "name": s["node"],
                "online": bool(s["last_seen"] and (now - s["last_seen"]).total_seconds() <= 30),
                "last-seen": _iso(s["last_seen"]),
                "applied-version": str(s["applied_version"]),  # uint64 is a string in RFC 7951
                "config-ok": bool(s["apply_ok"]),
            }
        site["link"] = [
            {
                "path": lk["path"],
                "id": str(lk["id"]),
                "carrier": lk["carrier"],
                "underlay-type": lk["underlay_type"],
                "commit-mbps": _dec(lk["commit_mbps"], 3),
                "state": {
                    k: v
                    for k, v in {
                        "bfd": (str(lk["bfd"]).lower() if lk["bfd"] in ("up", "down", "Up", "Down") else "unknown"),
                        "latency-ms": _dec(lk["rtt"]),
                        "jitter-ms": _dec(lk["jitter"]),
                        "loss-percent": _dec(lk["loss"], 3),
                        "under-maintenance": str(lk["id"]) in maint,
                    }.items()
                    if v is not None
                },
            }
            for lk in links
            if lk["site_id"] == s["id"]
        ]
        cls_out = []
        for c in classes:
            if c["site_id"] != s["id"]:
                continue
            m = by_site_path.get((s["id"], c["path"])) if c["path"] else None
            parts = []
            if m is not None:
                parts = [
                    score(float(v), float(lim))
                    for v, lim in (
                        (m["rtt"], c["max_latency_ms"]),
                        (m["jitter"], c["max_jitter_ms"]),
                        (m["loss"], c["max_loss_pct"]),
                    )
                    if v is not None and lim is not None
                ]
            entry: dict[str, Any] = {"name": c["class_name"]}
            if c["path"]:  # no path yet until the first routing pass
                entry["current-path"] = c["path"]
                entry["since"] = _iso(c["since"])
            entry |= {
                "sla": {
                    k: v
                    for k, v in {
                        "max-latency-ms": _dec(c["max_latency_ms"]),
                        "max-jitter-ms": _dec(c["max_jitter_ms"]),
                        "max-loss-percent": _dec(c["max_loss_pct"], 3),
                        "allow-satellite": bool(c["allow_satellite"]),
                    }.items()
                    if v is not None
                },
            }
            if parts:
                entry["sla-score"] = _dec(min(parts), 3)
            if c["last_reason"]:
                entry["last-reason"] = c["last_reason"]
            cls_out.append(entry)
        site["class"] = cls_out
        out.append(site)
    return {f"{MODULE}:connect": {"site": out}}


class NotFound(LookupError):
    pass


def select(doc: dict, path: str) -> dict:
    """Walk an RFC 8040 data resource path: connect, connect/site=x, connect/site=x/link=y..."""
    segs = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]
    if not segs or segs[0] != f"{MODULE}:connect":
        raise NotFound(path)
    node: Any = doc[f"{MODULE}:connect"]
    if len(segs) == 1:
        return doc
    keys = {"site": "name", "link": "path", "class": "name"}
    for seg in segs[1:]:
        name, _, key = seg.partition("=")
        name = name.split(":")[-1]
        if name in keys and key:
            match = [x for x in node.get(name, []) if x.get(keys[name]) == key]
            if not match:
                raise NotFound(seg)
            node, last = match[0], f"{MODULE}:{name}"
            result = {last: [node]}
        elif isinstance(node, dict) and name in node:
            node, last = node[name], f"{MODULE}:{name}"
            result = {last: node}
        else:
            raise NotFound(seg)
    return result


def library() -> dict:
    return {
        "ietf-yang-library:yang-library": {
            "module-set": [
                {
                    "name": "connect",
                    "module": [
                        {
                            "name": MODULE,
                            "revision": REVISION,
                            "namespace": "urn:exacarib:yang:exacarib-connect",
                            "location": ["/api/v1/restconf/modules/exacarib-connect.yang"],
                        }
                    ],
                    "import-only-module": [
                        {"name": "ietf-yang-types", "revision": "2013-07-15"},
                        {"name": "ietf-inet-types", "revision": "2013-07-15"},
                    ],
                }
            ],
            "content-id": f"{MODULE}@{REVISION}",
        }
    }
