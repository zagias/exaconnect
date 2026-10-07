"""Emergency addresses and the rules for each island (ADR 0027).

Every site, and every person who works somewhere else (a home worker), has an
emergency address. The address is checked through the SIP provider (simulated
until there is an account): it is submitted, the provider answers later, and
a durable job collects the answer. Moving a person to another site (the
stage 2 move flow) moves their numbers' address and checks the new one.

The rules differ by country and island:
- which numbers reach the emergency services;
- whether the emergency centre receives the caller's registered address with
  the call ("location delivery");
- whether outbound calling may start before the address is validated;
- what every user must be told about the limits of emergency calling.

These rules are ExaCarib's working assumptions. Confirming them with each
regulator or carrier is the "emergency" go-live criterion of every country
(see countries.py), and they are shown as such. Emergency calls themselves are
never blocked, whatever the state of the address.
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import events, jobs
from . import provider as providers
from .common import VoiceError, digits

events.register(
    "voice.emergency_submitted",
    "voice.emergency_validated",
    "voice.emergency_rejected",
    "voice.outbound_blocked",
    "voice.outbound_enabled",
)

ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "island", "country", "postcode")

_LIMITS = (
    "If the power or internet at your site is down, this phone cannot make emergency calls: use a mobile. "
    "On a softphone away from your registered address, the address on file is wrong: tell the operator "
    "where you are."
)


def _rule(numbers: tuple[str, ...], location: bool, required: bool, extra: str = "") -> dict:
    return {
        "numbers": list(numbers),
        "location_delivery": location,
        "requires_validated_address": required,
        "extra": extra,
    }


# Per country, with per-island differences where they exist ("islands").
RULES: dict[str, dict] = {
    "TT": _rule(("999", "990", "811"), False, False),
    "JM": _rule(("119", "110"), False, False),
    "BB": _rule(("211", "311", "511"), False, False),
    "BS": _rule(("911", "919"), False, False),
    "GY": _rule(("911", "912", "913"), False, False),
    "LC": _rule(("911", "999"), False, False),
    "VC": _rule(("911", "999"), False, False),
    "GD": _rule(("911",), False, False),
    "AG": _rule(("911", "999"), False, False),
    "KN": _rule(("911",), False, False),
    "DM": _rule(("999",), False, False),
    "BZ": _rule(("911", "90"), False, False),
    "US": _rule(("911",), True, True, "Your registered address is sent to the emergency centre with the call."),
    "CA": _rule(("911",), True, True, "Your registered address is sent to the emergency centre with the call."),
    "GB": _rule(("999", "112"), True, True, "Your registered address is passed to the emergency services."),
}
# Islands that differ from their country's rule (none known yet; keyed "TT/Tobago").
ISLANDS: dict[str, dict] = {}
DEFAULT_RULE = _rule(("911", "999", "112"), False, True)
ASSUMPTION = "Working assumption: confirmed with the regulator or carrier before numbers go live here."


def rules(country: str, island: str = "") -> dict:
    country = str(country or "").upper()
    base = ISLANDS.get(f"{country}/{island}") or RULES.get(country) or DEFAULT_RULE
    out = {**base, "country": country, "island": island, "status": ASSUMPTION}
    out["notice"] = notice_text(out)
    return out


def notice_text(rule: dict) -> str:
    nums = " or ".join(rule["numbers"])
    parts = [f"Emergency calls to {nums} work from your ExaCarib Connect phone."]
    if rule["location_delivery"]:
        parts.append(rule["extra"] or "Your registered address is sent with the call.")
        parts.append("Keep your address up to date: tell your administrator when you move.")
    else:
        parts.append("The emergency centre does not receive your address automatically. Always say where you are.")
    parts.append(_LIMITS)
    return " ".join(parts)


def notice_key(rule: dict) -> str:
    return f"{rule['country']}:{hashlib.sha256(rule['notice'].encode()).hexdigest()[:12]}"


def all_rules() -> list[dict]:
    return [rules(c) for c in RULES]


def business_numbers(conn: psycopg.Connection, cid: Any) -> set[str]:
    """Emergency numbers for every country the business has a site in."""
    out: set[str] = set()
    for r in conn.execute("SELECT DISTINCT country, island FROM voice_sites WHERE customer_id = %s", (cid,)).fetchall():
        out.update(rules(r["country"], r["island"])["numbers"])
    return out


def is_emergency(conn: psycopg.Connection, cid: Any, number: str, voice_user_id: Any = None) -> bool:
    d = digits(number)
    if not d or len(d) > 3:
        return False
    if voice_user_id:
        row = conn.execute(
            """SELECT s.country, s.island FROM voice_users v JOIN voice_sites s ON s.id = v.site_id
               WHERE v.id = %s AND v.customer_id = %s""",
            (voice_user_id, cid),
        ).fetchone()
        if row and d in rules(row["country"], row["island"])["numbers"]:
            return True
    return d in business_numbers(conn, cid)


def _address(src: dict) -> dict:
    return {k: str(src.get(k) or "").strip()[:200] for k in ADDRESS_FIELDS}


def _addr_key(addr: dict) -> str:
    return hashlib.sha256(repr(sorted(addr.items())).encode()).hexdigest()[:16]


def notify(conn, cid, kind: str, title: str, body: str = "", subject: str = "", voice_user_id: Any = None) -> None:
    conn.execute(
        """INSERT INTO voice_notifications (customer_id, voice_user_id, kind, title, body, subject)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (cid, voice_user_id, kind, title[:200], body[:1000], str(subject)[:200]),
    )


