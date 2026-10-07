"""FreeSWITCH asks the controller before each outside call (ADR 0033).

The rendered dial plan (freeswitch.py) calls

    GET /api/v1/commai/internal/voice/authorise?tenant=&ext=&fwd=&to=&call=

with mod_curl for every outside call that is not an emergency call, and reads
a one-line plain-text answer it can match with a regular expression:

    OK <set ids> <caller id>   e.g. "OK 2,1 +18685550101". Set ids are
                               Kamailio dispatcher sets in the controller's
                               carrier order ("none": the single provider);
                               caller id is the number to present ("none":
                               leave it as it is).
    NO <code>                  refused, e.g. "NO daily_cap" (codes below).

The same checks as the simulated calls run here (billing.authorise: blocked
prefixes, international on or off, the daily cap, revenue share fraud rules
and the emergency address rule for the calling number; carriers.plan for the
carrier order), so real and simulated calls are refused for the same reasons.

Who may ask: only the PBX. The request carries X-Exa-Pbx-Auth, a keyed digest
of its fields made with EXA_PBX_SECRET, which FreeSWITCH reads from its own
environment. It is a digest, not the secret itself, because mod_curl writes
every request header to FreeSWITCH's debug log; a digest seen there is good
for that one call only. FreeSWITCH's dial plan has md5 and nothing stronger,
so the digest is md5(secret:fields:secret) (the secret at both ends rules out
length extension). With no EXA_PBX_SECRET set, every request is refused. The
path is never served by the public proxy (deploy/public/Caddyfile).

The business comes from the tenant the dial plan was rendered for, never from
the caller: each business's dial plan carries its own tenant, so an extension
can't be charged to another business.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from . import billing, carriers
from .common import VoiceError, digits, domain, now

SECRET_ENV = "EXA_PBX_SECRET"
HEADER = "X-Exa-Pbx-Auth"
CODES = {
    "bad_request": "The request's fields are not valid.",
    "unknown_caller": "The calling extension is not an active user of this business.",
    "voice_off": "Voice is not part of this business's plan.",
    "blocked_prefix": "Premium or high-risk prefix.",
    "international_off": "International calls are switched off.",
    "daily_cap": "Today's call spend cap is reached.",
    "number_outbound_off": "The calling number can't call out yet (emergency address).",
    "intl_suspended": "International calling is suspended.",
    "high_risk": "High-risk destination.",
    "after_hours": "International calls are blocked outside business hours.",
    "intl_daily_calls": "Today's international call cap is reached.",
    "intl_daily_cap": "Today's international spend cap is reached.",
    "intl_spike": "International calling was suspended after a sudden rise.",
    "no_carrier": "No carrier can take the call.",
    "refused": "Refused.",
}
TENANT = re.compile(r"^c[0-9a-f]{12}$")
CALL = re.compile(r"^[0-9a-fA-F-]{8,64}$")
FIELD = {"ext": re.compile(r"^\d{2,6}$"), "to": re.compile(r"^\+?\d{7,15}$")}


def sign(secret: str, tenant: str, ext: str, fwd: str, to: str, call: str) -> str:
    """The digest the dial plan sends: ${md5(secret:tenant:ext:fwd:to:call:secret)}."""
    msg = ":".join((secret, tenant, ext, fwd, to, call, secret))
    return hashlib.md5(msg.encode()).hexdigest()  # noqa: S324 - all FreeSWITCH's dial plan offers; see above


def check(got: str | None, tenant: str, ext: str, fwd: str, to: str, call: str) -> None:
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise VoiceError("The PBX link is not set up (EXA_PBX_SECRET).", 503)
    want = sign(secret, tenant, ext, fwd, to, call)
    if not hmac.compare_digest((got or "").strip().lower().encode(), want.encode()):
        raise VoiceError("Not allowed.", 401)


def business_for_tenant(conn: psycopg.Connection, tenant: str) -> Any:
    """The business whose PBX tenant this is (common.domain), or None."""
    if not TENANT.match(tenant):
        return None
    rows = conn.execute(
        "SELECT id FROM customers WHERE replace(id::text, '-', '') LIKE %s", (tenant[1:] + "%",)
    ).fetchall()
    rows = [r for r in rows if domain(r["id"]).split(".")[0] == tenant]
    return rows[0]["id"] if len(rows) == 1 else None


def caller_id(conn: psycopg.Connection, cid: Any, voice_user_id: Any) -> str:
    """The number an outside call shows: the caller's own number, else the business's first."""
    row = conn.execute(
        """SELECT e164 FROM voice_numbers WHERE customer_id = %s AND status = 'active'
           ORDER BY (target_type = 'user' AND target_id = %s) DESC, e164 LIMIT 1""",
        (cid, voice_user_id),
    ).fetchone()
    return row["e164"] if row else ""


def _record(conn, cid, call: str, vu: Any, to: str, verdict: dict, sets: list[int], cli: str) -> None:
    conn.execute(
        """INSERT INTO voice_pbx_authorisations (customer_id, call_id, voice_user_id, to_number, from_number,
             allowed, code, reason, route, sets)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, call_id) DO UPDATE SET allowed = EXCLUDED.allowed, code = EXCLUDED.code,
             reason = EXCLUDED.reason, route = EXCLUDED.route, sets = EXCLUDED.sets, at = now()""",
        (
            cid,
            call,
            vu,
            to,
            cli,
            verdict["allowed"],
            verdict.get("code", ""),
            verdict.get("reason", "")[:300],
            Jsonb(verdict.get("route") or []),
            Jsonb(sets),
        ),
    )


def authorise(conn: psycopg.Connection, tenant: str, ext: str, fwd: str, to: str, call: str) -> str:
    """The answer line for the dial plan. Refusals are kept as blocked calls, as for simulated calls."""
    from .. import entitlements

    if not (TENANT.match(tenant) and FIELD["to"].match(to) and CALL.match(call)):
        return "NO bad_request"
    ext = ext if FIELD["ext"].match(ext) else ""
    fwd = fwd if FIELD["ext"].match(fwd) else ""
    to = digits(to)
    cid = business_for_tenant(conn, tenant)
    if cid is None:
        return "NO unknown_caller"
    who = fwd or ext  # a forwarded call is the forwarding person's call
    vu = conn.execute(
        "SELECT id FROM voice_users WHERE customer_id = %s AND extension = %s AND status = 'active'",
        (cid, who),
    ).fetchone()
    number = "+" + to
    if vu is None or not entitlements.enabled(conn, cid, "voice"):
        verdict = billing.refused("unknown_caller" if vu is None else "voice_off", "Refused by the PBX check.")
        vu_id = None
        cli = ""
    else:
        vu_id = vu["id"]
        cli = caller_id(conn, cid, vu_id)
        verdict = billing.authorise(conn, cid, number, voice_user_id=vu_id, from_number=cli or None)
    sets: list[int] = []
    if verdict["allowed"] and verdict.get("route"):
        by_key = carriers.carrier_sets(conn)
        sets = [by_key[k] for k in verdict["route"] if k in by_key]
        if not sets:  # carriers chosen, but none is in Kamailio's list
            verdict = billing.refused("no_carrier", "No carrier in the edge proxy's list can take the call.")
    _record(conn, cid, call, vu_id, number, verdict, sets, cli)
    if not verdict["allowed"]:
        at = now()
        billing.record_call(
            conn,
            cid,
            {
                "call_id": call,
                "direction": "outbound",
                "from_number": who,
                "to_number": number,
                "voice_user_id": vu_id,
                "started_at": at,
                "ended_at": at,
                "seconds": 0,
                "status": "blocked",
                "block_reason": verdict.get("reason", ""),
                "provider_ref": call,
            },
        )
        code = verdict.get("code") or "refused"
        audit.record(conn, "system:pbx", "commai.voice.pbx_refused", call, cid, {"code": code})
        return f"NO {code if code in CODES else 'refused'}"
    audit.record(conn, "system:pbx", "commai.voice.pbx_authorised", call, cid, {"sets": sets})
    return f"OK {','.join(map(str, sets)) or 'none'} {cli or 'none'}"


def emergency_sets(conn: psycopg.Connection, cid: Any) -> list[int]:
    """Every carrier switched on for this business, for emergency calls (no controller check)."""
    from .. import golive

    return [
        n
        for key, n in sorted(carriers.carrier_sets(conn).items(), key=lambda kv: kv[1])
        if golive.enabled(conn, "carrier", key, cid)
    ]
