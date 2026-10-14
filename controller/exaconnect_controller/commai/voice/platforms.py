"""Calling platforms joined to Jibsy (ADR 0056): Microsoft Teams, Zoom Phone, Webex
Calling, Google Voice, and the contact-centre platforms Genesys Cloud, NICE CXone and
Talkdesk.

Each of these lets an outside SIP edge carry its calls: Teams by Direct Routing, Zoom
and the contact centres by "bring your own carrier", Webex by a Local Gateway and
Google Voice by SIP Link. Jibsy's Kamailio is that edge, so people keep working in
the app they already use while Jibsy gives them numbers, carries their outside calls
on ExaCarib's carriers (or the organisation's own trunks, ADR 0044) and lets them call
Jibsy colleagues by extension.

A **link** joins one organisation to one platform. It is a `voice_carriers` row marked
with its platform (so Kamailio's dispatcher, allow-list and OPTIONS checks treat it
like a trunk; it never shows among the organisation's SIP trunks) plus a
`voice_platform_links` row with the SIP address the platform sends calls to:
`<organisation>-<platform>.<EXA_PLATFORM_SIP_DOMAIN>` (sip.exacarib.com). For Teams
that is Microsoft's "carrier hosting" model: the organisation adds the subdomain to
its Microsoft 365 tenant, ExaCarib publishes the TXT record Microsoft asks for, and
Kamailio presents the subdomain to Microsoft.

A **platform user** is someone (or, on a contact-centre platform, a queue or flow)
reached in the platform at one of the organisation's Jibsy numbers, optionally with
an extension Jibsy users can dial. Calls:

* to the user's number, or their extension from Jibsy: FreeSWITCH sends the call to
  Kamailio with X-Exa-Route set to the link's dispatcher set (free: it is the
  organisation's own call);
* from the user in the platform's app: Kamailio knows the link by the SIP address in
  the request (checked against the platform's signalling addresses) and tells
  FreeSWITCH in X-Exa-Link; extensions and the organisation's numbers ring in Jibsy,
  emergency numbers go out at once, and anything else is an outside call the
  controller authorises like any other (pbx.authorise_platform), billed per minute.

A link can be **simulated** until the platform account exists: calls to its users are
answered by Jibsy itself (an echo, so audio can be checked) and it can be set to
refuse calls or be down. A **live** link needs the platform's signalling addresses
(Microsoft's are built in) and a certificate for the SIP domain that the platform
trusts.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from . import billing, carriers, freeswitch
from .common import VoiceError, digits

SIP_DOMAIN_ENV = "EXA_PLATFORM_SIP_DOMAIN"
SIM_FAIL = ("", "refuse", "down")
HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
MAX_ADDRESSES = 16

# What each platform needs. "hosts" and "allow_ips" are built in where the platform
# publishes them for everyone; otherwise the platform gives them to the organisation
# when it switches on bring-your-own-carrier, and a voice admin enters them.
PLATFORMS: dict[str, dict] = {
    "teams": {
        "name": "Microsoft Teams",
        "method": "Direct Routing",
        "kind": "people",
        "account_label": "Teams sign-in",
        "account_hint": "name@company.com",
        "port": 5061,
        "srtp": True,
        "hosts": ["sip.pstnhub.microsoft.com", "sip2.pstnhub.microsoft.com", "sip3.pstnhub.microsoft.com"],
        "allow_ips": ["52.112.0.0/14", "52.122.0.0/15"],
        "needs": "Teams Phone licences (Microsoft 365 E5, or Teams Phone Standard) for the people who call.",
    },
    "zoom": {
        "name": "Zoom Phone",
        "method": "Bring your own carrier (BYOC)",
        "kind": "people",
        "account_label": "Zoom email",
        "account_hint": "name@company.com",
        "port": 5061,
        "srtp": True,
        "hosts": [],
        "allow_ips": [],
        "needs": "Zoom Phone licences, and BYOC switched on by Zoom for the account.",
    },
    "webex": {
        "name": "Webex Calling",
        "method": "Local Gateway (certificate based)",
        "kind": "people",
        "account_label": "Webex email",
        "account_hint": "name@company.com",
        "port": 5062,
        "srtp": True,
        "hosts": [],
        "allow_ips": [],
        "needs": "Webex Calling licences, and a Local Gateway trunk in Control Hub.",
    },
    "google": {
        "name": "Google Voice",
        "method": "SIP Link",
        "kind": "people",
        "account_label": "Google Workspace email",
        "account_hint": "name@company.com",
        "port": 5061,
        "srtp": True,
        "hosts": [],
        "allow_ips": [],
        "needs": "Google Voice Standard or Premier licences in Google Workspace.",
    },
    "genesys": {
        "name": "Genesys Cloud",
        "method": "BYOC Cloud",
        "kind": "destinations",
        "account_label": "Queue or call flow",
        "account_hint": "Sales queue",
        "port": 5061,
        "srtp": True,
        "hosts": [],
        "allow_ips": [],
        "needs": "A Genesys Cloud organisation with BYOC Cloud (an external trunk).",
    },
    "nice": {
        "name": "NICE CXone",
        "method": "Bring your own carrier (BYOC)",
        "kind": "destinations",
        "account_label": "Skill or call flow",
        "account_hint": "Support line",
        "port": 5061,
        "srtp": True,
        "hosts": [],
        "allow_ips": [],
        "needs": "A CXone account with BYOC set up by NICE.",
    },
    "talkdesk": {
        "name": "Talkdesk",
        "method": "Bring your own carrier (BYOC)",
        "kind": "destinations",
        "account_label": "Number name or flow",
        "account_hint": "Main line",
        "port": 5061,
        "srtp": True,
        "hosts": [],
        "allow_ips": [],
        "needs": "A Talkdesk account with BYOC set up by Talkdesk.",
    },
}


def sip_domain() -> str:
    return (os.environ.get(SIP_DOMAIN_ENV) or "sip.exacarib.com").strip(".").lower()


def catalogue() -> list[dict]:
    return [
        {
            "key": k,
            **{f: p[f] for f in ("name", "method", "kind", "account_label", "account_hint", "port", "needs")},
            "built_in_addresses": bool(p["allow_ips"]),
        }
        for k, p in PLATFORMS.items()
    ]


# ---- links ----------------------------------------------------------------------------------


def _uuid_or_404(v: Any, what: str) -> str:
    try:
        return str(uuid.UUID(str(v)))
    except ValueError as e:
        raise VoiceError(f"That {what} was not found.", 404) from e


def _link(conn, cid: Any, link_id: Any) -> dict:
    row = conn.execute(
        """SELECT l.*, c.key, c.adapter, c.enabled, c.outbound, c.inbound, c.health, c.last_options_at, c.set_id
           FROM voice_platform_links l JOIN voice_carriers c ON c.id = l.carrier_id
           WHERE l.id = %s AND l.customer_id = %s""",
        (_uuid_or_404(link_id, "calling platform"), cid),
    ).fetchone()
    if row is None:
        raise VoiceError("That calling platform was not found.", 404)
    return row


def _addresses(values: Any, *, ips: bool) -> list[str]:
    out: list[str] = []
    for v in values or []:
        v = str(v).strip().lower()
        if not v:
            continue
        if ips:
            try:
                net = ipaddress.ip_network(v, strict=False)
            except ValueError as e:
                raise VoiceError(f"{v} is not an IP address or network.", 422) from e
            if net.num_addresses > 256:
                raise VoiceError(f"{v} is too wide: list the platform's signalling addresses (at most a /24).", 422)
            v = str(net)
        else:
            try:
                ipaddress.ip_address(v)
            except ValueError:
                if not HOST.match(v) or "." not in v:
                    raise VoiceError(f"{v} is not a host name or IP address.", 422) from None
        if v not in out:
            out.append(v)
    if len(out) > MAX_ADDRESSES:
        raise VoiceError(f"List at most {MAX_ADDRESSES} addresses.", 422)
    return out


def _slug(conn, cid: Any) -> str:
    name = conn.execute("SELECT name FROM customers WHERE id = %s", (cid,)).fetchone()["name"]
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-") or "org"
    return f"{base}-{secrets.token_hex(2)}"


def link_out(conn, link: dict) -> dict:
    p = PLATFORMS[link["platform"]]
    o = link["outbound"] or {}
    live = link["adapter"] == "sip"
    users = list_users(conn, link["customer_id"], link["id"])
    hosts = o.get("hosts") or []
    ips = (link["inbound"] or {}).get("allow_ips", [])
    missing = []
    if live and not hosts:
        missing.append(f"{p['name']}'s SIP addresses")
    if live and not ips:
        missing.append(f"{p['name']}'s signalling IP addresses")
    if link["platform"] == "teams" and not link["dns_done"]:
        missing.append("the DNS record for your Microsoft 365 domain (ExaCarib publishes it)")
    return {
        "id": str(link["id"]),
        "key": link["key"],
        "platform": link["platform"],
        "name": p["name"],
        "method": p["method"],
        "kind": p["kind"],
        "account_label": p["account_label"],
        "mode": "live" if live else "simulated",
        "enabled": link["enabled"],
        "health": link["health"],
        "last_options_at": link["last_options_at"],
        "fqdn": link["fqdn"],
        "port": p["port"],
        "hosts": hosts,
        "allow_ips": ips,
        "built_in_addresses": bool(p["allow_ips"]),
        "max_channels": o.get("max_channels", 30),
        "sim_fail": o.get("sim_fail", "") if not live else "",
        "dns_txt": link["dns_txt"],
        "dns_done": link["dns_done"],
        "missing": missing,
        "users": users,
        "guide": guide(link, users),
    }


def list_links(conn: psycopg.Connection, cid: Any) -> list[dict]:
    rows = conn.execute(
        """SELECT l.*, c.key, c.adapter, c.enabled, c.outbound, c.inbound, c.health, c.last_options_at, c.set_id
           FROM voice_platform_links l JOIN voice_carriers c ON c.id = l.carrier_id
           WHERE l.customer_id = %s ORDER BY l.created_at""",
        (cid,),
    ).fetchall()
    return [link_out(conn, r) for r in rows]


def _carrier_fields(platform: str, fqdn: str, body: dict, old: dict | None) -> dict:
    """The carrier row's outbound and inbound settings for a link."""
    p = PLATFORMS[platform]
    o = (old or {}).get("outbound") or {}
    i = (old or {}).get("inbound") or {}
    mode = body.get("mode") or ("live" if old and old["adapter"] == "sip" else "simulated")
    if mode not in ("live", "simulated"):
        raise VoiceError("A link is live (calls go to the platform) or simulated (Jibsy answers them).", 422)
    if p["allow_ips"]:  # published for everyone: built in, not editable
        hosts, ips = list(p["hosts"]), list(p["allow_ips"])
    else:
        hosts = _addresses(body["hosts"] if body.get("hosts") is not None else o.get("hosts"), ips=False)
        ips = _addresses(body["allow_ips"] if body.get("allow_ips") is not None else i.get("allow_ips"), ips=True)
    try:
        channels = int(body.get("max_channels") or o.get("max_channels") or 30)
    except (TypeError, ValueError) as e:
        raise VoiceError("Channels are a whole number.", 422) from e
    if not 1 <= channels <= 10000:
        raise VoiceError("Channels are 1 to 10000.", 422)
    sim_fail = str(body["sim_fail"] if body.get("sim_fail") is not None else o.get("sim_fail", "")) or ""
    if sim_fail not in SIM_FAIL:
        raise VoiceError("A simulated link can refuse calls (refuse) or be down (down).", 422)
    if mode == "live":
        sim_fail = ""
    return {
        "adapter": "sip" if mode == "live" else "simulated",
        "outbound": {
            "host": hosts[0] if hosts else fqdn,
            "hosts": hosts,
            "port": p["port"],
            "transport": "tls",
            "max_channels": channels,
            "fqdn": fqdn,
            **({"sim_fail": sim_fail} if sim_fail else {}),
        },
        "inbound": {"allow_ips": ips},
        "sim_fail": sim_fail,
    }


