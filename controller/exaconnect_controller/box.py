"""Cloud management of ExaCarib boxes (ADR 0049).

A box that runs the box console (ADR 0046) has a "Managed from" setting:

- ``site``: only the box console changes the box's own settings;
- ``cloud``: only the portal does, and the console reads, apart from an
  emergency take-back that switches the box to ``site``;
- ``both``: either does, and the newest change wins.

The portal's copy lives on the node (``nodes.box_managed``) and goes down in
desired state; the box's copy comes up with telemetry. Each carries the time
it was set, and each side keeps the newest, so a switch made on either side
reaches the other however the two were cut off from each other meanwhile.
Switching never changes the box's settings themselves.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import audit, desired

MODES = ("site", "cloud", "both")
DEFAULT = "both"
NAMES = {"site": "on site", "cloud": "the cloud", "both": "on site and the cloud"}
_EPOCH = dt.datetime(1, 1, 1, tzinfo=dt.UTC)


def _at(m: dict | None) -> dt.datetime:
    if not m or not m.get("at"):
        return _EPOCH
    at = m["at"]
    if isinstance(at, str):
        at = dt.datetime.fromisoformat(at)
    return at if at.tzinfo else at.replace(tzinfo=dt.UTC)


def managed(node: dict) -> dict[str, Any]:
    """The node's setting as the portal knows it, with the default filled in."""
    m = node.get("box_managed") or {}
    return {"mode": m.get("mode") if m.get("mode") in MODES else DEFAULT, "at": m.get("at"), "by": m.get("by")}


def block(node: dict) -> dict[str, Any] | None:
    """The desired-state ``box`` block for a node, or None for nodes without a box console."""
    if not (node.get("box") or {}).get("console"):
        return None
    out: dict[str, Any] = {}
    m = node.get("box_managed")
    if m:
        out["managed_from"] = {"mode": m["mode"], "at": m["at"], "by": m.get("by") or ""}
    bn = node.get("box_network")
    if bn:
        out["network"] = {"network": bn["network"], "at": bn["at"], "by": bn.get("by") or ""}
    return out


def set_from_portal(conn: psycopg.Connection, node: dict, mode: str, actor: str) -> dict[str, Any]:
    """Switch a box from the portal. Returns the new setting."""
    if mode not in MODES:
        raise ValueError(mode)
    now = dt.datetime.now(dt.UTC)
    cur = node.get("box_managed")
    if _at(cur) >= now:
        # The box's clock ran ahead of ours: still make this the newest.
        now = _at(cur) + dt.timedelta(milliseconds=1)
    m = {"mode": mode, "at": now.isoformat(), "by": f"Connect portal ({actor.removeprefix('user:')})"}
    conn.execute("UPDATE nodes SET box_managed = %s WHERE id = %s", (Jsonb(m), node["id"]))
    audit.record(conn, actor, "box.managed", node["name"], node["customer_id"], {"mode": mode})
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, 'box_managed', %s)",
        (now, node["customer_id"], node["id"], Jsonb({"mode": mode, "by": m["by"], "where": "portal"})),
    )
    desired.refresh(conn, node["customer_id"])
    return m


def from_box(conn: psycopg.Connection, node: Any, reported: dict | None) -> bool:
    """Take the box's setting when it is newer than the portal's. Returns whether it changed."""
    if not reported or reported.get("mode") not in MODES:
        return False
    at = _at(reported)
    if at == _EPOCH:
        return False  # never set on the box: the portal's stands
    row = conn.execute("SELECT box_managed FROM nodes WHERE id = %s FOR UPDATE", (node.id,)).fetchone()
    cur = row["box_managed"] if row else None
    if at <= _at(cur):
        return False
    # Kept as the box sent it, even from a box clock running fast, so the two
    # copies agree and stop; a portal switch after it still counts as newer.
    m = {"mode": reported["mode"], "at": at.isoformat(), "by": (reported.get("by") or "box console")[:120]}
    conn.execute("UPDATE nodes SET box_managed = %s WHERE id = %s", (Jsonb(m), node.id))
    if not cur or cur.get("mode") != m["mode"]:
        audit.record(
            conn, f"node:{node.name}", "box.managed", node.name, node.customer_id, {"mode": m["mode"], "by": m["by"]}
        )
    desired.refresh(conn, node.customer_id)
    return True


