"""ExaConnect Fabric: virtual circuits and the cloud router (ADR 0009).

Two kinds of circuit:

- **cloud**: from the customer's sites to a cloud provider's site-to-site
  VPN gateway (AWS, Azure, Google, Oracle or any IKEv2 gateway). The PoP
  terminates a route-based IPsec tunnel and runs BGP with the gateway over
  the tunnel's inside addresses. The PoP is the customer's cloud router:
  sites learn the cloud's routes from it, and with cloud-to-cloud on, each
  cloud learns the others' routes too, so traffic between clouds turns at
  the PoP instead of going back to an office.
- **site**: a layer 2 circuit (like an EVPL) between two sites: a VLAN at
  each end bridged over VXLAN between the sites' loopbacks, through the PoP.

Every circuit has a bandwidth the customer can change at any time. Each
speed is kept with its start and end, and charged by the hour.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from decimal import Decimal
from typing import Any

import psycopg

from . import audit

# Presets for the portal: the provider's usual gateway ASN and where to find
# the tunnel details. The customer still types what their console shows.
PROVIDERS = {
    "aws": {"name": "Amazon Web Services", "asn": 64512, "where": "VPC > Site-to-Site VPN connections"},
    "azure": {"name": "Microsoft Azure", "asn": 65515, "where": "Virtual network gateway > Connections"},
    "gcp": {"name": "Google Cloud", "asn": 64512, "where": "Hybrid Connectivity > VPN (HA VPN)"},
    "oracle": {"name": "Oracle Cloud", "asn": 31898, "where": "Networking > Site-to-Site VPN"},
    "other": {"name": "Other IKEv2 gateway", "asn": 64512, "where": "the gateway's VPN settings"},
}
IKE_PROPOSALS = "aes256-sha256-modp2048"
ESP_PROPOSALS = "aes256-sha256-modp2048"
PSK_RE = re.compile(r"^[A-Za-z1-9._][A-Za-z0-9._]{7,63}$")
INSIDE_POOL = ipaddress.ip_network("169.254.100.0/22")
LOOPBACK_NET = ipaddress.ip_network("10.254.0.0/24")
VNI_BASE = 10000
L2_MTU = 1370  # WireGuard's 1420 less VXLAN's 50
MAX_PREFIXES = 100
# The second tunnel of a resilient pair reaches the agent as circuit SECOND + id (ADR 0012).
SECOND = 1_000_000
HOURS_PER_MONTH = Decimal(730)


class CircuitError(ValueError):
    pass


def loopback(site: dict) -> str:
    return f"{LOOPBACK_NET.network_address + site['overlay_host']}/32"


def ifname(c: dict) -> str:
    return f"vc{c['id']}" if c["kind"] == "cloud" else f"vx{c['id']}"


def _cidrs(values: list[str], what: str) -> list[str]:
    out = []
    for v in values:
        try:
            net = ipaddress.ip_network(v.strip(), strict=False)
        except ValueError:
            raise CircuitError(f"{what}: '{v}' is not a subnet, like 10.100.0.0/16.") from None
        if net.version != 4:
            raise CircuitError(f"{what}: IPv4 only for now.")
        out.append(str(net))
    return sorted(set(out))


def _inside(
    conn: psycopg.Connection, customer_id: Any, want: str | None, circuit_id: Any = None, taken: tuple[str, ...] = ()
) -> str:
    """A tunnel's inside /30: the one the cloud console shows, or the next free one."""
    used = set(taken)
    for r in conn.execute(
        """SELECT inside_cidr, secondary_inside_cidr FROM circuits
           WHERE customer_id = %s AND deleted_at IS NULL AND id IS DISTINCT FROM %s""",
        (customer_id, circuit_id),
    ):
        used |= {str(x) for x in (r["inside_cidr"], r["secondary_inside_cidr"]) if x is not None}
    if want:
        try:
            net = ipaddress.ip_network(want, strict=False)
        except ValueError:
            raise CircuitError("Inside addresses: give a /30, like 169.254.10.0/30.") from None
        if net.version != 4 or net.prefixlen != 30:
            raise CircuitError("Inside addresses: give a /30, like 169.254.10.0/30.")
        if str(net) in used:
            raise CircuitError("Another circuit already uses those inside addresses.")
        return str(net)
    for net in INSIDE_POOL.subnets(new_prefix=30):
        if str(net) not in used:
            return str(net)
    raise CircuitError("No inside addresses left.")