# ---- submitting and checking ----------------------------------------------------------


def submit_site(conn: psycopg.Connection, cid: Any, site_id: Any, actor: str = "system") -> dict:
    site = conn.execute("SELECT * FROM voice_sites WHERE id = %s AND customer_id = %s", (site_id, cid)).fetchone()
    if site is None:
        raise VoiceError("Site not found.", 404)
    addr = _address(site)
    prov = providers.get()
    sub = prov.validate_emergency_address(conn, addr, f"{cid}:site:{site_id}:{_addr_key(addr)}")
    conn.execute(
        """UPDATE voice_sites SET emergency_status = 'pending', emergency_ref = %s, emergency_reason = ''
           WHERE id = %s""",
        (sub["ref"], site_id),
    )
    _queue_check(conn, cid, "site", site_id, sub["ref"])
    events.emit(conn, cid, "voice.emergency_submitted", {"site": site["name"], "by": actor}, str(site_id))
    refresh_numbers(conn, cid, site_id=site_id)
    return {"status": "pending", "ref": sub["ref"]}


def set_user_address(conn: psycopg.Connection, cid: Any, voice_user_id: Any, address: dict | None, actor: str) -> dict:
    """Give a person their own emergency address (None: use their site's again)."""
    vu = conn.execute(
        "SELECT * FROM voice_users WHERE id = %s AND customer_id = %s AND status = 'active'", (voice_user_id, cid)
    ).fetchone()
    if vu is None:
        raise VoiceError("User not found.", 404)
    if address is None:
        site = conn.execute("SELECT * FROM voice_sites WHERE id = %s", (vu["site_id"],)).fetchone()
        from .config import site_address

        conn.execute(
            """UPDATE voice_users SET emergency_own = false, emergency_status = 'not_registered', emergency_ref = '',
                 emergency_reason = '', emergency_address = %s WHERE id = %s""",
            (Jsonb(site_address(site)), vu["id"]),
        )
        refresh_numbers(conn, cid, voice_user_id=vu["id"])
        return {"status": "site"}
    addr = _address(address)
    addr["country"] = (addr["country"] or "TT").upper()[:2]
    if not addr["address_line1"]:
        raise VoiceError("Give the street address.", 422)
    prov = providers.get()
    sub = prov.validate_emergency_address(conn, addr, f"{cid}:user:{vu['id']}:{_addr_key(addr)}")
    conn.execute(
        """UPDATE voice_users SET emergency_own = true, emergency_address = %s, emergency_status = 'pending',
             emergency_ref = %s, emergency_reason = '' WHERE id = %s""",
        (Jsonb({**addr, "own": True}), sub["ref"], vu["id"]),
    )
    _queue_check(conn, cid, "user", vu["id"], sub["ref"])
    events.emit(conn, cid, "voice.emergency_submitted", {"voice_user_id": str(vu["id"]), "by": actor}, str(vu["id"]))
    refresh_numbers(conn, cid, voice_user_id=vu["id"])
    return {"status": "pending", "ref": sub["ref"]}


def _queue_check(conn, cid, kind: str, rid: Any, ref: str) -> None:
    jobs.enqueue(
        conn,
        "voice.emergency_check",
        {"kind": kind, "id": str(rid), "ref": ref},
        customer_id=cid,
        dedupe_key=f"voice.emergency_check:{kind}:{rid}:{ref}",
        delay_s=min(providers.SIM_DELAYS.get("emergency", 0), 30),
        max_attempts=30,
    )


