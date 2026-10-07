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
import time
from typing import Any

import psycopg

from .common import VoiceError, digits

SETTINGS = ("EXA_LIVEKIT_URL", "EXA_LIVEKIT_API_KEY", "EXA_LIVEKIT_API_SECRET", "EXA_LIVEKIT_AGENT_SECRET")


def missing() -> list[str]:
    return [n for n in SETTINGS if not os.environ.get(n)]


def status() -> dict:
    m = missing()
    return {"configured": not m, "missing": m, "url": os.environ.get("EXA_LIVEKIT_URL", "") if not m else ""}


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
    got = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got.encode(), secret.encode()):
        raise VoiceError("Not allowed.", 401)


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