def inside_pair(inside_cidr: Any) -> tuple[str, str]:
    """(our address with length, the gateway's address). The cloud takes the
    first host and we take the second, as AWS and Azure lay them out."""
    net = ipaddress.ip_network(str(inside_cidr))
    hosts = list(net.hosts())
    return f"{hosts[1]}/{net.prefixlen}", str(hosts[0])


def validate(conn: psycopg.Connection, customer_id: Any, c: dict[str, Any], circuit_id: Any = None) -> dict[str, Any]:
    """Check and normalise a circuit (new or changed). Raises CircuitError."""
    sites = {
        str(r["id"]): r
        for r in conn.execute(
            "SELECT id, name, kind, lan_prefixes::text[] AS lan FROM sites WHERE customer_id = %s", (customer_id,)
        )
    }
    if not 1 <= int(c["bandwidth_mbps"]) <= 1000:
        raise CircuitError("Bandwidth is 1 to 1,000 Mbps on today's PoPs.")
    if c.get("class_name"):
        known = conn.execute(
            "SELECT 1 FROM app_classes WHERE customer_id = %s AND name = %s", (customer_id, c["class_name"])
        ).fetchone()
        if known is None:
            raise CircuitError(f"There is no class called '{c['class_name']}'.")
    a = sites.get(str(c.get("a_site_id"))) if c.get("a_site_id") else None
    if c.get("a_site_id") and (a is None or a["kind"] != "site"):
        raise CircuitError("The A end must be one of this organisation's sites.")
    if c["kind"] == "cloud":
        if c.get("provider") not in PROVIDERS:
            raise CircuitError("Choose a cloud provider.")
        try:
            peer = ipaddress.ip_address(str(c.get("peer_address") or "").strip())
        except ValueError:
            raise CircuitError("The gateway's public address is an IPv4 address, like 52.1.2.3.") from None
        if peer.version != 4 or peer.is_loopback or peer.is_multicast or peer.is_unspecified:
            raise CircuitError("The gateway's public address is an IPv4 address, like 52.1.2.3.")
        c["peer_address"] = str(peer)
        if not 1 <= int(c.get("peer_asn") or 0) <= 4294967294:
            raise CircuitError("The gateway's ASN is 1 to 4294967294.")
        if c.get("psk") is not None and not PSK_RE.fullmatch(c["psk"]):
            raise CircuitError(
                "The pre-shared key is 8 to 64 letters, digits, dots and underscores, and can't start with 0."
            )
        if circuit_id is None and not c.get("psk"):
            raise CircuitError("Paste the pre-shared key from the cloud console.")
        c["cloud_prefixes"] = _cidrs(c.get("cloud_prefixes") or [], "Cloud subnets")
        c["a_prefixes"] = _cidrs(c.get("a_prefixes") or [], "Site subnets")
        lan = [ipaddress.ip_network(p) for s in sites.values() if s["kind"] == "site" for p in s["lan"]]
        for p in c["a_prefixes"]:
            if not any(ipaddress.ip_network(p).subnet_of(n) for n in lan):
                raise CircuitError(f"Site subnets: {p} is not inside any of your sites' networks.")
        c["inside_cidr"] = _inside(conn, customer_id, c.get("inside_cidr"), circuit_id)
        # A resilient pair (ADR 0012): a second tunnel to the cloud's second gateway address.
        second = str(c.get("secondary_peer_address") or "").strip()
        if second:
            try:
                peer2 = ipaddress.ip_address(second)
            except ValueError:
                raise CircuitError("The second gateway address is an IPv4 address, like 52.1.2.4.") from None
            if peer2.version != 4 or peer2.is_loopback or peer2.is_multicast or peer2.is_unspecified:
                raise CircuitError("The second gateway address is an IPv4 address, like 52.1.2.4.")
            if str(peer2) == c["peer_address"]:
                raise CircuitError("The second gateway address must differ from the first.")
            c["secondary_peer_address"] = str(peer2)
            c["secondary_inside_cidr"] = _inside(
                conn, customer_id, c.get("secondary_inside_cidr"), circuit_id, (c["inside_cidr"],)
            )
        else:
            c["secondary_peer_address"] = c["secondary_inside_cidr"] = None
        c["b_site_id"] = c["a_vlan"] = c["b_vlan"] = None
    elif c["kind"] == "site":
        b = sites.get(str(c.get("b_site_id"))) if c.get("b_site_id") else None
        if a is None or b is None or b["kind"] != "site" or a["id"] == b["id"]:
            raise CircuitError("A site circuit joins two different sites of this organisation.")
        for k in ("a_vlan", "b_vlan"):
            if c.get(k) is not None and not 1 <= int(c[k]) <= 4094:
                raise CircuitError("VLAN IDs are 1 to 4094.")
        if c.get("a_vlan") is None:
            raise CircuitError("Choose the VLAN to carry.")
        c["b_vlan"] = c.get("b_vlan") or c["a_vlan"]
        for k in (
            "provider",
            "peer_address",
            "peer_asn",
            "inside_cidr",
            "psk",
            "class_name",
            "secondary_peer_address",
            "secondary_inside_cidr",
        ):
            c[k] = None
        c["cloud_prefixes"] = c["a_prefixes"] = []
    else:
        raise CircuitError("A circuit goes to a cloud or to another site.")
    return c