# Ports set in the portal (ADR 0051). The portal's copy lives in
# ``nodes.box_network`` as ``{network, at, by}`` and goes down in desired state.
# The box takes it while managed from the cloud or both, keeps it only once it
# can still reach ExaCarib with it, and reports what became of it
# (``box.portal``). A PPPoE password typed in the portal is kept only until the
# box has handled that set-up; after that it is "set", meaning "keep the one
# the box has".

PATHS = {"carrier-a": "Internet link 1", "carrier-b": "Internet link 2", "sat": "Satellite"}
_IFACE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,14}$")
_PPP = re.compile(r'^[^\s"\\]{1,128}$')
SET = "set"


class NetworkError(ValueError):
    pass


def _port(p: dict, have: set[str] | None, used: set[str], wan: bool, box_port: dict | None) -> dict:
    iface = str(p.get("iface") or "")
    if not _IFACE.match(iface) or (have is not None and iface not in have):
        raise NetworkError(f"This box has no port called {iface!r}.")
    if iface in used:
        raise NetworkError(f"{iface} is chosen twice.")
    used.add(iface)
    mode = p.get("mode")
    out: dict[str, Any] = {"iface": iface, "mode": mode}
    dns = [str(d).strip() for d in (p.get("dns") or []) if str(d).strip()]
    for d in dns:
        try:
            ipaddress.ip_address(d)
        except ValueError:
            raise NetworkError(f"DNS server {d!r} is not an IP address.") from None
    if dns:
        out["dns"] = dns[:4]
    if mode in ("dhcp", "keep"):
        pass
    elif mode == "static":
        addr, gw = str(p.get("address") or "").strip(), str(p.get("gateway") or "").strip()
        try:
            iface_net = ipaddress.IPv4Interface(addr)
        except ValueError:
            raise NetworkError(f"{iface}: give the address with its size, like 192.168.1.2/24.") from None
        if "/" not in addr or not 8 <= iface_net.network.prefixlen <= 31:
            raise NetworkError(f"{iface}: give the address with its size, like 192.168.1.2/24.")
        if iface_net.ip == iface_net.network.network_address:
            raise NetworkError(f"{iface}: {addr} is the network itself, not an address in it.")
        if gw:
            try:
                g = ipaddress.IPv4Address(gw)
            except ValueError:
                g = None
            if g is None or g not in iface_net.network or g == iface_net.ip:
                raise NetworkError(f"{iface}: the gateway must be another address in {iface_net.network}.")
            out["gateway"] = gw
        elif wan:
            raise NetworkError(f"{iface}: an internet port with a fixed address needs a gateway.")
        out["address"] = addr
    elif mode == "pppoe":
        if not wan:
            raise NetworkError("PPPoE is for internet ports only.")
        user, pw = str(p.get("pppoe_user") or ""), str(p.get("pppoe_password") or "")
        if pw == SET and not (box_port and box_port.get("pppoe_set")):
            pw = ""
        if not _PPP.match(user) or not _PPP.match(pw):
            raise NetworkError(f"{iface}: PPPoE needs the user name and password from your internet provider.")
        out["pppoe_user"], out["pppoe_password"] = user, pw
    else:
        raise NetworkError(f"{iface}: choose automatic (DHCP), fixed, PPPoE or leave as it is.")
    return out


