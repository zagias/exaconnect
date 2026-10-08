"""Traffic rules and priority queuing (ADR 0007).

A class says how traffic is treated: its priority, its SLA and a preferred
path. A traffic rule says which traffic belongs to a class: named
applications from the catalogue, ports, destination and source subnets,
VLANs, websites (domain names) and DSCP marks, optionally only at some sites.

Rules compile into the steering map's `matches`, which the agent turns into
nftables rules ahead of the classes' own DSCP and port matches. Inside one
match every field given must match; a destination subnet or a domain both
count as "the destination". A rule naming applications becomes one match per
application signature, plus one for its own ports and addresses if it has any.

Priorities become DSCP marks on the way into the tunnels, and each tunnel is
shaped a little under its link's speed with CAKE (diffserv4), so the
priority queues are the ones that decide what waits when a link is full.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import psycopg

from . import audit
from .ai import apps
from .ports import parse_ports

# Limits the agent enforces on a map (agent/internal/steer).
MAX_MATCHES = 200
MAX_DOMAINS = 100
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# Shape tunnels to 95% of the link speed so queues build where CAKE can sort them.
SHAPE_SHARE = 0.95


class RuleError(ValueError):
    pass


def clean_domain(d: str) -> str:
    d = d.strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    # Agents resolve names to addresses; a wildcard is matched by its base name.
    d = d.split("/", 1)[0].rstrip(".").removeprefix("*.")
    if not DOMAIN_RE.fullmatch(d):
        raise RuleError(f"'{d}' is not a website name, like teams.microsoft.com")
    return d


def clean_cidr(c: str) -> str:
    try:
        net = ipaddress.ip_network(c.strip(), strict=False)
    except ValueError:
        raise RuleError(f"'{c}' is not an IP address or subnet, like 10.1.0.0/16") from None
    if net.version != 4:
        raise RuleError(f"'{c}': rules match IPv4 addresses for now.")
    return str(net)


def validate(rule: dict[str, Any], class_names: set[str]) -> dict[str, Any]:
    """Normalise a rule's fields in place and check them. Raises RuleError."""
    if rule["class_name"] not in class_names:
        raise RuleError(f"There is no class called '{rule['class_name']}'.")
    unknown = [a for a in rule.get("apps", []) if a not in apps.BY_ID]
    if unknown:
        raise RuleError(f"Unknown application: {', '.join(unknown)}.")
    try:
        parse_ports(rule.get("ports", ""))
    except ValueError as e:
        raise RuleError(f"Ports: {e}.") from None
    rule["dst_subnets"] = [clean_cidr(c) for c in rule.get("dst_subnets", [])]
    rule["src_subnets"] = [clean_cidr(c) for c in rule.get("src_subnets", [])]
    rule["domains"] = sorted({clean_domain(d) for d in rule.get("domains", [])})
    rule["vlans"] = sorted(set(rule.get("vlans", [])))
    if any(not 1 <= v <= 4094 for v in rule["vlans"]):
        raise RuleError("VLAN IDs are 1 to 4094.")
    rule["dscp"] = sorted(set(rule.get("dscp", [])))
    if any(not 0 <= d <= 63 for d in rule["dscp"]):
        raise RuleError("DSCP values are 0 to 63.")
    what = rule.get("apps") or rule.get("ports") or rule["dst_subnets"] or rule["domains"]
    where = rule["src_subnets"] or rule["vlans"] or rule["dscp"]
    if not what and not where:
        raise RuleError("Say which traffic the rule is for: an application, ports, addresses, VLANs or websites.")
    return rule


def _match(rule: dict, ports: str, dst: list[str], domains: list[str]) -> dict[str, Any]:
    m: dict[str, Any] = {"class": rule["class_name"]}
    for key, value in (
        ("vlans", list(rule["vlans"] or [])),
        ("src", [str(s) for s in rule["src_subnets"] or []]),
        ("dst", [str(s) for s in dst]),
        ("domains", list(domains)),
        ("ports", parse_ports(ports)),
        ("dscp", list(rule["dscp"] or [])),
    ):
        if value:
            m[key] = value
    return m