def _health(old: dict | None, sim_fail: str, adapter: str) -> tuple[str, int]:
    if sim_fail == "down":
        return "down", carriers.DOWN_AFTER
    if old is None or old["adapter"] != adapter or (old["outbound"] or {}).get("sim_fail") == "down":
        return "unknown", 0
    return old["health"], old["consecutive_failures"]


def _rerender(conn, cid: Any) -> None:
    carriers.ensure_health_loop(conn)
    carriers.render_kamailio(conn)
    freeswitch.render_business(conn, cid)


def add_link(conn: psycopg.Connection, cid: Any, body: dict, actor: str) -> dict:
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise VoiceError(f"Choose a platform: {', '.join(p['name'] for p in PLATFORMS.values())}.", 422)
    p = PLATFORMS[platform]
    if conn.execute(
        "SELECT 1 FROM voice_platform_links WHERE customer_id = %s AND platform = %s", (cid, platform)
    ).fetchone():
        raise VoiceError(f"{p['name']} is already joined to Jibsy.", 409)
    fqdn = f"{_slug(conn, cid)}-{platform}.{sip_domain()}"
    v = _carrier_fields(platform, fqdn, body, None)
    health, fails = _health(None, v["sim_fail"], v["adapter"])
    key = f"org-{str(cid).replace('-', '')[:8]}-{uuid.uuid4().hex[:8]}"
    car = conn.execute(
        """INSERT INTO voice_carriers (key, name, adapter, outbound, inbound, enabled, customer_id, created_by,
             health, consecutive_failures, platform)
           VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s) RETURNING id""",
        (key, p["name"], v["adapter"], Jsonb(v["outbound"]), Jsonb(v["inbound"]), cid, actor, health, fails, platform),
    ).fetchone()
    row = conn.execute(
        """INSERT INTO voice_platform_links (customer_id, platform, carrier_id, fqdn, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (cid, platform, car["id"], fqdn, actor),
    ).fetchone()
    audit.record(conn, actor, "commai.voice.platform_add", key, cid, {"platform": platform, "fqdn": fqdn})
    _rerender(conn, cid)
    return link_out(conn, _link(conn, cid, row["id"]))


def change_link(conn: psycopg.Connection, cid: Any, link_id: Any, body: dict, actor: str) -> dict:
    old = _link(conn, cid, link_id)
    full = conn.execute("SELECT * FROM voice_carriers WHERE id = %s", (old["carrier_id"],)).fetchone()
    v = _carrier_fields(old["platform"], old["fqdn"], body, full)
    health, fails = _health(full, v["sim_fail"], v["adapter"])
    enabled = bool(body["enabled"]) if body.get("enabled") is not None else full["enabled"]
    conn.execute(
        """UPDATE voice_carriers SET adapter = %s, outbound = %s, inbound = %s, enabled = %s, health = %s,
             consecutive_failures = %s, updated_at = now() WHERE id = %s""",
        (v["adapter"], Jsonb(v["outbound"]), Jsonb(v["inbound"]), enabled, health, fails, full["id"]),
    )
    if body.get("dns_txt") is not None:
        txt = str(body["dns_txt"]).strip()
        if txt and not re.fullmatch(r"[A-Za-z0-9=+/_.:-]{4,200}", txt):
            raise VoiceError("Paste the TXT value exactly as Microsoft 365 shows it, for example ms=ms12345678.", 422)
        conn.execute(
            "UPDATE voice_platform_links SET dns_txt = %s, dns_done = false, updated_at = now() WHERE id = %s",
            (txt, old["id"]),
        )
    audit.record(
        conn,
        actor,
        "commai.voice.platform_change",
        old["key"],
        cid,
        {"mode": "live" if v["adapter"] == "sip" else "simulated", "enabled": enabled, "sim_fail": v["sim_fail"]},
    )
    _rerender(conn, cid)
    return link_out(conn, _link(conn, cid, old["id"]))


def delete_link(conn: psycopg.Connection, cid: Any, link_id: Any, actor: str) -> None:
    link = _link(conn, cid, link_id)
    for u in conn.execute("SELECT id FROM voice_platform_users WHERE link_id = %s", (link["id"],)).fetchall():
        _drop_user(conn, cid, u["id"])
    conn.execute("DELETE FROM voice_carriers WHERE id = %s", (link["carrier_id"],))  # the link goes with it
    audit.record(conn, actor, "commai.voice.platform_delete", link["key"], cid, {"platform": link["platform"]})
    _rerender(conn, cid)


# ---- DNS for Teams (ExaCarib staff) ----------------------------------------------------------


def dns_requests(conn: psycopg.Connection) -> list[dict]:
    """Microsoft 365 TXT records ExaCarib must publish, so organisations can verify their subdomain."""
    return [
        {**r, "id": str(r["id"]), "customer_id": str(r["customer_id"])}
        for r in conn.execute(
            """SELECT l.id, l.customer_id, c.name AS organisation, l.fqdn, l.dns_txt, l.dns_done, l.updated_at
               FROM voice_platform_links l JOIN customers c ON c.id = l.customer_id
               WHERE l.platform = 'teams' AND l.dns_txt <> '' ORDER BY l.dns_done, l.updated_at"""
        ).fetchall()
    ]


def mark_dns(conn: psycopg.Connection, link_id: Any, done: bool, actor: str) -> dict:
    row = conn.execute(
        "UPDATE voice_platform_links SET dns_done = %s, updated_at = now() WHERE id = %s RETURNING *",
        (done, _uuid_or_404(link_id, "calling platform")),
    ).fetchone()
    if row is None:
        raise VoiceError("That calling platform was not found.", 404)
    audit.record(conn, actor, "commai.voice.platform_dns", row["fqdn"], row["customer_id"], {"done": done})
    return {"id": str(row["id"]), "dns_done": row["dns_done"]}


# ---- people and destinations on a platform ---------------------------------------------------


def list_users(conn, cid: Any, link_id: Any = None) -> list[dict]:
    rows = conn.execute(
        """SELECT u.*, n.e164, n.target_type, n.target_id, l.platform
           FROM voice_platform_users u JOIN voice_numbers n ON n.id = u.number_id
           JOIN voice_platform_links l ON l.id = u.link_id
           WHERE u.customer_id = %s AND (%s::uuid IS NULL OR u.link_id = %s::uuid)
           ORDER BY lower(u.name)""",
        (cid, link_id, link_id),
    ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "link_id": str(r["link_id"]),
            "platform": r["platform"],
            "name": r["name"],
            "account": r["account"],
            "number_id": str(r["number_id"]),
            "e164": r["e164"],
            "extension": r["extension"],
            "enabled": r["enabled"],
            # Someone moved the number in Phone system: it no longer rings this person.
            "number_rings_here": r["target_type"] == "platform_user" and str(r["target_id"]) == str(r["id"]),
        }
        for r in rows
    ]


def free_numbers(conn, cid: Any) -> list[dict]:
    """The organisation's numbers not yet given to someone on a platform."""
    return [
        {**r, "id": str(r["id"])}
        for r in conn.execute(
            """SELECT n.id, n.e164, n.target_type FROM voice_numbers n
               WHERE n.customer_id = %s AND n.status = 'active'
                 AND NOT EXISTS (SELECT 1 FROM voice_platform_users u WHERE u.number_id = n.id)
               ORDER BY n.e164""",
            (cid,),
        ).fetchall()
    ]


def _user(conn, cid: Any, user_id: Any) -> dict:
    row = conn.execute(
        "SELECT * FROM voice_platform_users WHERE id = %s AND customer_id = %s",
        (_uuid_or_404(user_id, "person"), cid),
    ).fetchone()
    if row is None:
        raise VoiceError("That person was not found.", 404)
    return row


def _check_user(conn, cid: Any, link: dict, body: dict, old: dict | None) -> dict:
    from .config import OpError, _check_ext

    p = PLATFORMS[link["platform"]]

    def get(k: str) -> Any:
        return body[k] if body.get(k) is not None else (old or {}).get(k)

    name = " ".join(str(get("name") or "").split())[:120]
    if not name:
        raise VoiceError("Give a name.", 422)
    account = str(get("account") or "").strip()[:200]
    if not account:
        raise VoiceError(f"Give their {p['account_label'].lower()}.", 422)
    if p["kind"] == "people" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", account):
        raise VoiceError(f"Their {p['account_label'].lower()} looks like name@company.com.", 422)
    clash = conn.execute(
        "SELECT 1 FROM voice_platform_users WHERE link_id = %s AND lower(account) = lower(%s) AND id <> %s",
        (link["id"], account, old["id"] if old else uuid.uuid4()),
    ).fetchone()
    if clash:
        raise VoiceError(f"{account} is already on {p['name']}.", 409)
    number_id = get("number_id")
    num = conn.execute(
        "SELECT * FROM voice_numbers WHERE id = %s AND customer_id = %s AND status = 'active'",
        (_uuid_or_404(number_id, "number"), cid),
    ).fetchone()
    if num is None:
        raise VoiceError("Choose one of your numbers. Add numbers under Phone > Numbers or on a SIP trunk.", 422)
    taken = conn.execute(
        "SELECT name FROM voice_platform_users WHERE number_id = %s AND id <> %s",
        (num["id"], old["id"] if old else uuid.uuid4()),
    ).fetchone()
    if taken:
        raise VoiceError(f"{num['e164']} already belongs to {taken['name']}.", 409)
    ext = body.get("extension") if "extension" in body else (old or {}).get("extension")
    ext = str(ext or "").strip() or None
    if ext and ext != (old or {}).get("extension"):
        try:
            _check_ext(conn, cid, ext)
        except OpError as e:
            raise VoiceError(str(e), 422) from e
    enabled = bool(body["enabled"]) if body.get("enabled") is not None else (old or {}).get("enabled", True)
    return {"name": name, "account": account, "number": num, "extension": ext, "enabled": enabled}


def save_user(conn: psycopg.Connection, cid: Any, body: dict, actor: str, user_id: Any = None) -> dict:
    old = _user(conn, cid, user_id) if user_id else None
    link = _link(conn, cid, old["link_id"] if old else body.get("link_id"))
    v = _check_user(conn, cid, link, body, old)
    if old is None:
        row = conn.execute(
            """INSERT INTO voice_platform_users (customer_id, link_id, name, account, number_id, extension, enabled,
                 created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (cid, link["id"], v["name"], v["account"], v["number"]["id"], v["extension"], v["enabled"], actor),
        ).fetchone()
    else:
        if str(old["number_id"]) != str(v["number"]["id"]):
            _release_number(conn, old)
        row = conn.execute(
            """UPDATE voice_platform_users SET name = %s, account = %s, number_id = %s, extension = %s,
                 enabled = %s, updated_at = now() WHERE id = %s RETURNING *""",
            (v["name"], v["account"], v["number"]["id"], v["extension"], v["enabled"], old["id"]),
        ).fetchone()
    # The number now rings this person on the platform.
    conn.execute(
        "UPDATE voice_numbers SET target_type = 'platform_user', target_id = %s WHERE id = %s",
        (row["id"], v["number"]["id"]),
    )
    audit.record(
        conn,
        actor,
        "commai.voice.platform_user_" + ("add" if old is None else "change"),
        v["number"]["e164"],
        cid,
        {"platform": link["platform"], "extension": v["extension"], "enabled": v["enabled"]},
    )
    freeswitch.render_business(conn, cid)
    return next(u for u in list_users(conn, cid, link["id"]) if u["id"] == str(row["id"]))


def _release_number(conn, user: dict) -> None:
    conn.execute(
        """UPDATE voice_numbers SET target_type = 'none', target_id = NULL
           WHERE id = %s AND target_type = 'platform_user' AND target_id = %s""",
        (user["number_id"], user["id"]),
    )


def _drop_user(conn, cid: Any, user_id: Any) -> dict:
    u = _user(conn, cid, user_id)
    _release_number(conn, u)
    conn.execute("DELETE FROM voice_platform_users WHERE id = %s", (u["id"],))
    return u


def delete_user(conn: psycopg.Connection, cid: Any, user_id: Any, actor: str) -> None:
    u = _drop_user(conn, cid, user_id)
    audit.record(conn, actor, "commai.voice.platform_user_delete", u["account"], cid, {})
    freeswitch.render_business(conn, cid)


# ---- set-up guides ---------------------------------------------------------------------------


def guide(link: dict, users: list[dict]) -> dict:
    """The steps the organisation follows in the platform's own admin, filled in for it."""
    p = PLATFORMS[link["platform"]]
    fqdn, port = link["fqdn"], p["port"]
    o = link["outbound"] or {}
    channels = o.get("max_channels", 30)
    sbc = f"{fqdn}, port {port}, TLS, with SRTP for audio"
    script = ""
    if link["platform"] == "teams":
        steps = [
            f"In the Microsoft 365 admin centre, add the domain {fqdn} (Settings > Domains). Paste the TXT value "
            "Microsoft shows into Jibsy below; ExaCarib publishes it and you then press Verify.",
            f"Make one user with a {fqdn} address and a Teams licence, so Microsoft activates the domain. "
            "You can remove the licence once the steps below are done.",
            "Run the PowerShell below as a Teams admin. It adds Jibsy as your Direct Routing gateway, "
            "routes outside calls to it and gives each person their Jibsy number.",
            "Switch the link to live in Jibsy. Microsoft shows the gateway as active within a few minutes.",
        ]
        lines = [
            "Connect-MicrosoftTeams",
            f'New-CsOnlinePSTNGateway -Fqdn "{fqdn}" -SipSignalingPort {port} '
            f"-MaxConcurrentSessions {channels} -Enabled $true",
            'Set-CsOnlinePstnUsage -Identity Global -Usage @{Add="Jibsy"}',
            f'New-CsOnlineVoiceRoute -Identity "Jibsy" -NumberPattern ".*" -OnlinePstnGatewayList "{fqdn}" '
            '-Priority 1 -OnlinePstnUsages "Jibsy"',
            'New-CsOnlineVoiceRoutingPolicy -Identity "Jibsy" -OnlinePstnUsages "Jibsy"',
            "# Short numbers reach Jibsy extensions (for example 201).",
            'New-CsTenantDialPlan -Identity "Jibsy" -NormalizationRules @{Add=(New-CsVoiceNormalizationRule '
            "-Parent Global -Name \"Jibsy extensions\" -Pattern '^(\\d{2,6})$' -Translation '+$1' -InMemory)}",
        ]
        for u in users:
            lines += [
                f'Set-CsPhoneNumberAssignment -Identity "{u["account"]}" -PhoneNumber "{u["e164"]}" '
                "-PhoneNumberType DirectRouting",
                f'Grant-CsOnlineVoiceRoutingPolicy -Identity "{u["account"]}" -PolicyName "Jibsy"',
                f'Grant-CsTenantDialPlan -Identity "{u["account"]}" -PolicyName "Jibsy"',
            ]
        if not users:
            lines.append("# Add people in Jibsy first: their number lines appear here.")
        script = "\n".join(lines)
    elif link["platform"] == "zoom":
        steps = [
            "Ask Zoom to switch on BYOC (bring your own carrier) for your Zoom Phone account.",
            f"Give Zoom Jibsy's SIP address for the BYOC trunk: {sbc}.",
            "Zoom gives you its SIP addresses and signalling IP addresses for your region. Enter them in Jibsy below.",
            "In the Zoom admin portal, add each person's Jibsy number as a BYOC number (Phone System Management > "
            "Phone Numbers > BYOC) and assign it to them.",
            "Switch the link to live in Jibsy.",
        ]
    elif link["platform"] == "webex":
        steps = [
            "In Control Hub, go to Calling > Call Routing > Trunk and add a certificate-based trunk for "
            f"a Local Gateway with the address {fqdn}, port {port}.",
            "Enter the Webex Calling SIP addresses and signalling IP addresses for your region below.",
            "Make a route group with the trunk and choose it for PSTN calls at each location.",
            "Add each person's Jibsy number to the location and assign it to them.",
            "Switch the link to live in Jibsy. Cisco supports its own Local Gateway; ExaCarib's works the same way.",
        ]
    elif link["platform"] == "google":
        steps = [
            f"In the Google Admin console, go to Apps > Google Workspace > Google Voice > SIP Link and add a "
            f"gateway with Jibsy's address: {sbc}.",
            "Enter Google's SIP addresses and signalling IP addresses from the SIP Link page below.",
            "Add each person's Jibsy number to the SIP Link and assign it to them in Google Voice.",
            "Switch the link to live in Jibsy.",
        ]
    else:
        steps = [
            f"Ask {p['name']} to set up {p['method']} with Jibsy's SIP address: {sbc}.",
            f"{p['name']} gives you its SIP addresses and signalling IP addresses. Enter them in Jibsy below.",
            f"Add each Jibsy number below as a number in {p['name']} and point it at the queue or call flow.",
            "Switch the link to live in Jibsy.",
        ]
    return {
        "needs": p["needs"],
        "sip_address": fqdn,
        "port": port,
        "steps": steps,
        "script": script,
    }


# ---- test calls ------------------------------------------------------------------------------


def test_call(conn: psycopg.Connection, cid: Any, link_id: Any, body: dict) -> dict:
    """What happens to a call between Jibsy and someone on the platform, without a phone.

    direction "to": a Jibsy caller rings the person. "from": the person calls a
    number or extension from the platform's app."""
    link = _link(conn, cid, link_id)
    p = PLATFORMS[link["platform"]]
    u = _user(conn, cid, body.get("user_id"))
    if str(u["link_id"]) != str(link["id"]):
        raise VoiceError("That person is on another platform.", 422)
    num = conn.execute("SELECT e164 FROM voice_numbers WHERE id = %s", (u["number_id"],)).fetchone()["e164"]
    steps: list[str] = []
    car = conn.execute("SELECT * FROM voice_carriers WHERE id = %s", (link["carrier_id"],)).fetchone()
    if body.get("direction") == "to":

        def no(reason: str) -> dict:
            return {"ok": False, "steps": steps + [reason], "reason": reason}

        steps.append(f"Jibsy rings {u['name']} at {num} on {p['name']}.")
        if not u["enabled"]:
            return no(f"{u['name']} is switched off in Jibsy.")
        if not car["enabled"]:
            return no(f"The {p['name']} link is switched off.")
        if car["health"] == "down":
            return no(f"{p['name']} is not answering (no answer to OPTIONS).")
        if car["adapter"] == "simulated":
            from . import provider as providers

            res = providers.get().place_call(conn, car, num)
            if not res["ok"]:
                return no(f"{p['name']} (simulated) refused the call: {res['detail']}")
            steps.append(f"{p['name']} (simulated) answered: Jibsy plays the call back, so you hear yourself.")
        else:
            steps.append(f"Kamailio sends the call to {', '.join((car['outbound'] or {}).get('hosts') or [])}.")
        return {"ok": True, "steps": steps, "reason": ""}

    to = str(body.get("to") or "").strip()
    steps.append(f"{u['name']} calls {to} from {p['name']}.")
    if not u["enabled"] or not car["enabled"]:
        reason = f"{u['name'] if not u['enabled'] else 'The ' + p['name'] + ' link'} is switched off in Jibsy."
        return {"ok": False, "steps": steps + [reason], "reason": reason}
    if re.fullmatch(r"\d{2,6}", to):
        target = ring_target(conn, cid, to)
        if target is None:
            reason = f"There is no extension {to} in Jibsy."
            return {"ok": False, "steps": steps + [reason], "reason": reason}
        steps.append(f"Extension {to} rings {target} in Jibsy. No charge: it is your own call.")
        return {"ok": True, "steps": steps, "reason": ""}
    from .config import OpError, norm_e164

    try:
        number = norm_e164(to)
    except OpError as e:
        raise VoiceError(str(e), 422) from e
    own = conn.execute(
        "SELECT 1 FROM voice_numbers WHERE customer_id = %s AND e164 = %s AND status = 'active'", (cid, number)
    ).fetchone()
    if own:
        steps.append(f"{number} is one of your numbers, so it rings in Jibsy. No charge.")
        return {"ok": True, "steps": steps, "reason": ""}
    verdict = billing.authorise(conn, cid, number, from_number=num)
    if not verdict["allowed"]:
        return {"ok": False, "steps": steps + [verdict["reason"]], "reason": verdict["reason"]}
    if verdict.get("emergency"):
        steps.append("Emergency call: it goes out at once on ExaCarib's carriers.")
        return {"ok": True, "steps": steps, "reason": ""}
    route = verdict.get("route")
    if not route:
        steps.append(f"ExaCarib's standard provider carries it, showing {num}. Billed per minute.")
        return {"ok": True, "steps": steps, "reason": ""}
    names = {
        r["key"]: r["name"]
        for r in conn.execute("SELECT key, name FROM voice_carriers WHERE key = ANY(%s)", (route,)).fetchall()
    }
    res = carriers.connect(conn, cid, f"plat-{uuid.uuid4().hex[:12]}", number, route)
    for a in res["attempts"]:
        steps.append(f"{names.get(a['carrier'], a['carrier'])}: {a['detail']}")
    if not res["ok"]:
        return {"ok": False, "steps": steps, "reason": res["reason"]}
    steps.append(f"The call shows {num}. Billed per minute like any outside call.")
    return {"ok": True, "steps": steps, "reason": ""}


def ring_target(conn, cid: Any, ext: str) -> str | None:
    """Who an extension rings in Jibsy, by name, or None."""
    for table, label in (
        ("voice_users", "{}"),
        ("voice_ring_groups", "ring group {}"),
        ("voice_queues", "queue {}"),
        ("voice_menus", "menu {}"),
    ):
        cond = "AND status = 'active'" if table == "voice_users" else ""
        r = conn.execute(
            f"SELECT name FROM {table} WHERE customer_id = %s AND extension = %s {cond}", (cid, ext)
        ).fetchone()
        if r:
            return label.format(r["name"])
    r = conn.execute(
        """SELECT u.name, l.platform FROM voice_platform_users u JOIN voice_platform_links l ON l.id = u.link_id
           WHERE u.customer_id = %s AND u.extension = %s AND u.enabled""",
        (cid, ext),
    ).fetchone()
    if r:
        return f"{r['name']} on {PLATFORMS[r['platform']]['name']}"
    return None


# ---- the PBX: calls from a platform ----------------------------------------------------------

KEY = re.compile(r"^org-[0-9a-f]{8}-[0-9a-f]{8}$")


def caller(conn, cid: Any, link_key: str, from_number: str) -> dict | None:
    """The platform user making a call: on this organisation's live link, by their number."""
    if not KEY.match(link_key):
        return None
    return conn.execute(
        """SELECT u.*, n.e164 FROM voice_platform_users u
           JOIN voice_platform_links l ON l.id = u.link_id
           JOIN voice_carriers c ON c.id = l.carrier_id
           JOIN voice_numbers n ON n.id = u.number_id
           WHERE u.customer_id = %s AND c.key = %s AND c.enabled AND u.enabled AND n.e164 = %s""",
        (cid, link_key, "+" + digits(from_number)),
    ).fetchone()


# ---- what Kamailio and FreeSWITCH need ---------------------------------------------------------


def kamailio_list(conn: psycopg.Connection) -> str:
    """platforms.list, which Kamailio's watcher loads into its `platforms` htable. For each live link:
    "<SIP address> <platform>:<carrier key>" (a call from the platform's signalling addresses to that
    SIP address is the organisation's: X-Exa-Link) and "set:<dispatcher set> <SIP address>" (a call
    to the platform on that set presents the organisation's SIP address)."""
    lines = ["# Rendered by the ExaCarib controller (calling platforms). key value"]
    for r in conn.execute(
        """SELECT l.fqdn, l.platform, c.key, c.set_id FROM voice_platform_links l
           JOIN voice_carriers c ON c.id = l.carrier_id
           WHERE c.enabled AND c.adapter = 'sip' ORDER BY l.fqdn"""
    ).fetchall():
        lines.append(f"{r['fqdn']} {r['platform']}:{r['key']}")
        lines.append(f"set:{r['set_id']} {r['fqdn']}")
    return "\n".join(lines) + "\n"


def dial_data(conn: psycopg.Connection, cid: Any) -> dict:
    """The organisation's links and platform users for its dial plan (freeswitch.gather)."""
    links = conn.execute(
        """SELECT l.platform, c.key, c.adapter, c.enabled, c.set_id, c.outbound
           FROM voice_platform_links l JOIN voice_carriers c ON c.id = l.carrier_id
           WHERE l.customer_id = %s ORDER BY c.key""",
        (cid,),
    ).fetchall()
    users = conn.execute(
        """SELECT u.id::text AS id, u.name, u.extension, u.enabled, n.e164, c.key AS link
           FROM voice_platform_users u JOIN voice_numbers n ON n.id = u.number_id
           JOIN voice_platform_links l ON l.id = u.link_id JOIN voice_carriers c ON c.id = l.carrier_id
           WHERE u.customer_id = %s ORDER BY u.id""",
        (cid,),
    ).fetchall()
    return {
        "platform_links": [
            {
                "key": r["key"],
                "platform": r["platform"],
                "live": r["adapter"] == "sip",
                "enabled": r["enabled"],
                "set_id": r["set_id"],
                "srtp": PLATFORMS[r["platform"]]["srtp"],
                "sim_fail": (r["outbound"] or {}).get("sim_fail", ""),
            }
            for r in links
        ],
        "platform_users": [dict(u) for u in users],
    }
