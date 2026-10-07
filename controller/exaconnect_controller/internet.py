"""Internet breakout, NAT gateway and firewall (ADR 0010).

Each site sends its internet traffic through the PoP (the default), straight
out of its own carrier links, or nowhere. Where it leaves, the agent applies
NAT and the customer's firewall rules: at the PoP for sites going through it,
at the site for sites going straight out. Port forwards come in at the PoP's
public address, so they only reach sites that go through the PoP.
See docs/internet-contract.md.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import audit

MODES = ("pop", "local", "off")
PROTOCOLS = ("any", "tcp", "udp", "icmp")
PORTS_RE = re.compile(r"^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$")
MAX_RULES = 100
MAX_FORWARDS = 50
LOOPBACKS = "10.254.0.0/24"


class InternetError(ValueError):
    pass


def _cidrs(values: list[str] | None, what: str) -> list[str]:
    out = []
    for v in values or []:
        try:
            net = ipaddress.ip_network(str(v).strip(), strict=False)
        except ValueError:
            raise InternetError(f"{what}: '{v}' is not an address or subnet, like 203.0.113.0/24.") from None
        if net.version != 4:
            raise InternetError(f"{what}: IPv4 only for now.")
        out.append(str(net))
    if len(out) > 50:
        raise InternetError(f"{what}: at most 50 entries.")
    return sorted(set(out), key=lambda n: (ipaddress.ip_network(n).network_address, n))


def ports(spec: str | None) -> str:
    """'443, 8000-8100' -> '443,8000-8100'. Raises InternetError."""
    s = (spec or "").replace(" ", "")
    if not s:
        return ""
    if not PORTS_RE.fullmatch(s):
        raise InternetError("Ports are numbers or ranges separated by commas, like 443,8000-8100.")
    for part in s.split(","):
        lo, _, hi = part.partition("-")
        if not 1 <= int(lo) <= int(hi or lo) <= 65535:
            raise InternetError(f"Ports: {part} is not a port or range from 1 to 65535.")
    return s


def _sites(conn: psycopg.Connection, customer_id: Any) -> dict[str, dict]:
    return {
        str(r["id"]): r
        for r in conn.execute(
            "SELECT id, name, kind, internet_mode, lan_prefixes::text[] AS lan FROM sites WHERE customer_id = %s",
            (customer_id,),
        )
    }


# ---- breakout mode -------------------------------------------------------


def set_mode(conn: psycopg.Connection, customer_id: Any, site_id: str, mode: str, actor: str) -> None:
    from . import desired
    from .routing import maps

    if mode not in MODES:
        raise InternetError("Choose through the PoP, straight out at the site, or off.")
    site = _sites(conn, customer_id).get(str(site_id))
    if site is None or site["kind"] != "site":
        raise InternetError("Choose one of this organisation's sites.")
    conn.execute("UPDATE sites SET internet_mode = %s WHERE id = %s", (mode, site_id))
    audit.record(conn, actor, "internet.mode", site["name"], customer_id, {"from": site["internet_mode"], "to": mode})
    desired.refresh(conn, customer_id)
    maps.refresh(conn, customer_id)


# ---- firewall rules ------------------------------------------------------

RULE_FIELDS = ("site_id", "action", "src", "dst", "protocol", "ports", "description", "enabled")


def validate_rule(conn: psycopg.Connection, customer_id: Any, r: dict[str, Any]) -> dict[str, Any]:
    if r.get("action") not in ("allow", "deny"):
        raise InternetError("A rule allows or denies.")
    if r.get("site_id"):
        site = _sites(conn, customer_id).get(str(r["site_id"]))
        if site is None or site["kind"] != "site":
            raise InternetError("Choose one of this organisation's sites, or all sites.")
        r["site_id"] = str(site["id"])
    else:
        r["site_id"] = None
    r["protocol"] = r.get("protocol") or "any"
    if r["protocol"] not in PROTOCOLS:
        raise InternetError("Protocol is any, TCP, UDP or ICMP.")
    r["ports"] = ports(r.get("ports"))
    if r["ports"] and r["protocol"] not in ("tcp", "udp"):
        raise InternetError("Ports need TCP or UDP.")
    r["src"] = _cidrs(r.get("src"), "Source")
    r["dst"] = _cidrs(r.get("dst"), "Destination")
    r["description"] = (r.get("description") or "").strip()[:120]
    r["enabled"] = r.get("enabled") is not False
    return r


def _refresh(conn: psycopg.Connection, customer_id: Any) -> None:
    from . import desired

    desired.refresh(conn, customer_id)


def _renumber(conn: psycopg.Connection, customer_id: Any, ids: list[int]) -> None:
    for pos, rid in enumerate(ids, start=1):
        conn.execute("UPDATE firewall_rules SET position = %s WHERE id = %s", (pos, rid))


def _rule_ids(conn: psycopg.Connection, customer_id: Any) -> list[int]:
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM firewall_rules WHERE customer_id = %s ORDER BY position, id", (customer_id,)
        )
    ]


def create_rule(conn: psycopg.Connection, customer_id: Any, r: dict[str, Any], actor: str) -> int:
    ids = _rule_ids(conn, customer_id)
    if len(ids) >= MAX_RULES:
        raise InternetError(f"At most {MAX_RULES} firewall rules for now.")
    r = validate_rule(conn, customer_id, r)
    rid = conn.execute(
        """INSERT INTO firewall_rules (customer_id, site_id, position, action, src, dst, protocol, ports,
                                       description, enabled, created_by)
           VALUES (%s, %s, %s, %s, %s::cidr[], %s::cidr[], %s, %s, %s, %s, %s) RETURNING id""",
        (
            customer_id,
            r["site_id"],
            len(ids) + 1,
            r["action"],
            r["src"],
            r["dst"],
            r["protocol"],
            r["ports"],
            r["description"],
            r["enabled"],
            actor,
        ),
    ).fetchone()["id"]
    pos = r.get("position")
    if pos is not None:
        ids.insert(max(0, min(int(pos) - 1, len(ids))), rid)
        _renumber(conn, customer_id, ids)
    audit.record(conn, actor, "firewall.create", f"rule {rid}", customer_id, _detail(r))
    _refresh(conn, customer_id)
    return rid


def update_rule(conn: psycopg.Connection, customer_id: Any, rule: dict, changes: dict[str, Any], actor: str) -> None:
    merged = {k: rule[k] for k in RULE_FIELDS}
    merged["src"] = [str(p) for p in merged["src"]]
    merged["dst"] = [str(p) for p in merged["dst"]]
    merged.update({k: v for k, v in changes.items() if k in RULE_FIELDS and (v is not None or k == "site_id")})
    r = validate_rule(conn, customer_id, merged)
    conn.execute(
        """UPDATE firewall_rules SET site_id = %s, action = %s, src = %s::cidr[], dst = %s::cidr[], protocol = %s,
             ports = %s, description = %s, enabled = %s, updated_at = now() WHERE id = %s""",
        (
            r["site_id"],
            r["action"],
            r["src"],
            r["dst"],
            r["protocol"],
            r["ports"],
            r["description"],
            r["enabled"],
            rule["id"],
        ),
    )
    audit.record(conn, actor, "firewall.update", f"rule {rule['id']}", customer_id, _detail(changes))
    _refresh(conn, customer_id)


def delete_rule(conn: psycopg.Connection, customer_id: Any, rule: dict, actor: str) -> None:
    conn.execute("DELETE FROM firewall_rules WHERE id = %s", (rule["id"],))
    _renumber(conn, customer_id, _rule_ids(conn, customer_id))
    audit.record(conn, actor, "firewall.delete", f"rule {rule['id']}", customer_id)
    _refresh(conn, customer_id)


def reorder(conn: psycopg.Connection, customer_id: Any, ids: list[int], actor: str) -> None:
    have = _rule_ids(conn, customer_id)
    if sorted(ids) != sorted(have) or len(set(ids)) != len(ids):
        raise InternetError("Send every rule once, in the new order.")
    _renumber(conn, customer_id, ids)
    audit.record(conn, actor, "firewall.order", "", customer_id, {"ids": ids})
    _refresh(conn, customer_id)


def _detail(d: dict) -> dict:
    return {k: (v if isinstance(v, (int, bool, type(None), list)) else str(v)) for k, v in d.items()}


# ---- port forwards -------------------------------------------------------

FORWARD_FIELDS = ("description", "protocol", "port", "to_site_id", "to_address", "to_port", "allow_from", "enabled")


def validate_forward(
    conn: psycopg.Connection, customer_id: Any, f: dict[str, Any], forward_id: Any = None
) -> dict[str, Any]:
    if f.get("protocol") not in ("tcp", "udp"):
        raise InternetError("A port forward is TCP or UDP.")
    try:
        f["port"] = int(f.get("port") or 0)
        f["to_port"] = int(f.get("to_port") or f["port"])
    except (TypeError, ValueError):
        raise InternetError("Ports are numbers from 1 to 65535.") from None
    if not (1 <= f["port"] <= 65535 and 1 <= f["to_port"] <= 65535):
        raise InternetError("Ports are numbers from 1 to 65535.")
    site = _sites(conn, customer_id).get(str(f.get("to_site_id")))
    if site is None or site["kind"] != "site":
        raise InternetError("Choose the site the forward goes to.")
    f["to_site_id"] = str(site["id"])
    try:
        addr = ipaddress.IPv4Address(str(f.get("to_address") or "").strip())
    except ValueError:
        raise InternetError("The inside address is an IPv4 address, like 192.168.10.10.") from None
    if not any(addr in ipaddress.ip_network(p) for p in site["lan"]):
        raise InternetError(f"{addr} is not inside {site['name']}'s networks ({', '.join(site['lan'])}).")
    f["to_address"] = str(addr)
    f["allow_from"] = _cidrs(f.get("allow_from"), "Allowed from")
    f["description"] = (f.get("description") or "").strip()[:120]
    f["enabled"] = f.get("enabled") is not False
    taken = conn.execute(
        "SELECT customer_id FROM port_forwards WHERE protocol = %s AND port = %s AND id IS DISTINCT FROM %s",
        (f["protocol"], f["port"], forward_id),
    ).fetchone()
    if taken is not None:
        # The PoP's public address is shared, so say so without naming who has it.
        mine = str(taken["customer_id"]) == str(customer_id)
        raise InternetError(
            f"{f['protocol'].upper()} port {f['port']} is already forwarded"
            + (" by another of your forwards." if mine else " on this PoP. Choose another public port.")
        )
    return f


def create_forward(conn: psycopg.Connection, customer_id: Any, f: dict[str, Any], actor: str) -> int:
    count = conn.execute("SELECT count(*) AS n FROM port_forwards WHERE customer_id = %s", (customer_id,)).fetchone()
    if count["n"] >= MAX_FORWARDS:
        raise InternetError(f"At most {MAX_FORWARDS} port forwards for now.")
    f = validate_forward(conn, customer_id, f)
    fid = conn.execute(
        """INSERT INTO port_forwards (customer_id, description, protocol, port, to_site_id, to_address, to_port,
                                      allow_from, enabled, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::cidr[], %s, %s) RETURNING id""",
        (
            customer_id,
            f["description"],
            f["protocol"],
            f["port"],
            f["to_site_id"],
            f["to_address"],
            f["to_port"],
            f["allow_from"],
            f["enabled"],
            actor,
        ),
    ).fetchone()["id"]
    audit.record(conn, actor, "port_forward.create", f"forward {fid}", customer_id, _detail(f))
    _refresh(conn, customer_id)
    return fid


def update_forward(conn: psycopg.Connection, customer_id: Any, fwd: dict, changes: dict[str, Any], actor: str) -> None:
    merged = {k: fwd[k] for k in FORWARD_FIELDS}
    merged["allow_from"] = [str(p) for p in merged["allow_from"]]
    merged.update({k: v for k, v in changes.items() if k in FORWARD_FIELDS and v is not None})
    if "to_port" in changes and changes["to_port"] is None:
        merged["to_port"] = None  # cleared: the same as the public port
    f = validate_forward(conn, customer_id, merged, fwd["id"])
    conn.execute(
        """UPDATE port_forwards SET description = %s, protocol = %s, port = %s, to_site_id = %s, to_address = %s,
             to_port = %s, allow_from = %s::cidr[], enabled = %s, updated_at = now() WHERE id = %s""",
        (
            f["description"],
            f["protocol"],
            f["port"],
            f["to_site_id"],
            f["to_address"],
            f["to_port"],
            f["allow_from"],
            f["enabled"],
            fwd["id"],
        ),
    )
    audit.record(conn, actor, "port_forward.update", f"forward {fwd['id']}", customer_id, _detail(changes))
    _refresh(conn, customer_id)


def delete_forward(conn: psycopg.Connection, customer_id: Any, fwd: dict, actor: str) -> None:
    conn.execute("DELETE FROM port_forwards WHERE id = %s", (fwd["id"],))
    audit.record(conn, actor, "port_forward.delete", f"forward {fwd['id']}", customer_id)
    _refresh(conn, customer_id)


# ---- desired state -------------------------------------------------------


def load(conn: psycopg.Connection, customer_id: Any) -> dict[str, Any]:
    """What desired.build needs: enabled rules in order, enabled forwards and
    the PoP block list (ExaCarib's, for every customer)."""
    return {
        "blocklist": [
            r["p"]
            for r in conn.execute(
                """SELECT DISTINCT prefix::text AS p FROM blocked_sources
                   WHERE expires_at IS NULL OR expires_at > now() ORDER BY 1 LIMIT 1000"""
            )
        ],
        "firewall": conn.execute(
            """SELECT id, site_id, action, src::text[] AS src, dst::text[] AS dst, protocol, ports
               FROM firewall_rules WHERE customer_id = %s AND enabled ORDER BY position, id""",
            (customer_id,),
        ).fetchall(),
        "forwards": conn.execute(
            """SELECT id, protocol, port, to_site_id, host(to_address) AS to_address, to_port,
                      allow_from::text[] AS allow_from
               FROM port_forwards WHERE customer_id = %s AND enabled ORDER BY id""",
            (customer_id,),
        ).fetchall(),
    }


def _rules_for(rules: list[dict], sources: list[dict]) -> list[dict]:
    """Firewall rules for traffic from `sources` (sites), each rule's source
    narrowed to the sites it names."""
    out = []
    for r in rules:
        sites = [s for s in sources if r["site_id"] is None or s["id"] == r["site_id"]]
        if not sites:
            continue
        lan = [str(p) for s in sites for p in s["lan_prefixes"]]
        if r["src"]:
            src = [p for p in r["src"] if any(_overlaps(p, n) for n in lan)]
            if not src:
                continue
        else:
            src = lan
        out.append(
            {
                "id": r["id"],
                "action": r["action"],
                "src": sorted(set(src)),
                "dst": list(r["dst"]),
                "protocol": r["protocol"],
                "ports": r["ports"],
            }
        )
    return out


def _overlaps(a: str, b: str) -> bool:
    return ipaddress.ip_network(a).overlaps(ipaddress.ip_network(b))


def block(inv: dict[str, Any], site: dict, tunnels: list[str], links: list[dict]) -> dict[str, Any] | None:
    """The `internet` block of a node's desired state, or None."""
    net = inv["internet"]
    if site["kind"] == "pop":
        if not site.get("internet_interface") or not site.get("internet_gateway"):
            return None
        pop_mode = [s for s in inv["sites"] if s["kind"] == "site" and s["internet_mode"] == "pop"]
        lan = sorted({str(p) for s in [*pop_mode, site] for p in s["lan_prefixes"]})
        public = site.get("cloud_address")
        pop_ids = {s["id"] for s in pop_mode}
        return {
            "mode": "gateway",
            "lan_prefixes": lan,
            "uplinks": [
                {
                    "interface": site["internet_interface"],
                    "gateway": str(ipaddress.ip_address(str(site["internet_gateway"]))),
                    "path": "",
                    "tunnel": "",
                }
            ],
            "public_address": str(ipaddress.ip_interface(str(public)).ip) if public else "",
            "firewall": _rules_for(net["firewall"], pop_mode),
            "port_forwards": [
                {
                    "id": f["id"],
                    "protocol": f["protocol"],
                    "port": f["port"],
                    "to_address": f["to_address"],
                    "to_port": f["to_port"],
                    "allow_from": list(f["allow_from"]),
                }
                for f in net["forwards"]
                if f["to_site_id"] in pop_ids and public
            ],
            # DDoS protection on the public address (ADR 0012).
            "protection": {
                "enabled": bool(site["ddos_enabled"]) and bool(public),
                "new_per_source": site["ddos_new_per_source"],
                "syn_per_s": site["ddos_syn_per_s"],
                "block_minutes": site["ddos_block_minutes"],
                "blocklist": net["blocklist"],
            },
        }
    mode = site["internet_mode"]
    out: dict[str, Any] = {"mode": mode, "lan_prefixes": [str(p) for p in site["lan_prefixes"]]}
    if not out["lan_prefixes"]:
        return None
    if mode == "pop":
        out["tunnels"] = tunnels
    elif mode == "local":
        out["uplinks"] = [
            {
                "interface": lk["underlay_interface"],
                "gateway": str(lk["underlay_gateway"]),
                "path": lk["path"],
                "tunnel": lk["tunnel"],
            }
            for lk in links
            if lk["underlay_gateway"] is not None
        ]
        out["firewall"] = _rules_for(net["firewall"], [site])
    return out


def corporate(conn: psycopg.Connection, customer_id: Any) -> list[str]:
    """The customer's own destinations: site LANs, loopbacks, the overlay and
    cloud circuit prefixes. A site breaking out locally keeps classifying
    traffic to these, and nothing else."""
    nets = [LOOPBACKS]
    for r in conn.execute("SELECT lan_prefixes::text[] AS lan FROM sites WHERE customer_id = %s", (customer_id,)):
        nets += r["lan"]
    nets += [r["c"] for r in conn.execute("SELECT overlay_cidr::text AS c FROM paths")]
    for r in conn.execute(
        """SELECT cloud_prefixes::text[] AS p FROM circuits
           WHERE customer_id = %s AND kind = 'cloud' AND enabled AND deleted_at IS NULL""",
        (customer_id,),
    ):
        nets += r["p"]
    return [str(n) for n in ipaddress.collapse_addresses(ipaddress.ip_network(n) for n in nets)]


# ---- telemetry and the API view ------------------------------------------


def record(conn: psycopg.Connection, node: dict, data: dict[str, Any]) -> None:
    before = conn.execute("SELECT auto_blocked FROM internet_state WHERE node_id = %s", (node["id"],)).fetchone()
    had = {b.get("address") for b in (before["auto_blocked"] if before else []) or []}
    for b in data.get("auto_blocked", []):
        if str(b["address"]) not in had:
            # A newly blocked flooding source (ADR 0012), published as ddos.blocked (ADR 0026).
            conn.execute(
                "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (now(), %s, %s, 'ddos_blocked', %s)",
                (node["customer_id"], node["id"], Jsonb({"address": str(b["address"]), "expires_s": b["expires_s"]})),
            )
    conn.execute(
        """INSERT INTO internet_state (node_id, customer_id, mode, via, counters, auto_blocked, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (node_id) DO UPDATE SET mode = EXCLUDED.mode, via = EXCLUDED.via,
             counters = EXCLUDED.counters, auto_blocked = EXCLUDED.auto_blocked, updated_at = now()""",
        (
            node["id"],
            node["customer_id"],
            data.get("mode", ""),
            data.get("via", ""),
            Jsonb(data.get("counters", [])),
            Jsonb([{"address": str(b["address"]), "expires_s": b["expires_s"]} for b in data.get("auto_blocked", [])]),
        ),
    )


def view(conn: psycopg.Connection, customer_id: Any) -> dict[str, Any]:
    sites = conn.execute(
        """SELECT s.id, s.name, s.kind, s.internet_mode, s.cloud_address, s.lan_prefixes::text[] AS lan,
                  n.id AS node_id, st.via, st.updated_at AS state_at
           FROM sites s LEFT JOIN nodes n ON n.site_id = s.id LEFT JOIN internet_state st ON st.node_id = n.id
           WHERE s.customer_id = %s ORDER BY s.overlay_host""",
        (customer_id,),
    ).fetchall()
    links = conn.execute(
        """SELECT l.site_id, l.underlay_interface, l.path, p.tunnel, p.label FROM links l
           JOIN paths p ON p.name = l.path WHERE l.customer_id = %s ORDER BY p.ordinal""",
        (customer_id,),
    ).fetchall()
    pop = next((s for s in sites if s["kind"] == "pop"), None)
    # Counters: the PoP's for sites going through it, each site's own for local breakout.
    counters: dict[tuple[str, int], dict[str, int]] = {}
    nodes = [s["node_id"] for s in sites if s["node_id"] is not None]
    for row in conn.execute("SELECT node_id, counters FROM internet_state WHERE node_id = ANY(%s)", (nodes,)):
        for c in row["counters"] or []:
            k = (str(c.get("kind")), int(c.get("id") or 0))
            agg = counters.setdefault(k, {"packets": 0, "bytes": 0})
            agg["packets"] += int(c.get("packets") or 0)
            agg["bytes"] += int(c.get("bytes") or 0)

    def via_label(site: dict) -> str:
        via = site["via"] or ""
        for lk in links:
            if lk["site_id"] != site["id"]:
                continue
            if via == lk["tunnel"]:
                return f"ExaCarib PoP over {lk['label']}"
            if via == lk["underlay_interface"]:
                return lk["label"]
        return via

    names = {str(s["id"]): s["name"] for s in sites}
    modes = {str(s["id"]): s["internet_mode"] for s in sites}
    rules = conn.execute(
        """SELECT id, position, site_id, action, src::text[] AS src, dst::text[] AS dst, protocol, ports,
                  description, enabled, created_by, updated_at
           FROM firewall_rules WHERE customer_id = %s ORDER BY position, id""",
        (customer_id,),
    ).fetchall()
    forwards = conn.execute(
        """SELECT id, description, protocol, port, to_site_id, host(to_address) AS to_address, to_port,
                  allow_from::text[] AS allow_from, enabled, created_by, updated_at
           FROM port_forwards WHERE customer_id = %s ORDER BY port, id""",
        (customer_id,),
    ).fetchall()
    return {
        "public_address": str(ipaddress.ip_interface(str(pop["cloud_address"])).ip)
        if pop and pop["cloud_address"]
        else None,
        "sites": [
            {
                "id": s["id"],
                "name": s["name"],
                "mode": s["internet_mode"],
                "via": s["via"] or "",
                "via_label": via_label(s),
                "uplinks": [lk["label"] for lk in links if lk["site_id"] == s["id"]],
                "updated_at": s["state_at"],
            }
            for s in sites
            if s["kind"] == "site"
        ],
        "rules": [
            {
                **r,
                "site": names.get(str(r["site_id"])) if r["site_id"] else None,
                **counters.get(("rule", r["id"]), {"packets": 0, "bytes": 0}),
            }
            for r in rules
        ],
        "forwards": [
            {
                **f,
                "to_site": names.get(str(f["to_site_id"])),
                "active": f["enabled"] and modes.get(str(f["to_site_id"])) == "pop",
                **counters.get(("forward", f["id"]), {"packets": 0, "bytes": 0}),
            }
            for f in forwards
        ],
        "inbound_dropped": counters.get(("inbound", 0), {"packets": 0})["packets"],
        "protection": protection(conn, pop) if pop else None,
    }


DROP_KINDS = ("blocked", "auto", "flood", "syn")


def protection(conn: psycopg.Connection, pop: dict) -> dict[str, Any]:
    """The PoP's DDoS protection: its settings, what it dropped and what is
    blocked now (ADR 0012). Counts only: nothing about anyone's traffic."""
    row = conn.execute(
        """SELECT s.ddos_enabled, s.ddos_new_per_source, s.ddos_syn_per_s, s.ddos_block_minutes,
                  st.counters, st.auto_blocked, st.updated_at
           FROM sites s LEFT JOIN nodes n ON n.site_id = s.id LEFT JOIN internet_state st ON st.node_id = n.id
           WHERE s.id = %s""",
        (pop["id"],),
    ).fetchone()
    dropped = dict.fromkeys(DROP_KINDS, 0)
    for c in row["counters"] or []:
        if c.get("kind") in dropped:
            dropped[c["kind"]] += int(c.get("packets") or 0)
    return {
        "enabled": row["ddos_enabled"],
        "new_per_source": row["ddos_new_per_source"],
        "syn_per_s": row["ddos_syn_per_s"],
        "block_minutes": row["ddos_block_minutes"],
        "dropped": dropped,
        "auto_blocked": len(row["auto_blocked"] or []),
        "updated_at": row["updated_at"],
    }