COLUMNS = (
    "name",
    "kind",
    "a_site_id",
    "a_prefixes",
    "a_vlan",
    "b_site_id",
    "b_vlan",
    "provider",
    "region",
    "peer_address",
    "peer_asn",
    "inside_cidr",
    "secondary_peer_address",
    "secondary_inside_cidr",
    "cloud_prefixes",
    "class_name",
    "bandwidth_mbps",
    "enabled",
)


def _public(c: dict) -> dict:
    """Audit detail: never the key."""
    return {k: (str(v) if not isinstance(v, (int, bool, type(None))) else v) for k, v in c.items() if k != "psk"}


def create(conn: psycopg.Connection, customer_id: Any, c: dict[str, Any], actor: str) -> int:
    from . import desired

    c = validate(conn, customer_id, {**c, "enabled": c.get("enabled") is not False})
    row = {k: c.get(k) for k in COLUMNS}
    row["region"] = row["region"] or ""
    cid = conn.execute(
        """INSERT INTO circuits (customer_id, name, kind, a_site_id, a_prefixes, a_vlan, b_site_id, b_vlan, provider,
                                 region, peer_address, peer_asn, inside_cidr, secondary_peer_address,
                                 secondary_inside_cidr, psk, cloud_prefixes, class_name, bandwidth_mbps, enabled,
                                 created_by)
           VALUES (%(c)s, %(name)s, %(kind)s, %(a_site_id)s, %(a_prefixes)s::cidr[], %(a_vlan)s, %(b_site_id)s,
                   %(b_vlan)s, %(provider)s, %(region)s, %(peer_address)s, %(peer_asn)s, %(inside_cidr)s,
                   %(secondary_peer_address)s, %(secondary_inside_cidr)s, %(psk)s, %(cloud_prefixes)s::cidr[],
                   %(class_name)s, %(bandwidth_mbps)s, %(enabled)s, %(actor)s)
           RETURNING id""",
        {**row, "c": customer_id, "psk": c.get("psk"), "actor": actor},
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO circuit_bandwidth (circuit_id, mbps, valid_from, changed_by) VALUES (%s, %s, now(), %s)",
        (cid, row["bandwidth_mbps"], actor),
    )
    audit.record(conn, actor, "circuit.create", row["name"], customer_id, {"id": cid, **_public(row)})
    desired.refresh(conn, customer_id)
    return cid


