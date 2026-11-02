"""AI on phone calls through LiveKit (ADR 0033).

How a phone call reaches the AI agent:

    caller -> carrier -> Kamailio -> FreeSWITCH (AI rule: transfer to the
    exacarib_ai gateway) -> LiveKit SIP -> a LiveKit room "call-<id>"
    -> the ExaCarib agent worker (deploy/livekit/agent) joins the room

The worker does speech to text and text to speech, and asks the controller
what to say through the endpoints here (the same conversation flow as the
browser calls of stage 1: the call is an inbox conversation on channel
"voice", and staff can take over). Basic calling never depends on it: the
FreeSWITCH rule falls back to voicemail, a ring group or a queue.

Nothing here runs until LiveKit is set up: EXA_LIVEKIT_URL,
EXA_LIVEKIT_API_KEY and EXA_LIVEKIT_API_SECRET (the self-hosted server's
key pair) and EXA_LIVEKIT_AGENT_SECRET (what the worker sends to the
controller). Tokens are LiveKit's HS256 JWTs, made with the standard library.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import psycopg

from . import pbx_runtime
from .common import VoiceError, digits

SETTINGS = ("EXA_LIVEKIT_URL", "EXA_LIVEKIT_API_KEY", "EXA_LIVEKIT_API_SECRET", "EXA_LIVEKIT_AGENT_SECRET")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("Control-plane redirects are refused")


def missing() -> list[str]:
    return [n for n in SETTINGS if not os.environ.get(n)]


def valid_url() -> bool:
    """Validate the supported server root without returning its private value."""
    value = os.environ.get("EXA_LIVEKIT_URL", "")
    if len(value) > 2048 or any(ord(char) <= 32 for char in value):
        return False
    try:
        url = urllib.parse.urlsplit(value)
        return bool(
            url.scheme in ("http", "https", "ws", "wss")
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and url.path in ("", "/")
            and (url.port is None or 1 <= url.port <= 65535)
        )
    except ValueError:
        return False


def status() -> dict:
    m = missing()
    configured = not m and valid_url()
    ready = phone_ready()
    return {
        "configured": configured,
        "missing": m,
        "url": os.environ.get("EXA_LIVEKIT_URL", "") if configured else "",
        "phone_ready": ready,
        "phone_reason": (
            "Telephone AI has fresh speech, patched PBX and SIP handover evidence."
            if ready
            else "Telephone AI requires fresh speech, patched PBX and verified SIP handover evidence."
        ),
    }


def phone_ready(customer_id: Any = None, *, conn: psycopg.Connection | None = None) -> bool:
    """Only measured, expiring operational evidence can enable telephone AI.

    Evidence is recorded by the authenticated worker protocol after a speech
    loopback and a successful real SIP transfer. No environment readiness flag
    exists. A missing database, stale check or missing configuration fails shut.
    """
    if missing() or not valid_url():
        return False
    from ... import db

    def ready(connection) -> bool:
        row = connection.execute(
            """SELECT (SELECT version FROM voice_ai_phone_pbx_runtime WHERE instance_id = 'main') AS version
                   FROM voice_ai_phone_runtime WHERE NOT lab_only AND expires_at > now()
                   AND speech_verified_at > now() - interval '90 seconds'
                   AND transfer_verified_at > now() - interval '24 hours'
                   AND (%s::uuid IS NULL OR customer_id = %s)
                   AND NOT EXISTS (SELECT 1 FROM commai_entitlements e
                       WHERE e.customer_id = voice_ai_phone_runtime.customer_id
                       AND e.module IN ('voice', 'ai_agents') AND NOT e.enabled)
                   AND EXISTS (SELECT 1 FROM voice_ai_phone_pbx_runtime WHERE instance_id = 'main'
                               AND version_secure AND transfer_enabled AND expires_at > now()) LIMIT 1""",
            (customer_id, customer_id),
        ).fetchone()
        # Re-evaluate the stored binary against this controller's current floor.
        # An older controller may have signed version_secure for 1.11.2; a
        # still-fresh receipt must not survive a stricter security baseline.
        return row is not None and pbx_runtime.version(row["version"])[1]

    try:
        if conn is not None:
            return ready(conn)
        with db.tx() as connection:
            return ready(connection)
    except (psycopg.Error, RuntimeError, AssertionError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token(identity: str, room: str, *, ttl_s: int = 3600, can_publish: bool = True, agent: bool = False) -> str:
    """A LiveKit access token for one room."""
    key = os.environ.get("EXA_LIVEKIT_API_KEY", "")
    secret = os.environ.get("EXA_LIVEKIT_API_SECRET", "")
    if not key or not secret:
        raise VoiceError("LiveKit is not set up yet (EXA_LIVEKIT_API_KEY and EXA_LIVEKIT_API_SECRET).", 503)
    now = int(time.time())
    claims = {
        "iss": key,
        "sub": identity,
        "nbf": now - 5,
        "exp": now + ttl_s,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": can_publish,
            "canSubscribe": True,
            "agent": agent,
        },
    }
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def check_agent(authorization: str | None) -> None:
    """The worker proves itself with EXA_LIVEKIT_AGENT_SECRET (Bearer)."""
    secret = os.environ.get("EXA_LIVEKIT_AGENT_SECRET", "")
    if not secret:
        raise VoiceError("The LiveKit agent is not set up yet.", 503)
    scheme, _, got = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not got.strip():
        raise VoiceError("Not allowed.", 401)
    got = got.strip()
    if not hmac.compare_digest(got.encode(), secret.encode()):
        raise VoiceError("Not allowed.", 401)


def service_request(service: str, method: str, body: dict, *, room: str = "", timeout_s: float = 5) -> dict:
    """Bounded LiveKit Twirp request, with a narrowly granted service token.

    Keep this adapter small: tests replace transport here, while lifecycle,
    authentication, tenant scoping and transfer state remain real.
    """
    if service not in ("RoomService", "SIP") or method not in (
        "GetParticipant",
        "RemoveParticipant",
        "TransferSIPParticipant",
    ):
        raise ValueError("Unsupported LiveKit operation")
    if missing():
        raise VoiceError("LiveKit is not configured.", 503)
    if not valid_url():
        raise VoiceError("LiveKit URL is invalid.", 503)
    url = urllib.parse.urlsplit(os.environ["EXA_LIVEKIT_URL"])
    scheme = "https" if url.scheme in ("https", "wss") else "http"
    base = urllib.parse.urlunsplit((scheme, url.netloc, "", "", ""))
    now = int(time.time())
    grants = {"sip": {"call": True}} if service == "SIP" else {"video": {"room": room, "roomAdmin": True}}
    claims = {
        "iss": os.environ["EXA_LIVEKIT_API_KEY"],
        "sub": "exacarib-phone-controller",
        "nbf": now - 5,
        "exp": now + 60,
        **grants,
    }
    head = _b64(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = _b64(
        hmac.new(os.environ["EXA_LIVEKIT_API_SECRET"].encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    )
    request = urllib.request.Request(
        f"{base}/twirp/livekit.{service}/{method}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {head}.{payload}.{signature}", "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout_s) as response:  # noqa: S310 (operator endpoint)
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("Oversized response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid response")
        return result
    except urllib.error.HTTPError as error:
        if error.code == 404 and service == "RoomService" and method in ("GetParticipant", "RemoveParticipant"):
            try:
                body = error.read(65_537)
                result = json.loads(body) if len(body) <= 65_536 else None
                if isinstance(result, dict) and result.get("code") == "not_found":
                    raise VoiceError("The SIP participant was not found.", 404) from None
            except (ValueError, OSError):
                pass
        raise VoiceError("The phone control service did not confirm the operation.", 502) from None
    except (urllib.error.URLError, OSError, ValueError):
        # Provider replies may contain credentials or caller identifiers.
        raise VoiceError("The phone control service did not confirm the operation.", 502) from None


def sip_participant(room: str, identity: str, call_id: str) -> dict:
    info = service_request("RoomService", "GetParticipant", {"room": room, "identity": identity}, room=room)
    attrs = info.get("attributes") or {}
    if info.get("kind") not in (3, "SIP") or attrs.get("sip.callID") != call_id:
        raise VoiceError("The caller is not the active SIP participant for this call.", 403)
    return attrs


def transfer(room: str, identity: str, call_id: str, target: str) -> dict:
    """Transfer the actual participant. API acceptance is not a human receipt.

    Successful REFER completion depends on the SIP peer's NOTIFY behaviour;
    do not label it 'human answered' without an independent PBX receipt.
    """
    sip_participant(room, identity, call_id)
    service_request(
        "SIP",
        "TransferSIPParticipant",
        {"room_name": room, "participant_identity": identity, "transfer_to": target, "play_dialtone": True},
        timeout_s=35,
    )
    return {"state": "accepted", "human_answered": False}


def hangup(room: str, identity: str, call_id: str) -> None:
    try:
        sip_participant(room, identity, call_id)
        service_request("RoomService", "RemoveParticipant", {"room": room, "identity": identity}, room=room)
    except VoiceError as error:
        if error.code != 404:
            raise


def handover_target(conn: psycopg.Connection, customer_id: Any) -> str:
    """Resolve the operator's internal REFER ingress for this tenant.

    The dedicated PBX route must select that tenant's current fallback; the
    worker and model never supply an arbitrary destination or external number.
    """
    host = os.environ.get("EXA_PHONE_HANDOVER_HOST", "")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", host):
        raise VoiceError("The internal phone handover route is not configured.", 503)
    rule = conn.execute(
        "SELECT 1 FROM voice_ai_rules WHERE customer_id = %s AND enabled LIMIT 1", (customer_id,)
    ).fetchone()
    if rule is None:
        raise VoiceError("This business has no configured AI call fallback.", 503)
    from .common import domain

    tenant = domain(customer_id).split(".")[0]
    return f"sip:exa-ai-handover-{tenant}@{host}"


def business_for(conn: psycopg.Connection, dialled: str) -> dict:
    """Which business owns the number that was called."""
    d = digits(dialled)
    row = conn.execute(
        """SELECT customer_id, id, e164 FROM voice_numbers WHERE status = 'active'
           AND regexp_replace(e164, '\\D', '', 'g') = %s""",
        (d,),
    ).fetchone()
    if row is None:
        raise VoiceError("That number is not live on Jibsy.", 404)
    return row


def room_for(conversation_id: Any) -> str:
    return f"call-{conversation_id}"
