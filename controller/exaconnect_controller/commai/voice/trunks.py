"""Each organisation's own SIP trunks, a primary and secondary per location (ADR 0044).

An organisation can bring its own SIP trunks (from its own carrier contract)
next to ExaCarib's carriers. A trunk is a `voice_carriers` row with the
organisation's customer_id, so Kamailio's trunk and allow-lists, OPTIONS health
checks and per-call failover treat it like any carrier; it needs no go-live
switch and has no ExaCarib rate sheet, and its calls are billed by the
organisation's carrier, not by the minute (billing.rate_call).

Outgoing calls follow the caller's location (their phone site's location in
Organisation > Locations), trying in order:

1. the location's primary trunk;
2. the location's secondary trunk;
3. the organisation's default trunk;
4. ExaCarib's carriers, unless the organisation has switched that off.

A trunk that is switched off or down (no answer to OPTIONS) is skipped; a
trunk that refuses the call passes it to the next (carriers.connect, or
Kamailio's failover on real calls). An organisation with no trunks of its own
routes exactly as before.

Numbers that arrive on a trunk (the organisation's carrier owns them) are
added here; every number belongs to a location through its phone site.

Trunks authenticate by IP address (the trunk's host and the carrier's
signalling addresses on Kamailio's allow-list); trunks that need a SIP
registration or digest password are not supported yet. A simulated trunk can be
set to refuse calls or to be down, to try failover before a real carrier
exists.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from . import carriers, emergency, freeswitch
from .common import VoiceError, digits

SIM_FAIL = ("", "refuse", "down")
HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


# ---- trunks ---------------------------------------------------------------------------------


def _trunk(conn, cid: Any, trunk_id: Any, *, allow_none: bool = False) -> dict | None:
    if trunk_id in (None, ""):
        if allow_none:
            return None
        raise VoiceError("Choose a trunk.", 422)
    try:
        uuid.UUID(str(trunk_id))
    except ValueError as e:
        raise VoiceError("That trunk was not found.", 404) from e
    row = conn.execute(
        "SELECT * FROM voice_carriers WHERE id = %s AND customer_id = %s AND platform IS NULL", (trunk_id, cid)
    ).fetchone()
    if row is None:
        raise VoiceError("That trunk was not found.", 404)
    return row


def _out(t: dict) -> dict:
    o = t["outbound"] or {}
    return {
        "id": str(t["id"]),
        "key": t["key"],
        "name": t["name"],
        "adapter": t["adapter"],
        "host": o.get("host", ""),
        "port": o.get("port", 5060),
        "transport": o.get("transport", "udp"),
        "prefix": o.get("prefix", ""),
        "max_channels": o.get("max_channels", 30),
        "allow_ips": (t["inbound"] or {}).get("allow_ips", []),
        "enabled": t["enabled"],
        "health": t["health"],
        "last_options_at": t["last_options_at"],
        "last_rtt_ms": t["last_rtt_ms"],
        "sim_fail": o.get("sim_fail", "") if t["adapter"] == "simulated" else "",
    }


def _clean(body: dict, old: dict | None) -> dict:
    """Checked trunk fields from a request (missing fields keep their old value)."""
    cur = _out(old) if old else {}

    def get(k: str, default: Any) -> Any:
        return body[k] if k in body and body[k] is not None else cur.get(k, default)

    name = " ".join(str(get("name", "")).split())[:120]
    if not name:
        raise VoiceError("Give the trunk a name, for example the carrier's name.", 422)
    adapter = get("adapter", "sip")
    if adapter not in ("sip", "simulated"):
        raise VoiceError("A trunk is a real SIP trunk (sip) or a simulated one for testing (simulated).", 422)
    host = str(get("host", "")).strip().lower()
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not HOST.match(host) or "." not in host:
                raise VoiceError(f"{host} is not a host name or IP address.", 422) from None
    if adapter == "sip" and not host:
        raise VoiceError("A SIP trunk needs the carrier's host name or IP address.", 422)
    transport = get("transport", "udp")
    if transport not in ("udp", "tcp", "tls"):
        raise VoiceError("Transport is udp, tcp or tls.", 422)
    try:
        port = int(get("port", 5061 if transport == "tls" else 5060))
        max_channels = int(get("max_channels", 30))
    except (TypeError, ValueError) as e:
        raise VoiceError("Port and channels are whole numbers.", 422) from e
    if not 1 <= port <= 65535:
        raise VoiceError("The port is 1 to 65535.", 422)
    if not 1 <= max_channels <= 10000:
        raise VoiceError("Channels are 1 to 10000.", 422)
    ips = []
    for ip in get("allow_ips", []) or []:
        try:
            net = ipaddress.ip_network(str(ip).strip(), strict=False)
        except ValueError as e:
            raise VoiceError(f"{ip} is not an IP address or network.", 422) from e
        if net.num_addresses > 256:
            raise VoiceError(f"{ip} is too wide: list the carrier's signalling addresses (at most a /24).", 422)
        ips.append(str(net))
    sim_fail = str(get("sim_fail", "") or "")
    if sim_fail not in SIM_FAIL:
        raise VoiceError("A simulated trunk can refuse calls (refuse) or be down (down).", 422)
    if adapter != "simulated":
        sim_fail = ""
    return {
        "name": name,
        "adapter": adapter,
        "enabled": bool(get("enabled", True)),
        "outbound": {
            "host": host or f"{_slug(name)}.trunk.example",  # a simulated trunk needs no real host
            "port": port,
            "transport": transport,
            "max_channels": max_channels,
            "prefix": digits(str(get("prefix", ""))),
            **({"sim_fail": sim_fail} if sim_fail else {}),
        },
        "inbound": {"allow_ips": ips},
        "sim_fail": sim_fail,
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "trunk"


def _health_for(t: dict, sim_fail: str) -> tuple[str, int]:
    """A simulated trunk set to be down shows down at once; cleared, it starts again unknown."""
    if sim_fail == "down":
        return "down", carriers.DOWN_AFTER
    if t and (t["outbound"] or {}).get("sim_fail") == "down":
        return "unknown", 0
    return (t["health"], t["consecutive_failures"]) if t else ("unknown", 0)


def list_trunks(conn: psycopg.Connection, cid: Any) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM voice_carriers WHERE customer_id = %s AND platform IS NULL ORDER BY lower(name)", (cid,)
    ).fetchall()
    return [_out(t) for t in rows]


def save_trunk(conn: psycopg.Connection, cid: Any, body: dict, actor: str, trunk_id: Any = None) -> dict:
    old = _trunk(conn, cid, trunk_id) if trunk_id else None
    v = _clean(body, old)
    clash = conn.execute(
        "SELECT 1 FROM voice_carriers WHERE customer_id = %s AND platform IS NULL AND lower(name) = lower(%s)"
        " AND id <> %s",
        (cid, v["name"], old["id"] if old else uuid.uuid4()),
    ).fetchone()
    if clash:
        raise VoiceError(f"There is already a trunk called {v['name']}.", 409)
    health, fails = _health_for(old, v["sim_fail"])
    if old is None:
        key = f"org-{str(cid).replace('-', '')[:8]}-{uuid.uuid4().hex[:8]}"
        row = conn.execute(
            """INSERT INTO voice_carriers (key, name, adapter, outbound, inbound, enabled, customer_id, created_by,
                 health, consecutive_failures)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                key,
                v["name"],
                v["adapter"],
                Jsonb(v["outbound"]),
                Jsonb(v["inbound"]),
                v["enabled"],
                cid,
                actor,
                health,
                fails,
            ),
        ).fetchone()
        audit.record(conn, actor, "commai.voice.trunk_add", row["key"], cid, {"name": v["name"]})
    else:
        row = conn.execute(
            """UPDATE voice_carriers SET name = %s, adapter = %s, outbound = %s, inbound = %s, enabled = %s,
                 health = %s, consecutive_failures = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (
                v["name"],
                v["adapter"],
                Jsonb(v["outbound"]),
                Jsonb(v["inbound"]),
                v["enabled"],
                health,
                fails,
                old["id"],
            ),
        ).fetchone()
        audit.record(conn, actor, "commai.voice.trunk_change", row["key"], cid, {"name": v["name"]})
    carriers.ensure_health_loop(conn)
    carriers.render_kamailio(conn)  # Kamailio's trunk and allow-lists follow
    return _out(row)


def delete_trunk(conn: psycopg.Connection, cid: Any, trunk_id: Any, actor: str) -> None:
    t = _trunk(conn, cid, trunk_id)
    n = conn.execute(
        "SELECT count(*) AS n FROM voice_numbers WHERE customer_id = %s AND carrier = %s AND status <> 'removed'",
        (cid, t["key"]),
    ).fetchone()["n"]
    if n:
        raise VoiceError(f"{n} number(s) arrive on this trunk. Remove or move them first.", 409)
    conn.execute("DELETE FROM voice_carriers WHERE id = %s", (t["id"],))
    audit.record(conn, actor, "commai.voice.trunk_delete", t["key"], cid, {"name": t["name"]})
    carriers.render_kamailio(conn)


# ---- defaults and locations -----------------------------------------------------------------


def settings(conn: psycopg.Connection, cid: Any) -> dict:
    row = conn.execute("SELECT * FROM voice_trunk_settings WHERE customer_id = %s", (cid,)).fetchone()
    return {
        "default_trunk_id": str(row["default_trunk_id"]) if row and row["default_trunk_id"] else None,
        "exacarib_fallback": row["exacarib_fallback"] if row else True,
    }


def save_settings(conn: psycopg.Connection, cid: Any, body: dict, actor: str) -> dict:
    cur = settings(conn, cid)
    default = body.get("default_trunk_id", cur["default_trunk_id"])
    t = _trunk(conn, cid, default, allow_none=True)
    fallback = bool(body.get("exacarib_fallback", cur["exacarib_fallback"]))
    conn.execute(
        """INSERT INTO voice_trunk_settings (customer_id, default_trunk_id, exacarib_fallback, updated_by)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (customer_id) DO UPDATE SET default_trunk_id = EXCLUDED.default_trunk_id,
             exacarib_fallback = EXCLUDED.exacarib_fallback, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (cid, t["id"] if t else None, fallback, actor),
    )
    audit.record(
        conn,
        actor,
        "commai.voice.trunk_defaults",
        str(cid),
        cid,
        {"default": t["key"] if t else None, "exacarib_fallback": fallback},
    )
    return settings(conn, cid)