@jobs.handler("voice.emergency_check")
def _check(conn: psycopg.Connection, job: dict):
    p = job["payload"]
    cid = job["customer_id"]
    table = "voice_sites" if p["kind"] == "site" else "voice_users"
    row = conn.execute(f"SELECT * FROM {table} WHERE id = %s FOR UPDATE", (p["id"],)).fetchone()
    if row is None or row["emergency_ref"] != p["ref"]:
        return None  # a newer address replaced this one
    res = providers.get().emergency_address_status(conn, p["ref"])
    if res["status"] == "pending":
        return jobs.Later("The provider is still checking the address.", 15)
    who = row["name"]
    if res["status"] == "registered":
        conn.execute(
            f"""UPDATE {table} SET emergency_status = 'registered', emergency_reason = ''
                {", emergency_normalised = %s, emergency_checked_at = now()" if p["kind"] == "site" else ""}
                WHERE id = %s""",
            ((Jsonb(res.get("normalised") or {}), row["id"]) if p["kind"] == "site" else (row["id"],)),
        )
        events.emit(conn, cid, "voice.emergency_validated", {"kind": p["kind"], "name": who}, str(row["id"]))
        notify(conn, cid, "emergency", f"Emergency address validated: {who}", subject=str(row["id"]))
    else:
        reason = res.get("reason") or "The provider could not validate the address."
        conn.execute(
            f"""UPDATE {table} SET emergency_status = 'rejected', emergency_reason = %s
                {", emergency_checked_at = now()" if p["kind"] == "site" else ""} WHERE id = %s""",
            (reason[:300], row["id"]),
        )
        events.emit(
            conn, cid, "voice.emergency_rejected", {"kind": p["kind"], "name": who, "reason": reason}, str(row["id"])
        )
        notify(
            conn,
            cid,
            "emergency",
            f"Emergency address not accepted: {who}",
            f"{reason} Correct the address so it can be checked again.",
            str(row["id"]),
        )
    if p["kind"] == "site":
        refresh_numbers(conn, cid, site_id=row["id"])
    else:
        refresh_numbers(conn, cid, voice_user_id=row["id"])
    return None


# ---- outbound calling ---------------------------------------------------------------


def address_for_number(conn: psycopg.Connection, n: dict) -> dict:
    """{"country", "island", "status", "where"} of the address a number's calls use."""
    if n["target_type"] == "user" and n["target_id"]:
        vu = conn.execute("SELECT * FROM voice_users WHERE id = %s", (n["target_id"],)).fetchone()
        if vu and vu["emergency_own"]:
            a = vu["emergency_address"] or {}
            return {
                "country": a.get("country", ""),
                "island": a.get("island", ""),
                "status": vu["emergency_status"],
                "where": f"{vu['name']}'s own address",
            }
    site = conn.execute("SELECT * FROM voice_sites WHERE id = %s", (n["site_id"],)).fetchone() if n["site_id"] else None
    if site is None and n["target_type"] == "user" and n["target_id"]:
        site = conn.execute(
            "SELECT s.* FROM voice_sites s JOIN voice_users v ON v.site_id = s.id WHERE v.id = %s", (n["target_id"],)
        ).fetchone()
    if site is None:
        return {"country": n.get("country") or "", "island": "", "status": "not_registered", "where": "no site"}
    return {
        "country": site["country"],
        "island": site["island"],
        "status": site["emergency_status"],
        "where": site["name"],
    }


def outbound_verdict(conn: psycopg.Connection, n: dict) -> tuple[bool, str]:
    if n["status"] != "active":
        return False, "The number is not active yet."
    a = address_for_number(conn, n)
    rule = rules(a["country"] or n.get("country") or "", a["island"])
    if rule["requires_validated_address"] and a["status"] != "registered":
        return False, (
            f"Outbound calls start once the emergency address ({a['where']}) is validated: "
            f"in {a['country'] or 'this country'} the address goes to the emergency centre with each call."
        )
    return True, ""


def refresh_numbers(
    conn: psycopg.Connection, cid: Any, *, site_id: Any = None, voice_user_id: Any = None, number_id: Any = None
) -> list[dict]:
    """Re-check outbound calling for the numbers an address change touches."""
    cond, args = ["customer_id = %s", "status <> 'removed'"], [cid]
    if number_id:
        cond.append("id = %s")
        args.append(number_id)
    elif voice_user_id:
        cond.append("target_type = 'user' AND target_id = %s")
        args.append(voice_user_id)
    elif site_id:
        cond.append(
            "(site_id = %s OR (target_type = 'user' AND target_id IN "
            "(SELECT id FROM voice_users WHERE site_id = %s AND NOT emergency_own)))"
        )
        args += [site_id, site_id]
    changed = []
    for n in conn.execute(f"SELECT * FROM voice_numbers WHERE {' AND '.join(cond)}", args).fetchall():
        if n["status"] != "active":
            continue
        ok, reason = outbound_verdict(conn, n)
        if ok != n["outbound_enabled"] or reason != n["outbound_reason"]:
            conn.execute(
                "UPDATE voice_numbers SET outbound_enabled = %s, outbound_reason = %s WHERE id = %s",
                (ok, reason, n["id"]),
            )
            if ok != n["outbound_enabled"]:
                events.emit(
                    conn,
                    cid,
                    "voice.outbound_enabled" if ok else "voice.outbound_blocked",
                    {"e164": n["e164"], "reason": reason},
                    str(n["id"]),
                )
                changed.append({"e164": n["e164"], "outbound_enabled": ok, "reason": reason})
    return changed


