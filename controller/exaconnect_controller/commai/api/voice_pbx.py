"""The PBX's own endpoint (ADR 0033): FreeSWITCH asks before each outside call.

Not for people and not public: it is under /api/v1/commai/internal/, which the
public proxy refuses (deploy/public/Caddyfile), and it needs the PBX's keyed
digest (voice/pbx.py). The answer is one line of plain text for the dial plan.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ... import db
from ..voice import pbx
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