def locations(conn: psycopg.Connection, cid: Any) -> list[dict]:
    from ... import organisation

    organisation.join_up(conn, cid)  # phone sites made before locations existed get one
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "primary_trunk_id": str(r["primary_trunk_id"]) if r["primary_trunk_id"] else None,
            "secondary_trunk_id": str(r["secondary_trunk_id"]) if r["secondary_trunk_id"] else None,
            "has_phone_site": r["sites"] > 0,
            "people": r["people"],
            "numbers": r["numbers"],
        }
        for r in conn.execute(
            """SELECT l.id, l.name, lt.primary_trunk_id, lt.secondary_trunk_id,
                      (SELECT count(*) FROM voice_sites s WHERE s.org_location_id = l.id) AS sites,
                      (SELECT count(*) FROM voice_users v JOIN voice_sites s ON s.id = v.site_id
                        WHERE s.org_location_id = l.id AND v.status = 'active') AS people,
                      (SELECT count(*) FROM voice_numbers n JOIN voice_sites s ON s.id = n.site_id
                        WHERE s.org_location_id = l.id AND n.status <> 'removed') AS numbers
               FROM org_locations l LEFT JOIN voice_location_trunks lt ON lt.org_location_id = l.id
               WHERE l.customer_id = %s ORDER BY lower(l.name)""",
            (cid,),
        ).fetchall()
    ]


