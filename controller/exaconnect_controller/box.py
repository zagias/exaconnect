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
import json
import logging
import os
import re
import time
import urllib.request
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import audit, desired

log = logging.getLogger(__name__)

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
    sw = node.get("box_software")
    if sw:
        out["software"] = {"version": sw["version"], "at": sw["at"], "by": sw.get("by") or ""}
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
    out = {"network": {**net, "wan": wan}, "at": bn.get("at"), "by": bn.get("by")}
    if bn.get("template"):
        out["template"] = bn["template"]
    return out


def set_network(
    conn: psycopg.Connection,
    node: dict,
    net: dict,
    actor: str,
    template: dict | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    """Set a box's ports from the portal, directly or through a template.
    Raises NetworkError for a set-up the box would refuse. Setting them directly
    takes the box off any template it followed."""
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
    who = actor.removeprefix("user:")
    bn: dict[str, Any] = {"network": clean, "at": now.isoformat(), "by": f"Connect portal ({who})"}
    if template:
        bn["by"] = f"Template {template['name']} ({who})"
        bn["template"] = {"id": str(template["id"]), "name": template["name"]}
    conn.execute(
        "UPDATE nodes SET box_network = %s, box_template_id = %s WHERE id = %s",
        (Jsonb(bn), template["id"] if template else None, node["id"]),
    )
    audit.record(
        conn,
        actor,
        "box.network",
        node["name"],
        node["customer_id"],
        {**public_network(bn)["network"], **({"template": template["name"]} if template else {})},
    )
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, 'box_network', %s)",
        (now, node["customer_id"], node["id"], Jsonb({"by": bn["by"]})),
    )
    if refresh:
        desired.refresh(conn, node["customer_id"])
    return bn


# Templates (ADR 0053): one port set-up for many boxes of an organisation. A box
# that follows a template gets the template's ports, and again whenever the
# template changes. PPPoE sign-ins differ per location, so a template cannot hold
# one; its office port can take the address "auto": the first address of each
# location's own office network (the site's first LAN prefix).

AUTO = "auto"


def check_template(net: dict) -> dict:
    """A template's set-up: the box checks without a box's port list, no PPPoE."""
    for p in (net.get("wan") or []) + ([net["lan"]] if net.get("lan") else []):
        if p.get("mode") == "pppoe":
            raise NetworkError(
                "PPPoE sign-ins differ per location: set them on each box, or choose Leave as it is in the template."
            )
    work = json.loads(json.dumps(net))
    auto = bool(work.get("lan")) and work["lan"].get("address") == AUTO
    if auto:
        work["lan"]["address"] = "10.0.0.1/24"  # any address: each location's own goes in at apply time
    clean = check_network(work, None)
    if auto:
        clean["lan"]["address"] = AUTO
    return clean


def for_site(net: dict, lan_prefixes: list) -> dict:
    """A template's set-up made concrete for one location."""
    out = json.loads(json.dumps(net))
    lan = out.get("lan")
    if lan and lan.get("address") == AUTO:
        if not lan_prefixes:
            raise NetworkError('This location has no office network (LAN prefix) for the address "auto".')
        n = ipaddress.ip_network(str(lan_prefixes[0]), strict=False)
        if n.version != 4 or n.num_addresses < 4:
            raise NetworkError(f'This location\'s office network {n} is too small for the address "auto".')
        lan["address"] = f"{next(n.hosts())}/{n.prefixlen}"
    return out


def apply_template(conn: psycopg.Connection, template: dict, site_ids: list[str], actor: str) -> list[dict]:
    """Put a template on boxes and make them follow it. Returns one result per site."""
    results = []
    touched = False
    for sid in site_ids:
        row = conn.execute(
            """SELECT n.id, n.name, n.customer_id, n.box, n.box_managed, n.box_network, s.name AS site, s.lan_prefixes
               FROM sites s LEFT JOIN nodes n ON n.site_id = s.id
               WHERE s.id = %s AND s.customer_id = %s FOR UPDATE OF s""",
            (sid, template["customer_id"]),
        ).fetchone()
        if row is None:
            results.append({"site_id": sid, "ok": False, "error": "No such location in this organisation."})
            continue
        res: dict[str, Any] = {"site_id": sid, "site": row["site"], "ok": False}
        if row["id"] is None or not (row["box"] or {}).get("console"):
            res["error"] = "No ExaCarib box at this location."
        elif managed(row)["mode"] == "site":
            res["error"] = "This box is managed on site. Switch Managed from to Cloud or both first."
        else:
            try:
                set_network(
                    conn, row, for_site(template["network"], row["lan_prefixes"]), actor, template, refresh=False
                )
                res["ok"] = True
                touched = True
            except NetworkError as e:
                res["error"] = str(e)
        results.append(res)
    if touched:
        desired.refresh(conn, template["customer_id"])
    return results


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