def rule_matches(rule: dict) -> list[dict[str, Any]]:
    out = []
    for app_id in rule["apps"] or []:
        for sig in apps.BY_ID[app_id].signatures:
            out.append(_match(rule, sig.ports, list(sig.subnets), list(sig.domains)))
    own = rule["ports"] or rule["dst_subnets"] or rule["domains"]
    if own or not rule["apps"]:
        out.append(_match(rule, rule["ports"] or "", list(rule["dst_subnets"] or []), list(rule["domains"] or [])))
    return out


def applies_to(rule: dict, site_id: Any) -> bool:
    return not rule["site_ids"] or site_id in rule["site_ids"]


def compile_matches(rules: list[dict], site_ids: list[Any]) -> list[dict[str, Any]]:
    """The map's matches for a node serving these sites (one for a site, all
    of them for the PoP), in rule order, without duplicates."""
    out: list[dict] = []
    seen: set[str] = set()
    for rule in rules:
        if not rule["enabled"] or not any(applies_to(rule, s) for s in site_ids):
            continue
        for m in rule_matches(rule):
            key = repr(sorted(m.items()))
            if key not in seen:
                seen.add(key)
                out.append(m)
    return out


def fit(matches: list[dict]) -> list[dict]:
    """The leading matches that stay within the agent's limits. The API checks
    the limits when a rule is saved; this is the backstop, since the agent
    refuses a whole map that is over them."""
    out, domains = [], 0
    for m in matches:
        d = len(m.get("domains", []))
        if len(out) >= MAX_MATCHES or domains + d > MAX_DOMAINS:
            break
        out.append(m)
        domains += d
    return out


def check_limits(matches: list[dict]) -> None:
    if len(matches) > MAX_MATCHES:
        raise RuleError(f"That makes {len(matches)} matches at one site; the limit is {MAX_MATCHES}.")
    domains = sum(len(m.get("domains", [])) for m in matches)
    if domains > MAX_DOMAINS:
        raise RuleError(f"That makes {domains} websites at one site; the limit is {MAX_DOMAINS}.")


def qos(classes: list[dict], links: list[dict]) -> dict[str, Any] | None:
    """DSCP per class priority, and the shaping rate per tunnel."""
    q_classes = [
        {"name": c["name"], "dscp": apps.PRIORITY_DSCP[c["priority"]]}
        for c in classes
        if c.get("priority") in apps.PRIORITY_DSCP
    ]
    q_paths = [
        {"tunnel": lk["tunnel"], "shape_kbit": int(float(lk["shape_mbps"]) * 1000 * SHAPE_SHARE)}
        for lk in links
        if lk.get("shape_mbps")
    ]
    if not q_classes and not q_paths:
        return None
    return {"classes": q_classes, "paths": q_paths}


# ---- Storage ----

RULE_FIELDS = (
    "name",
    "class_name",
    "site_ids",
    "apps",
    "ports",
    "dst_subnets",
    "src_subnets",
    "vlans",
    "domains",
    "dscp",
    "enabled",
    "ordinal",
)