def _location(conn, cid: Any, location_id: Any) -> dict:
    try:
        uuid.UUID(str(location_id))
    except ValueError as e:
        raise VoiceError("That location was not found.", 404) from e
    loc = conn.execute("SELECT * FROM org_locations WHERE id = %s AND customer_id = %s", (location_id, cid)).fetchone()
    if loc is None:
        raise VoiceError("That location was not found.", 404)
    return loc


def set_location(conn: psycopg.Connection, cid: Any, location_id: Any, body: dict, actor: str) -> dict:
    loc = _location(conn, cid, location_id)
    first = _trunk(conn, cid, body.get("primary_trunk_id"), allow_none=True)
    second = _trunk(conn, cid, body.get("secondary_trunk_id"), allow_none=True)
    if second and not first:
        raise VoiceError("Choose a primary trunk before a secondary one.", 422)
    if first and second and first["id"] == second["id"]:
        raise VoiceError("The secondary trunk must be a different trunk from the primary.", 422)
    conn.execute(
        """INSERT INTO voice_location_trunks (org_location_id, customer_id, primary_trunk_id, secondary_trunk_id,
             updated_by) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (org_location_id) DO UPDATE SET primary_trunk_id = EXCLUDED.primary_trunk_id,
             secondary_trunk_id = EXCLUDED.secondary_trunk_id, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (loc["id"], cid, first["id"] if first else None, second["id"] if second else None, actor),
    )
    audit.record(
        conn,
        actor,
        "commai.voice.location_trunks",
        loc["name"],
        cid,
        {"primary": first["key"] if first else None, "secondary": second["key"] if second else None},
    )
    return next(x for x in locations(conn, cid) if x["id"] == str(loc["id"]))


# ---- routing --------------------------------------------------------------------------------


def caller_location(conn, cid: Any, voice_user_id: Any) -> dict | None:
    if not voice_user_id:
        return None
    return conn.execute(
        """SELECT l.* FROM voice_users v JOIN voice_sites s ON s.id = v.site_id
           JOIN org_locations l ON l.id = s.org_location_id
           WHERE v.id = %s AND v.customer_id = %s""",
        (voice_user_id, cid),
    ).fetchone()


def route(conn: psycopg.Connection, cid: Any, number: str, voice_user_id: Any = None) -> dict | None:
    """The trunks to try for this caller and number, in order, with why each is
    used or skipped. None when the organisation has no trunks of its own."""
    if not conn.execute(
        "SELECT 1 FROM voice_carriers WHERE customer_id = %s AND platform IS NULL LIMIT 1", (cid,)
    ).fetchone():
        return None
    from ... import organisation

    organisation.join_up(conn, cid)
    st = settings(conn, cid)
    loc = caller_location(conn, cid, voice_user_id)
    lt = (
        conn.execute("SELECT * FROM voice_location_trunks WHERE org_location_id = %s", (loc["id"],)).fetchone()
        if loc
        else None
    )
    wanted: list[tuple[str, Any]] = []
    if lt:
        wanted += [(f"Primary for {loc['name']}", lt["primary_trunk_id"])]
        wanted += [(f"Secondary for {loc['name']}", lt["secondary_trunk_id"])]
    wanted.append(("Organisation default", st["default_trunk_id"]))
    steps, seen = [], set()
    for role, tid in wanted:
        if not tid or str(tid) in seen:
            continue
        seen.add(str(tid))
        t = conn.execute("SELECT * FROM voice_carriers WHERE id = %s", (tid,)).fetchone()
        reason = ""
        if not t["enabled"]:
            reason = "Switched off."
        elif t["health"] == "down":
            reason = "Trunk is down (no answer to OPTIONS)."
        steps.append(
            {
                "key": t["key"],
                "name": t["name"],
                "role": role,
                "health": t["health"],
                "usable": not reason,
                "reason": reason,
            }
        )
    route_keys = [s["key"] for s in steps if s["usable"]]
    exacarib: list[str] = []
    note, single = "", False
    if not st["exacarib_fallback"]:
        note = "ExaCarib's carriers are switched off as a fallback for this organisation."
    elif not carriers.any_enabled(conn, cid):
        single = True
        note = "ExaCarib's standard provider takes calls when none of the organisation's trunks can."
    else:
        p = carriers.plan(conn, cid, number)
        exacarib = p["route"]
        for c in p["carriers"]:
            steps.append(
                {
                    "key": c["key"],
                    "name": c["name"],
                    "role": "ExaCarib carrier",
                    "health": c["health"],
                    "usable": c["usable"],
                    "reason": c["reason"],
                }
            )
    return {
        "location": loc["name"] if loc else None,
        "steps": steps,
        "route": route_keys + [k for k in exacarib if k not in route_keys],
        "note": note,
        # No trunk of its own can take the call and ExaCarib has no carriers switched on:
        # the single provider takes it, as for an organisation without trunks.
        "single_provider": single and not route_keys,
    }


# ---- numbers --------------------------------------------------------------------------------


def _site_for(conn, cid: Any, location_id: Any) -> dict:
    loc = _location(conn, cid, location_id)
    site = conn.execute(
        "SELECT * FROM voice_sites WHERE org_location_id = %s AND customer_id = %s ORDER BY created_at LIMIT 1",
        (loc["id"], cid),
    ).fetchone()
    if site is None:
        raise VoiceError(
            f"{loc['name']} has no address yet. Add it under Organisation > Locations, so calls from its numbers "
            "carry an emergency address.",
            422,
        )
    return site


def add_number(conn: psycopg.Connection, cid: Any, trunk_id: Any, body: dict, actor: str) -> dict:
    """A number the organisation's carrier delivers on this trunk, at a location."""
    from .config import norm_e164

    t = _trunk(conn, cid, trunk_id)
    try:
        e164 = norm_e164(str(body.get("e164", "")))
    except Exception as e:  # noqa: BLE001 - any parse failure is the same answer
        raise VoiceError("Give the number in full, for example +1 868 555 0100.", 422) from e
    if not re.fullmatch(r"\+\d{8,15}", e164 or ""):
        raise VoiceError("Give the number in full, for example +1 868 555 0100.", 422)
    if conn.execute("SELECT 1 FROM voice_numbers WHERE e164 = %s AND status <> 'removed'", (e164,)).fetchone():
        raise VoiceError(f"{e164} is already in use.", 409)
    site = _site_for(conn, cid, body.get("location_id"))
    row = conn.execute(
        """INSERT INTO voice_numbers (customer_id, e164, source, status, site_id, provider, carrier,
             emergency_address, country)
           VALUES (%s, %s, 'trunk', 'active', %s, %s, %s, %s, %s) RETURNING *""",
        (cid, e164, site["id"], t["key"], t["key"], Jsonb(_address(site)), (site.get("country") or "TT")[:2]),
    ).fetchone()
    target = body.get("target_type") or "none"
    if target != "none":
        from . import config

        out = config.apply(
            conn,
            cid,
            [{"op": "assign_number", "number": str(row["id"]), "target_type": target, "target": body.get("target")}],
            actor=actor,
            summary=f"Number {e164} on {t['name']}",
            check_price=False,
        )
        if not out["ok"]:
            raise VoiceError("; ".join(x["error"] for x in out["errors"]), 422)
    emergency.refresh_numbers(conn, cid)
    freeswitch.render_business(conn, cid)
    audit.record(conn, actor, "commai.voice.trunk_number_add", e164, cid, {"trunk": t["key"]})
    return number_out(conn, cid, row["id"])


def _address(site: dict) -> dict:
    return {k: site.get(k) or "" for k in ("address_line1", "address_line2", "city", "island", "postcode", "country")}


def number_out(conn, cid: Any, number_id: Any) -> dict:
    r = conn.execute(
        """SELECT n.id, n.e164, n.source, n.status, n.carrier, n.target_type, n.outbound_enabled, n.outbound_reason,
                  l.id AS location_id, l.name AS location, c.name AS trunk
           FROM voice_numbers n LEFT JOIN voice_sites s ON s.id = n.site_id
           LEFT JOIN org_locations l ON l.id = s.org_location_id
           LEFT JOIN voice_carriers c ON c.key = n.carrier AND c.customer_id = n.customer_id
           WHERE n.id = %s AND n.customer_id = %s""",
        (number_id, cid),
    ).fetchone()
    if r is None:
        raise VoiceError("That number was not found.", 404)
    return {**r, "id": str(r["id"]), "location_id": str(r["location_id"]) if r["location_id"] else None}


def numbers(conn: psycopg.Connection, cid: Any) -> list[dict]:
    ids = conn.execute(
        "SELECT id FROM voice_numbers WHERE customer_id = %s AND status <> 'removed' ORDER BY e164", (cid,)
    ).fetchall()
    return [number_out(conn, cid, r["id"]) for r in ids]


def set_number_location(conn: psycopg.Connection, cid: Any, number_id: Any, body: dict, actor: str) -> dict:
    try:
        uuid.UUID(str(number_id))
    except ValueError as e:
        raise VoiceError("That number was not found.", 404) from e
    num = conn.execute(
        "SELECT * FROM voice_numbers WHERE id = %s AND customer_id = %s AND status <> 'removed'", (number_id, cid)
    ).fetchone()
    if num is None:
        raise VoiceError("That number was not found.", 404)
    site = _site_for(conn, cid, body.get("location_id"))
    conn.execute(
        "UPDATE voice_numbers SET site_id = %s, emergency_address = %s WHERE id = %s",
        (site["id"], Jsonb(_address(site)), num["id"]),
    )
    emergency.refresh_numbers(conn, cid)
    audit.record(conn, actor, "commai.voice.number_location", num["e164"], cid, {"site": str(site["id"])})
    return number_out(conn, cid, num["id"])
