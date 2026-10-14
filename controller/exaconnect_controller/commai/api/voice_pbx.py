"""The PBX's own endpoint (ADR 0033): FreeSWITCH asks before each outside call.

Not for people and not public: it is under /api/v1/commai/internal/, which the
public proxy refuses (deploy/public/Caddyfile), and it needs the PBX's keyed
digest (voice/pbx.py). The answer is one line of plain text for the dial plan.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ... import db
from ..voice import pbx, push
from ..voice.common import VoiceError

router = APIRouter()  # nothing for signed-in people
public = APIRouter(prefix="/internal/voice", tags=["commai: voice PBX (internal)"])


@public.get("/authorise", response_class=PlainTextResponse)
def pbx_authorise(
    tenant: str = Query("", max_length=40),
    ext: str = Query("", max_length=20),
    fwd: str = Query("", max_length=20),
    to: str = Query("", max_length=40),
    call: str = Query("", max_length=80),
    x_exa_pbx_auth: str | None = Header(default=None),
) -> str:
    """May this outside call go ahead? "OK <set ids> <caller id>" or "NO <code>"."""
    try:
        pbx.check(x_exa_pbx_auth, tenant, ext, fwd, to, call)
    except VoiceError as e:
        raise HTTPException(e.code, str(e)) from e
    with db.tx() as conn:
        return pbx.authorise(conn, tenant, ext, fwd, to, call)


@public.get("/authorise-platform", response_class=PlainTextResponse)
def pbx_authorise_platform(
    tenant: str = Query("", max_length=40),
    link: str = Query("", max_length=40),
    frm: str = Query("", alias="from", max_length=20),
    to: str = Query("", max_length=40),
    call: str = Query("", max_length=80),
    x_exa_pbx_auth: str | None = Header(default=None),
) -> str:
    """May this outside call from Teams, Zoom or the like go ahead (ADR 0056)? Same answer as /authorise."""
    try:
        pbx.check(x_exa_pbx_auth, tenant, link, frm, to, call)
    except VoiceError as e:
        raise HTTPException(e.code, str(e)) from e
    with db.tx() as conn:
        return pbx.authorise_platform(conn, tenant, link, frm, to, call)


@public.get("/ring", response_class=PlainTextResponse)
def pbx_ring(
    tenant: str = Query("", max_length=40),
    ext: str = Query("", max_length=20),
    caller: str = Query("", max_length=40),
    name: str = Query("", max_length=80),
    call: str = Query("", max_length=80),
    x_exa_pbx_auth: str | None = Header(default=None),
) -> str:
    """A call rings an extension whose Jibsy Phone isn't connected (ADR 0045): wake its
    devices with a push message. "PUSHED <n>" (the dial plan waits for the app) or "NONE"."""
    try:
        pbx.check(x_exa_pbx_auth, tenant, ext, "", caller, call)
    except VoiceError as e:
        raise HTTPException(e.code, str(e)) from e
    if not pbx.FIELD["ext"].match(ext) or not pbx.CALL.match(call) or not re.fullmatch(r"\+?\d{0,15}", caller):
        return "NONE"
    with db.tx() as conn:
        cid = pbx.business_for_tenant(conn, tenant)
        if cid is None:
            return "NONE"
        return push.ring(conn, cid, ext, caller, name.strip()[:60], call)
