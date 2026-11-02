"""The PBX's own endpoint (ADR 0033): FreeSWITCH asks before each outside call.

Not for people and not public: it is under /api/v1/commai/internal/, which the
public proxy refuses (deploy/public/Caddyfile), and it needs the PBX's keyed
digest (voice/pbx.py). The answer is one line of plain text for the dial plan.
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from ... import db
from .. import inbox
from ..voice import livekit, pbx, phone_worker, push
from ..voice.common import VoiceError

router = APIRouter()  # nothing for signed-in people
public = APIRouter(prefix="/internal/voice", tags=["commai: voice PBX (internal)"])


class PhoneRuntimeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: Literal["main"]
    version: str = Field(min_length=1, max_length=320)
    transfer_enabled: bool
    checked_at: str = Field(min_length=20, max_length=40)
    nonce: UUID


@public.post("/phone-runtime")
def phone_runtime(body: PhoneRuntimeIn, x_exa_pbx_auth: str | None = Header(default=None)) -> dict:
    """Trusted PBX probe receipt; controller checks version and expiry itself."""
    try:
        with db.tx() as conn:
            return phone_worker.pbx_receipt(conn, body.model_dump(mode="json"), x_exa_pbx_auth)
    except VoiceError as error:
        raise HTTPException(error.code, str(error)) from error


@public.get("/phone-authorise", response_class=PlainTextResponse)
def phone_authorise(
    tenant: str = Query("", max_length=40),
    to: str = Query("", max_length=40),
    call: str = Query("", max_length=80),
    x_exa_pbx_auth: str | None = Header(default=None),
) -> str:
    """A fresh per-call lease check, so stale rendered XML cannot answer into AI."""
    try:
        pbx.check(x_exa_pbx_auth, tenant, "", "", to, call)
    except VoiceError as error:
        raise HTTPException(error.code, str(error)) from error
    if not pbx.TENANT.fullmatch(tenant) or not pbx.FIELD["to"].fullmatch(to) or not pbx.CALL.fullmatch(call):
        return "NO"
    try:
        with db.tx() as conn:
            cid = pbx.business_for_tenant(conn, tenant)
            number = livekit.business_for(conn, to)
            if cid is None or str(cid) != str(number["customer_id"]):
                return "NO"
            # Match start_call without invoking its settings initializer in a
            # read-only readiness check. Lost model access or a business-level
            # off switch must select the PBX fallback before answering into AI.
            profile = conn.execute("SELECT config FROM commai_settings WHERE customer_id = %s", (cid,)).fetchone()
            if profile is None or not inbox.ai_available():
                return "NO"
            if ((profile["config"] or {}).get("ai") or {}).get("enabled", True) is not True:
                return "NO"
            rule = conn.execute(
                "SELECT 1 FROM voice_ai_rules WHERE customer_id = %s AND enabled "
                "AND (number_id IS NULL OR number_id = %s) LIMIT 1",
                (cid, number["id"]),
            ).fetchone()
            return "OK" if rule and livekit.phone_ready(cid, conn=conn) else "NO"
    except VoiceError:
        return "NO"


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
