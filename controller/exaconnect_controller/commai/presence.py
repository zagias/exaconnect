"""Typing indicators and presence (ADR 0032).

Who is typing, or looking at, a conversation right now. Rows are overwritten
in place and expire on their own (typing after TYPING_S, viewing after
VIEWING_S), so nothing piles up and nothing here is an event a webhook can
see: typing is too chatty and says nothing a business system needs.

- Staff: POST .../conversations/{id}/typing marks them typing (and viewing);
  the inbox live feed sends {"type": "presence", ...} when it changes, and
  GET .../presence answers the same for polling clients.
- Website visitors: the widget posts its own typing and reads whether the
  team (a person, or the AI preparing a reply) is typing. A visitor never
  sees who on the team it is.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

TYPING_S = 6
VIEWING_S = 30


def touch(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    who_kind: str,
    who: str,
    *,
    name: str = "",
    typing: bool | None = None,
) -> None:
    """Record that someone is here; with `typing`, start or stop their typing."""
    conn.execute(
        """INSERT INTO commai_presence (customer_id, conversation_id, who_kind, who, name, typing_until, seen_at)
           VALUES (%(c)s, %(v)s, %(k)s, %(w)s, %(n)s,
                   CASE WHEN %(t)s::boolean IS TRUE THEN now() + make_interval(secs => %(ttl)s) END, now())
           ON CONFLICT (conversation_id, who_kind, who) DO UPDATE SET
             name = EXCLUDED.name, seen_at = now(),
             typing_until = CASE WHEN %(t)s::boolean IS NULL THEN commai_presence.typing_until
                                 ELSE EXCLUDED.typing_until END""",
        {
            "c": customer_id,
            "v": conversation_id,
            "k": who_kind,
            "w": who,
            "n": name[:120],
            "t": typing,
            "ttl": TYPING_S,
        },
    )


def state(conn: psycopg.Connection, customer_id: Any, conversation_id: Any) -> dict:
    """{"typing": [{"who_kind", "name"}], "viewing": [{"name"}], "ai_typing": bool}."""
    rows = conn.execute(
        """SELECT who_kind, who, name, typing_until > now() AS typing,
                  seen_at > now() - make_interval(secs => %s) AS here
           FROM commai_presence WHERE customer_id = %s AND conversation_id = %s
           ORDER BY seen_at DESC""",
        (VIEWING_S, customer_id, conversation_id),
    ).fetchall()
    return {
        "typing": [
            {"who_kind": r["who_kind"], "name": r["name"] if r["who_kind"] == "user" else "Customer"}
            for r in rows
            if r["typing"]
        ],
        "viewing": [{"name": r["name"]} for r in rows if r["here"] and r["who_kind"] == "user"],
        "ai_typing": ai_typing(conn, conversation_id),
    }


def ai_typing(conn: psycopg.Connection, conversation_id: Any) -> bool:
    """The AI is preparing a reply: its job is queued or running."""
    return bool(
        conn.execute(
            """SELECT 1 FROM jobs WHERE kind = 'ai.respond' AND status IN ('queued', 'running')
               AND payload->>'conversation_id' = %s LIMIT 1""",
            (str(conversation_id),),
        ).fetchone()
    )


def team_typing(conn: psycopg.Connection, customer_id: Any, conversation_id: Any) -> bool:
    """What a visitor may know: someone on the business's side is typing."""
    row = conn.execute(
        """SELECT 1 FROM commai_presence WHERE customer_id = %s AND conversation_id = %s AND who_kind = 'user'
           AND typing_until > now() LIMIT 1""",
        (customer_id, conversation_id),
    ).fetchone()
    return bool(row) or ai_typing(conn, conversation_id)


def changes(conn: psycopg.Connection, customer_id: Any, since: dt.datetime) -> list[dict]:
    """Presence rows touched since `since`, for the inbox live feed (no addresses of contacts)."""
    return conn.execute(
        """SELECT conversation_id, who_kind, CASE WHEN who_kind = 'user' THEN name ELSE 'Customer' END AS name,
                  COALESCE(typing_until > now(), false) AS typing, typing_until, seen_at
           FROM commai_presence WHERE customer_id = %s AND seen_at > %s ORDER BY seen_at LIMIT 200""",
        (customer_id, since),
    ).fetchall()
