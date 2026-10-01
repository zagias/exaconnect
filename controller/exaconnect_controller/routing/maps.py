"""Steering maps: what each agent is told about classes and paths.

A site gets one rule per class with an ordered list of paths: the engine's
choice first, then the backups. The PoP gets one rule per class and site LAN
with that site's list, so return traffic follows the same path. Maps are
versioned per node like desired state (docs/desired-state.md).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

TABLE_BASE = 100
MARK_BASE = 0x100
SATELLITE_TYPES = ("leo", "geo")


def parse_ports(spec: str) -> list[dict[str, Any]]:
    """'udp:5060,10000-20000 tcp:443' or 'udp:5060,tcp:443' -> port ranges."""
    out: list[dict[str, Any]] = []
    proto = None
    for tok in spec.replace(" ", ",").split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if ":" in tok:
            proto, tok = tok.split(":", 1)
        if proto not in ("tcp", "udp"):
            raise ValueError(f"port '{tok}' needs a protocol, like udp:5060")
        lo, _, hi = tok.partition("-")
        a, b = int(lo), int(hi or lo)
        if not 1 <= a <= b <= 65535:
            raise ValueError(f"bad port range {tok}")
        out.append({"proto": proto, "from": a, "to": b})
    return out


def class_allows_sat(cls: dict, customer: dict) -> bool:
    """Satellite is a candidate only in Storm Mode, and bulk-like classes
    (allow_satellite false) only if the admin allowed it (CLAUDE.md §4.4)."""
    if not customer["storm_mode"]:
        return False
    return bool(cls["allow_satellite"]) or bool(customer["storm_allow_bulk_sat"])


def candidates(cls: dict, customer: dict, site_paths: list[dict]) -> list[str]:
    out = []
    for p in sorted(site_paths, key=lambda p: (p["satellite"], p["ordinal"])):
        if p["satellite"] and not class_allows_sat(cls, customer):
            continue
        out.append(p["name"])
    return out


def pause_if_none(cls: dict, customer: dict) -> bool:
    """A class that may never use satellite pauses when only satellite is
    left, instead of falling back to BGP (which would put it there)."""
    return not cls["allow_satellite"] and not (customer["storm_mode"] and customer["storm_allow_bulk_sat"])


def load(conn: psycopg.Connection, customer_id: Any) -> dict[str, Any]:
    customer = conn.execute("SELECT * FROM customers WHERE id = %s", (customer_id,)).fetchone()
    sites = conn.execute(
        "SELECT s.*, n.id AS node_id FROM sites s LEFT JOIN nodes n ON n.site_id = s.id"
        " WHERE s.customer_id = %s ORDER BY s.overlay_host",
        (customer_id,),
    ).fetchall()
    links = conn.execute(
        """SELECT l.site_id, l.path AS name, p.label, p.tunnel, p.ordinal, l.underlay_type, l.commit_mbps,
                  c.name AS carrier, (l.underlay_type = ANY(%s)) AS satellite
           FROM links l JOIN paths p ON p.name = l.path JOIN carriers c ON c.id = l.carrier_id
           WHERE l.customer_id = %s ORDER BY p.ordinal""",
        (list(SATELLITE_TYPES), customer_id),
    ).fetchall()
    classes = conn.execute(
        """SELECT c.name, c.dscp, c.ports, c.subnets::text[] AS subnets, c.ordinal,
                  coalesce(s.allow_satellite, true) AS allow_satellite,
                  s.max_latency_ms, s.max_jitter_ms, s.max_loss_pct
           FROM app_classes c LEFT JOIN sla_policies s
             ON s.customer_id = c.customer_id AND s.class_name = c.name
           WHERE c.customer_id = %s ORDER BY c.ordinal, c.name""",
        (customer_id,),
    ).fetchall()
    steering = conn.execute(
        "SELECT site_id, class_name, path FROM steering WHERE customer_id = %s", (customer_id,)
    ).fetchall()
    by_site: dict[Any, list[dict]] = {}
    for link in links:
        by_site.setdefault(link["site_id"], []).append(link)
    return {
        "customer": customer,
        "sites": sites,
        "links": by_site,
        "classes": classes,
        "steering": {(r["site_id"], r["class_name"]): r["path"] for r in steering},
    }


def site_rules(inv: dict[str, Any], site: dict) -> list[dict[str, Any]]:
    """The ordered path list per class for one site."""
    customer = inv["customer"]
    rules = []
    for cls in inv["classes"]:
        cands = candidates(cls, customer, inv["links"].get(site["id"], []))
        chosen = None if customer["shadow_mode"] else inv["steering"].get((site["id"], cls["name"]))
        order = ([chosen] if chosen in cands else []) + [c for c in cands if c != chosen]
        rules.append({"class": cls["name"], "paths": order, "pause_if_none": pause_if_none(cls, customer)})
    return rules


def build(inv: dict[str, Any], site: dict) -> dict[str, Any]:
    paths = [
        {"name": p["name"], "tunnel": p["tunnel"], "table": TABLE_BASE + p["ordinal"]}
        for p in inv["links"].get(site["id"], [])
    ]
    classes = []
    for i, c in enumerate(inv["classes"]):
        classes.append(
            {
                "name": c["name"],
                "mark": MARK_BASE + i + 1,
                "dscp": sorted(set(c["dscp"] or [])),
                "ports": parse_ports(c["ports"] or ""),
                "subnets": [str(s) for s in (c["subnets"] or [])],
            }
        )
    if site["kind"] == "pop":
        rules = []
        for other in inv["sites"]:
            if other["kind"] != "site" or other["node_id"] is None:
                continue
            for r in site_rules(inv, other):
                for prefix in other["lan_prefixes"]:
                    rules.append({**r, "dst": str(prefix)})
    else:
        rules = site_rules(inv, site)
    return {
        "storm": bool(inv["customer"]["storm_mode"]),
        "paths": paths,
        "classes": classes,
        "rules": rules,
        "local_prefixes": [str(p) for p in site["lan_prefixes"]],
    }


def _hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def refresh(conn: psycopg.Connection, customer_id: Any) -> dict[str, int]:
    """Rebuild every enrolled node's steering map; store a new version where it changed."""
    inv = load(conn, customer_id)
    if inv["customer"] is None:
        return {}
    out: dict[str, int] = {}
    for site in inv["sites"]:
        if site["node_id"] is None:
            continue
        body = build(inv, site)
        h = _hash(body)
        latest = conn.execute(
            "SELECT version, body_hash FROM steering_maps WHERE node_id = %s ORDER BY version DESC LIMIT 1",
            (site["node_id"],),
        ).fetchone()
        version = latest["version"] if latest else 0
        if latest is None or latest["body_hash"] != h:
            version += 1
            conn.execute(
                "INSERT INTO steering_maps (node_id, version, body, body_hash) VALUES (%s, %s, %s, %s)",
                (site["node_id"], version, Jsonb(body), h),
            )
        out[site["name"]] = version
    return out


def latest(conn: psycopg.Connection, node_id: Any) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT version, body FROM steering_maps WHERE node_id = %s ORDER BY version DESC LIMIT 1", (node_id,)
    ).fetchone()
    if row is None:
        return None
    return {**row["body"], "version": row["version"]}
