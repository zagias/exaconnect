"""The browser phone for staff (ADR 0039).

A signed-in person with a phone extension gets what the portal's dialer
needs to sign in to FreeSWITCH's Verto endpoint over secure WebSocket
(deploy/freeswitch/autoload_configs/verto.conf.xml): the address, their own
extension's login and its SIP password. Only their own extension, never
anyone else's; nothing is cached (no-store) and the fetch is audited.

The browser phone stays off until EXA_VERTO_URL (a wss:// address) is set,
and calls outside the business need the SIP provider (not live yet).
"""

from __future__ import annotations

import os
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, entitlements
from ..voice import selfservice
from ..voice.common import VoiceError, domain

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: voice"])


def verto_url() -> str:
    url = os.environ.get("EXA_VERTO_URL", "").strip()
    return url if url.startswith("wss://") else ""


@router.get("/voice/webphone")
def webphone(customer_id: str, user: UserDep, response: Response, sign_in: bool = False) -> dict:
    """Whether the browser phone is on and the person's extension; with sign_in=true, the sign-in details."""
    access.check(user, customer_id, "commai:read")
    response.headers["Cache-Control"] = "no-store"
    with db.tx() as conn:
        if not entitlements.enabled(conn, customer_id, "voice"):
            raise HTTPException(403, "Voice is not part of this business's plan.")
        try:
            me = selfservice.mine(conn, customer_id, user.id)
        except VoiceError as e:
            if sign_in or e.code != 404:
                raise HTTPException(e.code, str(e)) from e
            # No extension is a normal state for the screen, not an error.
            return {"enabled": False, "extension": None, "reason": str(e)}
        url = verto_url()
        if not url:
            return {
                "enabled": False,
                "extension": me["extension"],
                "reason": "The browser phone is not switched on yet: ExaCarib sets its secure address first.",
            }
        if not sign_in:
            return {"enabled": True, "extension": me["extension"]}
        pw = conn.execute("SELECT sip_password FROM voice_users WHERE id = %s", (me["id"],)).fetchone()
        audit.record(conn, user.actor, "commai.voice.webphone_sign_in", me["extension"], customer_id)
    dom = domain(customer_id)
    return {
        "enabled": True,
        "url": url,
        "login": f"{me['extension']}@{dom}",
        "password": pw["sip_password"],
        "extension": me["extension"],
        "name": me["name"],
        "domain": dom,
    }


@router.get("/voice/webphone/contacts")
def contacts(customer_id: str, user: UserDep, response: Response) -> dict:
    """Who a person can call inside the business: colleagues' extensions and the shared
    lines (ring groups, queues, menus). Names and extensions only, for the phone app."""
    access.check(user, customer_id, "commai:read")
    response.headers["Cache-Control"] = "no-store"
    with db.tx() as conn:
        if not entitlements.enabled(conn, customer_id, "voice"):
            raise HTTPException(403, "Voice is not part of this business's plan.")
        try:
            me = selfservice.mine(conn, customer_id, user.id)
        except VoiceError as e:
            raise HTTPException(e.code, str(e)) from e
        people = conn.execute(
            """SELECT name, extension FROM voice_users
               WHERE customer_id = %s AND status = 'active' AND id <> %s ORDER BY name, extension""",
            (customer_id, me["id"]),
        ).fetchall()
        lines = conn.execute(
            """SELECT name, extension, 'ring_group' AS kind FROM voice_ring_groups WHERE customer_id = %s
               UNION ALL SELECT name, extension, 'queue' FROM voice_queues WHERE customer_id = %s
               UNION ALL SELECT name, extension, 'menu' FROM voice_menus WHERE customer_id = %s
               ORDER BY name""",
            (customer_id, customer_id, customer_id),
        ).fetchall()
    return {
        "me": {"name": me["name"], "extension": me["extension"]},
        "people": [dict(r) for r in people],
        "lines": [dict(r) for r in lines],
    }


class CallQuality(BaseModel):
    """One answered call as the person's browser heard it (portal/src/pages/commai/voice/quality.ts)."""

    call_id: str = Field(min_length=1, max_length=64)
    direction: Literal["in", "out"]
    secs: int = Field(ge=0, le=86_400)
    codec: str = Field(default="", max_length=40)
    rx_packets: int = Field(ge=0, le=10_000_000)
    rx_lost: int = Field(ge=0, le=10_000_000)
    concealed_pct: float = Field(ge=0, le=100)
    bad_stretches: int = Field(ge=0, le=100_000)
    jitter_ms: int = Field(ge=0, le=60_000)
    jitter_max_ms: int = Field(ge=0, le=60_000)
    jitter_buffer_ms: int = Field(ge=0, le=60_000)
    rtt_ms: int | None = Field(default=None, ge=0, le=60_000)
    rtt_max_ms: int | None = Field(default=None, ge=0, le=60_000)
    up_lost_pct: float | None = Field(default=None, ge=-100, le=100)
    up_jitter_ms: int | None = Field(default=None, ge=0, le=60_000)
    path: str = Field(default="", max_length=40)
    network: str = Field(default="", max_length=40)


BROWSERS = (("Edge", r"Edg/"), ("Firefox", r"Firefox/"), ("Chrome", r"Chrome/|CriOS/"), ("Safari", r"Safari/"))
SYSTEMS = (
    ("Android", r"Android"),
    ("iPhone", r"iPhone"),
    ("iPad", r"iPad"),
    ("Mac", r"Macintosh"),
    ("Windows", r"Windows"),
    ("Linux", r"Linux"),
)


def device(ua: str) -> str:
    """Browser and system in a few words, e.g. "Chrome on Android"; never the full user agent."""
    browser = next((n for n, pat in BROWSERS if re.search(pat, ua)), "other browser")
    system = next((n for n, pat in SYSTEMS if re.search(pat, ua)), "other system")
    return f"{browser} on {system}"


@router.post("/voice/webphone/quality", status_code=204)
def quality(customer_id: str, body: CallQuality, user: UserDep, request: Request) -> Response:
    """Stores how a call sounded to this person, when it ends (kept 90 days)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        try:
            me = selfservice.mine(conn, customer_id, user.id)
        except VoiceError as e:
            raise HTTPException(e.code, str(e)) from e
        q = {**body.model_dump(), "cid": customer_id, "uid": str(user.id), "ext": me["extension"]}
        q["device"] = device(request.headers.get("user-agent", ""))
        conn.execute(
            """INSERT INTO voice_call_quality (customer_id, user_id, extension, device, call_id, direction, secs, codec,
                 rx_packets, rx_lost, concealed_pct, bad_stretches, jitter_ms, jitter_max_ms, jitter_buffer_ms,
                 rtt_ms, rtt_max_ms, up_lost_pct, up_jitter_ms, path, network)
               VALUES (%(cid)s, %(uid)s, %(ext)s, %(device)s, %(call_id)s, %(direction)s, %(secs)s, %(codec)s,
                 %(rx_packets)s, %(rx_lost)s, %(concealed_pct)s, %(bad_stretches)s, %(jitter_ms)s,
                 %(jitter_max_ms)s, %(jitter_buffer_ms)s, %(rtt_ms)s, %(rtt_max_ms)s, %(up_lost_pct)s,
                 %(up_jitter_ms)s, %(path)s, %(network)s)""",
            q,
        )
        conn.execute("DELETE FROM voice_call_quality WHERE created_at < now() - interval '90 days'")
    return Response(status_code=204)
