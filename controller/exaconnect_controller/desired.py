"""Builds each node's desired state from the inventory and versions it.

A new version is stored only when the rendered body changes, so agents polling
with ?have=<version> get 204 until something real changes. See
docs/desired-state.md for the format (it mirrors agent/internal/desired)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .routing import maps

SCHEMA = 1
PROBE_PORT = 7000
# Probe rate per underlay type (ADR 0005): a 1% loss SLA needs thousands of
# probes per SLA window, so terrestrial paths are probed 20 times a second
# (about 35 kbit/s with tunnel overhead). Metered LTE is probed less, and
# satellite once a second.
PROBE_INTERVAL_MS = {"fibre": 50, "broadband": 50, "lte": 200, "leo": 1000, "geo": 1000}
# Storm Mode keeps the satellite warm: probed 5 times a second (ADR 0004).
STORM_SAT_PROBE_MS = 200
KEEPALIVE_S = 10
BFD_PROFILES = [
    # Detects a dead terrestrial path in 3 x 200 ms = 600 ms (demo step 3 needs < 1 s).
    {"name": "terrestrial", "tx_ms": 200, "rx_ms": 200, "multiplier": 3},
    # GEO satellite RTT is ~600 ms, so its BFD must be slower to avoid false alarms.
    {"name": "satellite", "tx_ms": 1000, "rx_ms": 1000, "multiplier": 3},
]


def _host(cidr: Any, host: int) -> ipaddress.IPv4Address:
    net = ipaddress.ip_network(str(cidr))
    return net.network_address + host


def probe_interval(underlay_type: str, storm: bool) -> int:
    ms = PROBE_INTERVAL_MS.get(underlay_type, 1000)
    if storm and underlay_type in ("leo", "geo"):
        ms = min(ms, STORM_SAT_PROBE_MS)
    return ms


def _load(conn: psycopg.Connection, customer_id: Any) -> dict[str, Any]:
    sites = conn.execute("SELECT * FROM sites WHERE customer_id = %s ORDER BY overlay_host", (customer_id,)).fetchall()
    links = conn.execute(
        "SELECT l.*, p.tunnel, p.overlay_cidr, p.pop_port, p.bfd_profile, p.local_pref, p.ordinal"
        " FROM links l JOIN paths p ON p.name = l.path WHERE l.customer_id = %s ORDER BY p.ordinal",
        (customer_id,),
    ).fetchall()
    nodes = conn.execute("SELECT * FROM nodes WHERE customer_id = %s", (customer_id,)).fetchall()
    customer = conn.execute("SELECT storm_mode FROM customers WHERE id = %s", (customer_id,)).fetchone()
    return {
        "storm": bool(customer and customer["storm_mode"]),
        "sites": sites,
        "links": links,
        "nodes": {n["site_id"]: n for n in nodes},
    }


def build(inv: dict[str, Any], site: dict[str, Any]) -> dict[str, Any]:
    """The desired-state body (without version) for the node at `site`."""
    node = inv["nodes"][site["id"]]
    pop = next((s for s in inv["sites"] if s["kind"] == "pop"), None)
    links_by_site: dict[Any, list[dict]] = {}
    for link in inv["links"]:
        links_by_site.setdefault(link["site_id"], []).append(link)

    tunnels = []
    router_id = None
    for link in links_by_site.get(site["id"], []):
        addr = _host(link["overlay_cidr"], site["overlay_host"])
        prefixlen = ipaddress.ip_network(str(link["overlay_cidr"])).prefixlen
        router_id = router_id or str(addr)
        t: dict[str, Any] = {
            "name": link["tunnel"],
            "path": link["path"],
            "underlay_interface": link["underlay_interface"],
            "address": f"{addr}/{prefixlen}",
            "peers": [],
            "bgp_neighbors": [],
        }
        if site["kind"] == "pop":
            t["listen_port"] = link["pop_port"]
            for other in inv["sites"]:
                if other["kind"] != "site" or other["id"] not in inv["nodes"]:
                    continue
                if not any(o["path"] == link["path"] for o in links_by_site.get(other["id"], [])):
                    continue
                o_addr = _host(link["overlay_cidr"], other["overlay_host"])
                t["peers"].append(
                    {
                        "name": other["name"],
                        "public_key": inv["nodes"][other["id"]]["wg_public_key"],
                        "allowed_ips": [f"{o_addr}/32", *[str(p) for p in other["lan_prefixes"]]],
                    }
                )
                # Same preference as the sites use, so return traffic takes the same path.
                t["bgp_neighbors"].append(
                    {
                        "address": str(o_addr),
                        "asn": other["asn"],
                        "bfd_profile": link["bfd_profile"],
                        "local_pref": link["local_pref"],
                    }
                )
        elif pop is not None:
            pop_link = next((pl for pl in links_by_site.get(pop["id"], []) if pl["path"] == link["path"]), None)
            pop_node = inv["nodes"].get(pop["id"])
            pop_addr = _host(link["overlay_cidr"], pop["overlay_host"])
            if pop_link is not None and pop_node is not None and pop_link["underlay_ip"] is not None:
                t["peers"].append(
                    {
                        "name": pop["name"],
                        "public_key": pop_node["wg_public_key"],
                        "endpoint": f"{ipaddress.ip_interface(str(pop_link['underlay_ip'])).ip}:{link['pop_port']}",
                        "allowed_ips": ["0.0.0.0/0"],
                        "keepalive": KEEPALIVE_S,
                    }
                )
                t["bgp_neighbors"].append(
                    {
                        "address": str(pop_addr),
                        "asn": pop["asn"],
                        "bfd_profile": link["bfd_profile"],
                        "local_pref": link["local_pref"],
                    }
                )
                t["probe"] = {
                    "target": f"{pop_addr}:{PROBE_PORT}",
                    "interval_ms": probe_interval(link["underlay_type"], inv.get("storm", False)),
                }
        tunnels.append(t)

    body: dict[str, Any] = {
        "schema": SCHEMA,
        "node_id": str(node["id"]),
        "node_name": node["name"],
        "customer_id": str(site["customer_id"]),
        "role": site["kind"],
        "asn": site["asn"],
        "router_id": router_id or "0.0.0.0",
        "lan_prefixes": [str(p) for p in site["lan_prefixes"]],
        "tunnels": tunnels,
        "bfd_profiles": BFD_PROFILES,
    }
    if site["kind"] == "pop":
        body["reflector"] = {"listen": f":{PROBE_PORT}"}
    return body


def _hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def refresh(conn: psycopg.Connection, customer_id: Any) -> dict[str, int]:
    """Rebuild every enrolled node's desired state for a customer; store a new
    version where the body changed. Returns {node name: latest version}."""
    inv = _load(conn, customer_id)
    out: dict[str, int] = {}
    for site in inv["sites"]:
        node = inv["nodes"].get(site["id"])
        if node is None:
            continue
        body = build(inv, site)
        h = _hash(body)
        latest = conn.execute(
            "SELECT version, body_hash FROM desired_states WHERE node_id = %s ORDER BY version DESC LIMIT 1",
            (node["id"],),
        ).fetchone()
        version = latest["version"] if latest else 0
        if latest is None or latest["body_hash"] != h:
            version += 1
            conn.execute(
                "INSERT INTO desired_states (node_id, version, body, body_hash) VALUES (%s, %s, %s, %s)",
                (node["id"], version, Jsonb(body), h),
            )
        out[node["name"]] = version
    maps.refresh(conn, customer_id)
    return out


def latest(conn: psycopg.Connection, node_id: Any) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT version, body FROM desired_states WHERE node_id = %s ORDER BY version DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return {**row["body"], "version": row["version"]}