def update(conn: psycopg.Connection, circuit: dict, changes: dict[str, Any], actor: str) -> None:
    """Change a circuit. A new bandwidth takes effect within 10 seconds and
    is billed from now."""
    from . import desired

    merged = {**{k: circuit[k] for k in COLUMNS}, **{k: v for k, v in changes.items() if v is not None}}
    for k in ("a_site_id", "b_site_id"):
        merged[k] = str(merged[k]) if merged[k] else None
    for k in ("a_prefixes", "cloud_prefixes"):
        merged[k] = [str(p) for p in merged[k] or []]
    for k in ("inside_cidr", "peer_address", "secondary_peer_address", "secondary_inside_cidr"):
        merged[k] = str(merged[k]) if merged[k] else None
    if changes.get("secondary_peer_address") == "":
        # Removing the second tunnel frees its inside addresses too.
        merged["secondary_inside_cidr"] = None
    psk = changes.get("psk")
    merged["psk"] = psk
    c = validate(conn, circuit["customer_id"], merged, circuit["id"])
    conn.execute(
        """UPDATE circuits SET name = %(name)s, a_site_id = %(a_site_id)s, a_prefixes = %(a_prefixes)s::cidr[],
             a_vlan = %(a_vlan)s, b_site_id = %(b_site_id)s, b_vlan = %(b_vlan)s, provider = %(provider)s,
             region = %(region)s, peer_address = %(peer_address)s, peer_asn = %(peer_asn)s,
             inside_cidr = %(inside_cidr)s, secondary_peer_address = %(secondary_peer_address)s,
             secondary_inside_cidr = %(secondary_inside_cidr)s, cloud_prefixes = %(cloud_prefixes)s::cidr[],
             class_name = %(class_name)s,
             bandwidth_mbps = %(bandwidth_mbps)s, enabled = %(enabled)s, psk = coalesce(%(psk)s, psk),
             updated_at = now()
           WHERE id = %(id)s""",
        {**{k: c.get(k) for k in COLUMNS}, "region": c.get("region") or "", "psk": psk, "id": circuit["id"]},
    )
    if int(c["bandwidth_mbps"]) != circuit["bandwidth_mbps"]:
        _close_bandwidth(conn, circuit["id"])
        conn.execute(
            "INSERT INTO circuit_bandwidth (circuit_id, mbps, valid_from, changed_by) VALUES (%s, %s, now(), %s)",
            (circuit["id"], c["bandwidth_mbps"], actor),
        )
    changed = {k: v for k, v in changes.items() if v is not None}
    audit.record(conn, actor, "circuit.update", circuit["name"], circuit["customer_id"], _public(changed))
    desired.refresh(conn, circuit["customer_id"])


def _close_bandwidth(conn: psycopg.Connection, circuit_id: Any) -> None:
    conn.execute(
        "UPDATE circuit_bandwidth SET valid_to = now() WHERE circuit_id = %s AND valid_to IS NULL", (circuit_id,)
    )


def delete(conn: psycopg.Connection, circuit: dict, actor: str) -> None:
    """Stop a circuit. The row stays (deleted) so this month's bill can be worked out."""
    from . import desired

    conn.execute("UPDATE circuits SET deleted_at = now(), enabled = false WHERE id = %s", (circuit["id"],))
    _close_bandwidth(conn, circuit["id"])
    audit.record(conn, actor, "circuit.delete", circuit["name"], circuit["customer_id"], {"id": circuit["id"]})
    desired.refresh(conn, circuit["customer_id"])


def charges(conn: psycopg.Connection, circuit: dict, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """Bandwidth charges between start and end: each speed for the hours it
    was set, at the circuit's monthly price per Mbps (730 hours a month)."""
    rows = conn.execute(
        """SELECT mbps, greatest(valid_from, %(s)s) AS f, least(coalesce(valid_to, %(e)s), %(e)s) AS t
           FROM circuit_bandwidth WHERE circuit_id = %(id)s AND valid_from < %(e)s
             AND coalesce(valid_to, %(e)s) > %(s)s ORDER BY valid_from""",
        {"id": circuit["id"], "s": start, "e": end},
    ).fetchall()
    price = Decimal(str(circuit["price_per_mbps_month"]))
    segments, total = [], Decimal(0)
    for r in rows:
        hours = Decimal(str((r["t"] - r["f"]).total_seconds())) / 3600
        amount = (Decimal(r["mbps"]) * hours * price / HOURS_PER_MONTH).quantize(Decimal("0.01"))
        total += amount
        segments.append(
            {"mbps": r["mbps"], "from": r["f"], "to": r["t"], "hours": round(float(hours), 2), "amount": float(amount)}
        )
    return {"price_per_mbps_month": float(price), "segments": segments, "total": float(total)}


def status(c: dict, st: list[dict], received: int, now: dt.datetime) -> str:
    """One word for the portal: off, provisioning, up or down. `st` is the
    circuit's reported state per node, `received` the probe replies in the
    last minute."""
    if c["deleted_at"] is not None:
        return "deleted"
    if not c["enabled"]:
        return "off"
    fresh = [s for s in st if (now - s["updated_at"]).total_seconds() < 60]
    if not fresh:
        return "provisioning"
    if c["kind"] == "cloud":
        # A resilient pair is up while either tunnel is.
        if any(s["ike"] == "up" and s["bgp"] == "Established" for s in fresh):
            return "up"
        return "provisioning" if all(s["ike"] in ("connecting", "") for s in fresh) else "down"
    return "up" if received > 0 else "down"
