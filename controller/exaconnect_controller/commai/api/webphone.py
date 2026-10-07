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

from fastapi import APIRouter, HTTPException, Response

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
            raise HTTPException(e.code, str(e)) from e
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