def load_rules(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT id, customer_id, name, class_name, site_ids, apps, ports, dst_subnets::text[] AS dst_subnets,
                  src_subnets::text[] AS src_subnets, vlans, domains, dscp, enabled, ordinal, source, created_by,
                  created_at, updated_at
           FROM traffic_rules WHERE customer_id = %s ORDER BY ordinal, id""",
        (customer_id,),
    ).fetchall()


def save_rule(
    conn: psycopg.Connection,
    customer_id: Any,
    rule: dict[str, Any],
    actor: str,
    rule_id: int | None = None,
    source: str = "customer",
) -> int:
    """Validate and store a rule (new, or replacing rule_id), check the
    limits at every site, audit it and refresh the steering maps."""
    from .routing import maps

    classes = {r["name"] for r in conn.execute("SELECT name FROM app_classes WHERE customer_id = %s", (customer_id,))}
    rule = validate(dict(rule), classes)
    sites = {
        str(r["id"]): r["id"]
        for r in conn.execute("SELECT id FROM sites WHERE customer_id = %s AND kind = 'site'", (customer_id,))
    }
    bad = [s for s in rule.get("site_ids", []) if str(s) not in sites]
    if bad:
        raise RuleError("A rule can only name this organisation's own sites.")
    rule["site_ids"] = [sites[str(s)] for s in rule.get("site_ids", [])]
    row = {k: rule.get(k) for k in RULE_FIELDS}
    row["enabled"] = True if row["enabled"] is None else row["enabled"]
    row["ordinal"] = row["ordinal"] or 100
    row["ports"] = row["ports"] or ""
    for k in ("site_ids", "apps", "dst_subnets", "src_subnets", "vlans", "domains", "dscp"):
        row[k] = row[k] or []
    if rule_id is None:
        rule_id = conn.execute(
            """INSERT INTO traffic_rules (customer_id, name, class_name, site_ids, apps, ports, dst_subnets,
                                          src_subnets, vlans, domains, dscp, enabled, ordinal, source, created_by)
               VALUES (%(c)s, %(name)s, %(class_name)s, %(site_ids)s, %(apps)s, %(ports)s, %(dst_subnets)s::cidr[],
                       %(src_subnets)s::cidr[], %(vlans)s, %(domains)s, %(dscp)s, %(enabled)s, %(ordinal)s,
                       %(source)s, %(actor)s)
               RETURNING id""",
            {**row, "c": customer_id, "source": source, "actor": actor},
        ).fetchone()["id"]
        action = "rule.create"
    else:
        n = conn.execute(
            """UPDATE traffic_rules SET name = %(name)s, class_name = %(class_name)s, site_ids = %(site_ids)s,
                 apps = %(apps)s, ports = %(ports)s, dst_subnets = %(dst_subnets)s::cidr[],
                 src_subnets = %(src_subnets)s::cidr[], vlans = %(vlans)s, domains = %(domains)s, dscp = %(dscp)s,
                 enabled = %(enabled)s, ordinal = %(ordinal)s, updated_at = now()
               WHERE id = %(id)s AND customer_id = %(c)s""",
            {**row, "c": customer_id, "id": rule_id},
        ).rowcount
        if not n:
            raise LookupError("rule not found")
        action = "rule.update"
    rules = load_rules(conn, customer_id)
    nodes = conn.execute(
        "SELECT s.id, s.kind FROM sites s JOIN nodes n ON n.site_id = s.id WHERE s.customer_id = %s", (customer_id,)
    ).fetchall()
    served_by_pop = [s["id"] for s in nodes if s["kind"] == "site"]
    for s in nodes:
        check_limits(compile_matches(rules, served_by_pop if s["kind"] == "pop" else [s["id"]]))
    audit.record(conn, actor, action, row["name"], customer_id, {"id": rule_id, **_jsonable(row)})
    maps.refresh(conn, customer_id)
    _reconcile_detections(conn, customer_id)
    return rule_id


def delete_rule(conn: psycopg.Connection, customer_id: Any, rule_id: int, actor: str) -> None:
    from .routing import maps

    # A detection whose rule is deleted goes back to being a suggestion.
    conn.execute(
        "UPDATE app_detections SET status = 'suggested' WHERE rule_id = %s AND customer_id = %s",
        (rule_id, customer_id),
    )
    row = conn.execute(
        "DELETE FROM traffic_rules WHERE id = %s AND customer_id = %s RETURNING name", (rule_id, customer_id)
    ).fetchone()
    if row is None:
        raise LookupError("rule not found")
    audit.record(conn, actor, "rule.delete", row["name"], customer_id, {"id": rule_id})
    maps.refresh(conn, customer_id)
    _reconcile_detections(conn, customer_id)


def _reconcile_detections(conn: psycopg.Connection, customer_id: Any) -> None:
    from .ai import detect

    detect.reconcile(conn, customer_id)


def _jsonable(row: dict) -> dict:
    return {k: [str(x) for x in v] if isinstance(v, list) else v for k, v in row.items()}