def activate_outbound(conn: psycopg.Connection, cid: Any, number_id: Any) -> dict:
    """Called when a number goes live; outbound starts only if the rules allow."""
    refresh_numbers(conn, cid, number_id=number_id)
    return conn.execute(
        "SELECT e164, outbound_enabled, outbound_reason FROM voice_numbers WHERE id = %s", (number_id,)
    ).fetchone()


def after_move(conn: psycopg.Connection, cid: Any, voice_user_id: Any, site: dict) -> None:
    """A person moved to `site` (stage 2 move): check the site's address if it
    hasn't been, re-check their numbers, and tell them if the rules changed."""
    if site["emergency_status"] in ("not_registered", "rejected") or not site.get("emergency_ref"):
        submit_site(conn, cid, site["id"], "move")
    refresh_numbers(conn, cid, voice_user_id=voice_user_id)
    rule = rules(site["country"], site["island"])
    acked = conn.execute(
        "SELECT 1 FROM voice_emergency_notices WHERE voice_user_id = %s AND notice_key = %s",
        (voice_user_id, notice_key(rule)),
    ).fetchone()
    if not acked:
        notify(
            conn,
            cid,
            "emergency_notice",
            "Read the emergency calling notice for your new site",
            rule["notice"],
            str(voice_user_id),
            voice_user_id=voice_user_id,
        )


# ---- the notice each person must read --------------------------------------------------


def user_notice(conn: psycopg.Connection, cid: Any, vu: dict) -> dict:
    if vu.get("emergency_own"):
        a = vu["emergency_address"] or {}
        country, island = a.get("country", ""), a.get("island", "")
    else:
        site = conn.execute("SELECT country, island FROM voice_sites WHERE id = %s", (vu["site_id"],)).fetchone()
        country, island = (site["country"], site["island"]) if site else ("", "")
    rule = rules(country, island)
    ack = conn.execute(
        "SELECT acknowledged_at FROM voice_emergency_notices WHERE voice_user_id = %s AND notice_key = %s",
        (vu["id"], notice_key(rule)),
    ).fetchone()
    return {
        "key": notice_key(rule),
        "text": rule["notice"],
        "country": rule["country"],
        "numbers": rule["numbers"],
        "location_delivery": rule["location_delivery"],
        "acknowledged_at": ack["acknowledged_at"] if ack else None,
        "status": ASSUMPTION,
    }


def acknowledge(conn: psycopg.Connection, cid: Any, vu: dict, key: str) -> dict:
    n = user_notice(conn, cid, vu)
    if key != n["key"]:
        raise VoiceError("The notice has changed. Read the current one.", 409)
    conn.execute(
        """INSERT INTO voice_emergency_notices (customer_id, voice_user_id, notice_key, text)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (cid, vu["id"], key, n["text"]),
    )
    return user_notice(conn, cid, vu)


def overview(conn: psycopg.Connection, cid: Any) -> dict:
    sites = conn.execute(
        """SELECT id, name, address_line1, address_line2, city, island, country, postcode, emergency_status,
                  emergency_reason, emergency_checked_at FROM voice_sites WHERE customer_id = %s ORDER BY name""",
        (cid,),
    ).fetchall()
    for s in sites:
        s["rules"] = rules(s["country"], s["island"])
    people = conn.execute(
        """SELECT v.id, v.name, v.extension, v.site_id, s.name AS site, v.emergency_own, v.emergency_status,
                  v.emergency_reason, v.emergency_address, s.country AS site_country, s.island AS site_island
           FROM voice_users v LEFT JOIN voice_sites s ON s.id = v.site_id
           WHERE v.customer_id = %s AND v.status = 'active' ORDER BY v.extension""",
        (cid,),
    ).fetchall()
    for p in people:
        n = user_notice(conn, cid, {**p, "id": p["id"]})
        p["notice_acknowledged"] = n["acknowledged_at"] is not None
        p["notice_country"] = n["country"]
    numbers = conn.execute(
        """SELECT id, e164, status, country, site_id, target_type, target_id, outbound_enabled, outbound_reason
           FROM voice_numbers WHERE customer_id = %s AND status <> 'removed' ORDER BY e164""",
        (cid,),
    ).fetchall()
    return {"sites": sites, "people": people, "numbers": numbers, "rules": all_rules()}
