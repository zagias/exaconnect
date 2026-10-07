"""A website visitor talks to the AI agent from the widget (ADR 0038).

The browser call of ADR 0019 (speech recognised and spoken in the browser,
text to the controller), started by a visitor rather than staff, through
public endpoints keyed by the widget key and the visitor's session:

  POST /widget/{key}/calls                       start (the greeting comes back)
  POST /widget/{key}/calls/{conversation}/turns  what the visitor said -> the reply
  POST /widget/{key}/calls/{conversation}/end

Guards, all checked by the service:
- the business switched it on for that widget (settings.ai_calls, off by
  default), the AI agent is on and the business is AI-first;
- the visitor's session owns the call (a call started from one session can't
  be driven from another);
- rate limits shared through Postgres: per visitor CALLS_PER_VISITOR_HOUR
  calls an hour, per widget key CALLS_PER_KEY_HOUR, and TURNS_PER_MIN turns a
  minute per call; a call ends itself after MAX_CALL_MINUTES;
- the hard monthly limit on AI voice minutes (usage.allowed).
The call is linked to the visitor's own contact, so staff see it in the
contact's history; it records ai_voice_minute usage when it ends.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from . import inbox, ratelimit, usage

CALLS_PER_VISITOR_HOUR = 3
CALLS_PER_KEY_HOUR = 60
TURNS_PER_MIN = 20
MAX_CALL_MINUTES = 15


class CallRefused(inbox.InboxError):
    pass


def enabled(conn: psycopg.Connection, key: dict) -> bool:
    """Whether this widget offers visitors a call with the AI agent."""
    from .ai import runtime

    if not (key.get("settings") or {}).get("ai_calls"):
        return False
    s = inbox.settings(conn, key["customer_id"])
    return s["mode"] == "ai_first" and inbox.ai_available() and runtime.profile(conn, key["customer_id"])["enabled"]


def start(conn: psycopg.Connection, key: dict, visitor: str, name: str = "") -> dict:
    from .ai import voice

    cid = key["customer_id"]
    if not enabled(conn, key):
        raise CallRefused("Calls with our assistant aren't available on this website.", 403)
    if not usage.allowed(conn, cid, "ai_voice_minute"):
        raise CallRefused("Calls with our assistant aren't available just now. Please write to us instead.", 429)
    ok, _, reset = ratelimit.hit(conn, f"widget-call:{key['id']}:{visitor}", CALLS_PER_VISITOR_HOUR, 3600)
    if not ok:
        raise CallRefused(f"You've started several calls. Try again in {max(1, reset // 60)} minutes.", 429)
    ok, _, _ = ratelimit.hit(conn, f"widget-call:{key['id']}", CALLS_PER_KEY_HOUR, 3600)
    if not ok:
        raise CallRefused("Our assistant is busy. Please write to us instead.", 429)
    contact = inbox.find_or_create_identity(conn, cid, "web", visitor, name=name)
    out = voice.start_call(
        conn, cid, caller=name, started_by=f"widget:{key['public_key']}", contact_id=contact["contact_id"]
    )
    conn.execute(
        "UPDATE ai_calls SET widget_key_id = %s, visitor = %s WHERE conversation_id = %s",
        (key["id"], visitor, out["conversation_id"]),
    )
    return {"conversation_id": out["conversation_id"], "greeting": out["greeting"], "handler": out["handler"]}


def own_call(conn: psycopg.Connection, key: dict, visitor: str, conversation_id: Any) -> dict:
    row = conn.execute(
        """SELECT * FROM ai_calls WHERE conversation_id = %s AND customer_id = %s AND widget_key_id = %s
           AND visitor = %s""",
        (conversation_id, key["customer_id"], key["id"], visitor),
    ).fetchone()
    if row is None:
        raise CallRefused("Call not found.", 404)
    return row


def turn_allowed(conn: psycopg.Connection, key: dict, call: dict) -> None:
    from .ai import voice

    if call["ended_at"] is not None:
        raise CallRefused("This call has ended.", 409)
    if dt.datetime.now(dt.UTC) - call["started_at"] > dt.timedelta(minutes=MAX_CALL_MINUTES):
        voice.end_call(conn, key["customer_id"], call["conversation_id"], "system:time limit")
        raise CallRefused(f"Calls last up to {MAX_CALL_MINUTES} minutes. Please write to us to carry on.", 409)
    ok, _, _ = ratelimit.hit(conn, f"widget-turn:{call['id']}", TURNS_PER_MIN, 60)
    if not ok:
        raise CallRefused("That's a lot at once. Give our assistant a moment.", 429)