# Fleet software updates (ADR 0054). The portal asks boxes for a box software
# version (``nodes.box_software`` = {version, at, by}, down in desired state as
# ``box.software``). A box installed from the ExaCarib box image installs it with
# its A/B update (ADR 0052): it restarts into the new software and keeps it only
# if it comes up healthy, otherwise it goes back to the old one by itself. It
# reports what became of the request as ``box.software_portal``. Going back to an
# earlier version fleet-wide is asking for that version.

VERSION = re.compile(r"^[0-9][0-9A-Za-z.+~-]{0,40}$")
RELEASES_REPO = os.environ.get("EXA_BOX_RELEASES", "zagias/exaconnect")
_releases: dict[str, Any] = {"at": 0.0, "versions": None}


def releases(fetch=None, feed=None, exists=None) -> list[str]:
    """Box software versions with an update file, newest first (GitHub releases
    box-v<version>), cached for ten minutes. GitHub's API allows few calls without
    a token from one address, so when it refuses, the releases feed (no limit)
    gives the versions and each one's checksum file is read to see it has an update file."""
    if _releases["versions"] is not None and time.monotonic() - _releases["at"] < 600:
        return _releases["versions"]

    def get(u: str, accept: str = "*/*") -> bytes:
        req = urllib.request.Request(u, headers={"Accept": accept})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (fixed https addresses)
            return r.read(4 << 20)

    if fetch is None:

        def fetch(u: str) -> list:
            return json.loads(get(u, "application/vnd.github+json"))

    out: list[str] = []
    try:
        for r in fetch(f"https://api.github.com/repos/{RELEASES_REPO}/releases?per_page=30"):
            v = (r.get("tag_name") or "").removeprefix("box-v")
            if v == r.get("tag_name") or not VERSION.match(v) or r.get("draft"):
                continue
            if any((a.get("name") or "").endswith("-amd64.root.tar.zst") for a in r.get("assets") or []):
                out.append(v)
    except Exception as e:  # noqa: BLE001 (rate limited or unreachable: try the feed)
        log.warning("GitHub releases API failed (%s); reading the releases feed", e)
        if feed is None:

            def feed() -> str:
                return get(f"https://github.com/{RELEASES_REPO}/releases.atom").decode()

        if exists is None:

            def exists(v: str) -> bool:
                u = f"https://github.com/{RELEASES_REPO}/releases/download/box-v{v}/exacarib-box-{v}-amd64.sha256"
                try:
                    return b".root.tar.zst" in get(u)
                except Exception:  # noqa: BLE001
                    return False

        tags = re.findall(r"/releases/tag/box-v([0-9][0-9A-Za-z.+~-]{0,40})", feed())
        seen: list[str] = []
        for v in tags:
            if v not in seen and VERSION.match(v):
                seen.append(v)
        out = [v for v in seen[:10] if exists(v)]
    _releases.update(at=time.monotonic(), versions=out)
    return out


def set_software(conn: psycopg.Connection, node: dict, version: str, actor: str) -> dict[str, Any]:
    """Ask a box for a software version. The caller refreshes desired state."""
    now = dt.datetime.now(dt.UTC)
    newest = max(_at(node.get("box_software")), _at(node.get("box_managed")))
    if newest >= now:
        now = newest + dt.timedelta(milliseconds=1)
    who = actor.removeprefix("user:")
    sw = {"version": version, "at": now.isoformat(), "by": f"Connect portal ({who})"}
    conn.execute("UPDATE nodes SET box_software = %s WHERE id = %s", (Jsonb(sw), node["id"]))
    audit.record(conn, actor, "box.software", node["name"], node["customer_id"], {"version": version})
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, 'box_software', %s)",
        (now, node["customer_id"], node["id"], Jsonb({"by": sw["by"], "version": version})),
    )
    return sw


def software_state(node: dict) -> dict[str, Any]:
    """A box's software as the portal shows it: what runs, what was asked, what became of it."""
    box = node.get("box") or {}
    sw = box.get("software") or {}
    asked = node.get("box_software")
    rep = box.get("software_portal") or {}
    state = None
    if asked:
        state = rep.get("state") if rep.get("at") and _at(rep) == _at(asked) else "sent"
    return {
        "ab": bool(sw.get("ab")),
        "version": sw.get("version") or "",
        "slot": sw.get("slot") or "",
        "trial": bool(sw.get("trial")),
        "installing": bool(sw.get("running")),
        "asked": {"version": asked["version"], "at": asked["at"], "by": asked.get("by")} if asked else None,
        "state": state,
        "detail": rep.get("detail") if state and state != "sent" else None,
    }
