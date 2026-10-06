"""Voice stage 1: the AI agent on browser calls (ADR 0019).

Speech recognition and speech synthesis run in the caller's browser (the Web
Speech API), so they cost nothing: the browser posts each caller turn as
text and speaks the AI's reply. The call is a conversation on channel
"voice"; its transcript is the conversation's messages, so staff see it in
the inbox, can take over, and the AI follows every inbox rule.

A server-side speech provider (DeepInfra speech-to-text and text-to-speech)
is written behind `SpeechProvider` but is NOT live: it needs Dudley's yes to
the spend, then EXA_COMMAI_SERVER_SPEECH=on and EXA_SPEECH_API_KEY.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Protocol

import psycopg

from .. import events, inbox, usage
from . import runtime


class SpeechError(Exception):
    pass


class SpeechProvider(Protocol):
    name: str
    live: bool

    def transcribe(self, audio: bytes, mime: str, language: str = "") -> str: ...

    def synthesise(self, text: str, voice: str = "") -> bytes: ...


class BrowserSpeech:
    """Speech happens in the browser; the server only ever sees text."""

    name = "browser"
    live = True

    def transcribe(self, audio: bytes, mime: str, language: str = "") -> str:
        raise SpeechError("Speech recognition runs in the caller's browser; send the transcript as text.")

    def synthesise(self, text: str, voice: str = "") -> bytes:
        raise SpeechError("Speech synthesis runs in the caller's browser.")


class DeepInfraSpeech:
    """DeepInfra's OpenAI-compatible audio endpoints. Not live until Dudley
    agrees to the spend and the environment switches it on."""

    name = "deepinfra"

    def __init__(self, api_key: str, base_url: str, stt_model: str, tts_model: str, timeout_s: float = 30):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.timeout_s = timeout_s
        self.live = bool(api_key)

    def _post(self, path: str, data: bytes, content_type: str) -> bytes:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:  # noqa: S310 (configured endpoint)
                return r.read(20_000_000)
        except urllib.error.HTTPError as e:
            raise SpeechError(f"The speech service answered {e.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SpeechError("The speech service could not be reached.") from None

    def transcribe(self, audio: bytes, mime: str, language: str = "") -> str:
        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{self.stt_model}\r\n'.encode(),
        ]
        if language:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\n{language}\r\n'.encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
            + audio
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        raw = self._post("/audio/transcriptions", b"".join(parts), f"multipart/form-data; boundary={boundary}")
        try:
            return str(json.loads(raw)["text"])
        except (ValueError, KeyError, TypeError):
            raise SpeechError("The speech service sent an answer in an unexpected shape.") from None

    def synthesise(self, text: str, voice: str = "") -> bytes:
        body = {"model": self.tts_model, "input": text[:4000], "response_format": "mp3"}
        if voice:
            body["voice"] = voice
        return self._post("/audio/speech", json.dumps(body).encode(), "application/json")


def server_speech() -> DeepInfraSpeech | None:
    """The server-side provider, or None while it is not switched on."""
    if os.environ.get("EXA_COMMAI_SERVER_SPEECH", "").lower() not in ("on", "1", "true", "yes"):
        return None
    key = os.environ.get("EXA_SPEECH_API_KEY", "")
    if not key:
        return None
    return DeepInfraSpeech(
        key,
        os.environ.get("EXA_SPEECH_BASE_URL", "https://api.deepinfra.com/v1/openai"),
        os.environ.get("EXA_SPEECH_STT_MODEL", "openai/whisper-large-v3-turbo"),
        os.environ.get("EXA_SPEECH_TTS_MODEL", "hexgrad/Kokoro-82M"),
    )


SPEECH_OFF = (
    "Speech runs in the caller's browser at no cost. Server-side speech (DeepInfra) is built but not live: it"
    " waits for agreement to the spend, then EXA_COMMAI_SERVER_SPEECH=on and EXA_SPEECH_API_KEY."
)


def speech_status() -> dict:
    s = server_speech()
    return {
        "browser": True,
        "server": bool(s and s.live),
        "note": "Server-side speech is switched on." if s else SPEECH_OFF,
    }


# ---- calls --------------------------------------------------------------------------


def _greeting(conn, customer_id: Any, prof: dict) -> str:
    if (prof.get("greeting") or "").strip():
        return prof["greeting"].strip()
    return (
        f"Hello, you're through to {runtime.business_name(conn, customer_id)}. "
        f"I'm {prof['name']}, an AI assistant. How can I help?"
    )


def start_call(conn: psycopg.Connection, customer_id: Any, *, caller: str = "", started_by: str = "") -> dict:
    """Open a browser call: a conversation on channel 'voice', handled by the
    AI when the AI agent is switched on, with a spoken greeting."""
    prof = runtime.profile(conn, customer_id)
    identity = inbox.find_or_create_identity(
        conn, customer_id, "voice", f"browser:{uuid.uuid4()}", name=caller.strip()[:100]
    )
    conv = inbox.open_conversation(
        conn,
        customer_id,
        identity,
        "voice",
        subject="Browser call",
        language=prof["business_language"],
        actor=started_by,
    )
    if prof.get("enabled", True):
        conv = inbox.set_handler(conn, conv, "ai", None, started_by or "system", "browser call with the AI agent")
    call = conn.execute(
        """INSERT INTO ai_calls (customer_id, conversation_id, caller, started_by) VALUES (%s, %s, %s, %s)
           RETURNING *""",
        (customer_id, conv["id"], caller.strip()[:100], started_by),
    ).fetchone()
    if conv["handler"] == "ai":
        msg = inbox.send(
            conn, customer_id, conv["id"], _greeting(conn, customer_id, prof), author_kind="ai", author=prof["name"]
        )
    else:
        msg = inbox._insert_out(
            conn, conv, "Thanks for calling. A member of our team will be with you shortly.", "system", "CommAI"
        )
    return {"call": call, "conversation_id": str(conv["id"]), "greeting": msg["body"], "handler": conv["handler"]}


def get_call(conn: psycopg.Connection, customer_id: Any, conversation_id: Any) -> dict:
    call = conn.execute(
        "SELECT * FROM ai_calls WHERE conversation_id = %s AND customer_id = %s", (conversation_id, customer_id)
    ).fetchone()
    if call is None:
        raise inbox.InboxError("Call not found.", 404)
    return call


def caller_turn(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, text: str) -> dict:
    """Store what the caller said (transcribed in their browser). Returns the
    inbound message. The AI answers in a separate step (see reply_after)."""
    call = get_call(conn, customer_id, conversation_id)
    if call["ended_at"] is not None:
        raise inbox.InboxError("This call has ended.", 409)
    text = " ".join((text or "").split())
    if not text:
        raise inbox.InboxError("Say something first.")
    conv = inbox.get(conn, customer_id, conversation_id)
    ident = conn.execute("SELECT address FROM contact_identities WHERE id = %s", (conv["identity_id"],)).fetchone()
    out = inbox.receive(conn, customer_id, "voice", ident["address"], text[:2000], conversation_id=conversation_id)
    conn.execute("UPDATE ai_calls SET turns = turns + 1 WHERE id = %s", (call["id"],))
    return out["message"]


def replies_after(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, message: dict) -> list[dict]:
    return conn.execute(
        """SELECT id, author_kind, body, created_at FROM messages
           WHERE conversation_id = %s AND customer_id = %s AND direction = 'out' AND created_at > %s
           ORDER BY created_at, id""",
        (conversation_id, customer_id, message["created_at"]),
    ).fetchall()


def end_call(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, actor: str) -> dict:
    call = get_call(conn, customer_id, conversation_id)
    if call["ended_at"] is not None:
        return call
    call = conn.execute(
        "UPDATE ai_calls SET ended_at = now() WHERE id = %s RETURNING *, extract(epoch FROM ended_at - started_at)"
        " AS seconds",
        (call["id"],),
    ).fetchone()
    minutes = max(1, math.ceil(float(call["seconds"]) / 60))
    usage.record(conn, customer_id, "ai_voice_minute", minutes, ref=f"call:{call['id']}", detail={"speech": "browser"})
    events.emit(
        conn,
        customer_id,
        "ai.call_ended",
        {"conversation_id": str(conversation_id), "minutes": minutes, "turns": call["turns"], "by": actor},
        conversation_id,
    )
    return call