def check_network(net: dict, box: dict | None) -> dict:
    """The same checks as the box makes (agent/internal/console/netcfg.go), so a
    mistake shows in the portal at once. Returns the clean set-up."""
    box = box or {}
    have = {p["name"] for p in box.get("ports") or [] if p.get("name")} or None
    reported = {p["iface"]: p for p in (box.get("wan") or []) + ([box["lan"]] if box.get("lan") else [])}
    wan = net.get("wan") or []
    if not 1 <= len(wan) <= 3:
        raise NetworkError("Choose one to three internet ports.")
    used: set[str] = set()
    seen: set[str] = set()
    out: dict[str, Any] = {"wan": []}
    for i, p in enumerate(wan):
        path = p.get("path") or ("carrier-a", "carrier-b", "sat")[i]
        if path not in PATHS:
            raise NetworkError(f"Unknown link {path!r}.")
        if path in seen:
            raise NetworkError(f"{PATHS[path]} is set on two ports.")
        seen.add(path)
        q = _port(p, have, used, True, reported.get(p.get("iface")))
        out["wan"].append({**q, "path": path})
    lan = net.get("lan")
    dhcp = bool(net.get("lan_dhcp"))
    if lan:
        if lan.get("mode") not in ("static", "keep"):
            raise NetworkError("The office port needs a fixed address.")
        out["lan"] = _port(lan, have, used, False, None)
        if dhcp:
            if lan.get("mode") != "static":
                raise NetworkError("To hand out addresses, give the office port a fixed address.")
            if ipaddress.IPv4Interface(out["lan"]["address"]).network.num_addresses < 8:
                raise NetworkError("The office network is too small to hand out addresses.")
    elif dhcp:
        raise NetworkError("Choose the office port to hand out addresses on it.")
    out["lan_dhcp"] = dhcp
    return out


def public_network(bn: dict | None) -> dict | None:
    """The portal's copy as the API shows it: PPPoE passwords as "set"."""
    if not bn:
        return None
    net = bn.get("network") or {}
    wan = [{**p, "pppoe_password": SET} if p.get("pppoe_password") else p for p in net.get("wan") or []]
    return {"network": {**net, "wan": wan}, "at": bn.get("at"), "by": bn.get("by")}


def set_network(conn: psycopg.Connection, node: dict, net: dict, actor: str) -> dict[str, Any]:
    """Set a box's ports from the portal. Raises NetworkError for a set-up the box would refuse."""
    clean = check_network(net, node.get("box"))
    prev = node.get("box_network") or {}
    # A password left as "set" keeps the one typed in the portal before, if the box has not had it yet.
    old = {p["iface"]: p for p in (prev.get("network") or {}).get("wan") or []}
    for p in clean["wan"]:
        if p.get("pppoe_password") == SET and old.get(p["iface"], {}).get("pppoe_password") not in (None, SET):
            p["pppoe_password"] = old[p["iface"]]["pppoe_password"]
    now = dt.datetime.now(dt.UTC)
    box_at = _at({"at": (node.get("box") or {}).get("network_at")})
    newest = max(box_at, _at(prev), _at(node.get("box_managed")))
    if newest >= now:
        now = newest + dt.timedelta(milliseconds=1)
    bn = {"network": clean, "at": now.isoformat(), "by": f"Connect portal ({actor.removeprefix('user:')})"}
    conn.execute("UPDATE nodes SET box_network = %s WHERE id = %s", (Jsonb(bn), node["id"]))
    audit.record(conn, actor, "box.network", node["name"], node["customer_id"], public_network(bn)["network"])
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, 'box_network', %s)",
        (now, node["customer_id"], node["id"], Jsonb({"by": bn["by"]})),
    )
    desired.refresh(conn, node["customer_id"])
    return bn


def portal_report(conn: psycopg.Connection, node: Any, portal: dict | None) -> None:
    """Once the box has handled the portal's set-up (kept, undone, refused or
    replaced), forget any PPPoE password typed for it: the box has its own copy."""
    if not portal or portal.get("state") == "trying":
        return
    row = conn.execute("SELECT box_network FROM nodes WHERE id = %s FOR UPDATE", (node.id,)).fetchone()
    bn = row["box_network"] if row else None
    if not bn or _at(bn) != _at(portal):
        return
    wan = (bn.get("network") or {}).get("wan") or []
    if not any(p.get("pppoe_password") not in (None, SET) for p in wan):
        return
    masked = public_network(bn)
    conn.execute("UPDATE nodes SET box_network = %s WHERE id = %s", (Jsonb(masked), node.id))
    # Earlier desired-state versions that carried it lose it too.
    conn.execute(
        """UPDATE desired_states SET body = jsonb_set(body, '{box,network,network}', %s)
           WHERE node_id = %s AND body #>> '{box,network,at}' = %s""",
        (Jsonb(masked["network"]), node.id, bn["at"]),
    )
    desired.refresh(conn, node.customer_id)
