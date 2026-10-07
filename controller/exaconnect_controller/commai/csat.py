"""Satisfaction surveys after resolution (ADR 0032).

When a conversation is resolved and the business has surveys switched on,
the job "csat.send" sends one short message on the conversation's own
channel, through inbox.send, so the channel's rules apply (the WhatsApp
24-hour window, email opt-outs...). A send the channel refuses is recorded as
"blocked" with the channel's reason, never forced through another channel.

The message carries a link to a one-page form (no sign-in; an unguessable
token) in the customer's language when that language is switched on for the
business. Results feed the outcomes report (reports.outcomes "satisfaction").

At most one survey per resolution, none for browser calls or the API
channel, and none to a contact surveyed in the last 7 days.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from typing import Any

import psycopg

from ..settings import get_settings
from . import events, i18n, inbox, jobs

events.register("csat.sent", "csat.answered")

DEFAULTS = {"enabled": False, "delay_minutes": 5, "skip_channels": ["voice", "api"], "cooldown_days": 7}


def config(conn: psycopg.Connection, customer_id: Any) -> dict:
    cfg = (inbox.settings(conn, customer_id)["config"] or {}).get("csat") or {}
    return {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _on_state(conn: psycopg.Connection, customer_id: Any, _type: str, data: dict, event_id: str) -> None:
    if data.get("to") != "resolved":
        return
    cfg = config(conn, customer_id)
    if not cfg["enabled"]:
        return
    jobs.enqueue(
        conn,
        "csat.send",
        {"conversation_id": data.get("conversation_id"), "event_id": event_id},
        customer_id=customer_id,
        dedupe_key=f"csat:{event_id}",
        delay_s=max(0, int(cfg["delay_minutes"])) * 60,
        max_attempts=3,
    )


events.listen("conversation.state_changed", _on_state)


def link(token: str) -> str:
    return f"{get_settings().public_url}/api/v1/commai/csat/{token}"


@jobs.handler("csat.send")
def _send_job(conn: psycopg.Connection, job: dict):
    send(conn, job["customer_id"], job["payload"]["conversation_id"], job["payload"]["event_id"])
    return None


def _record(conn, conv: dict, event_id: str, status: str, reason: str, **extra: Any) -> dict:
    return conn.execute(
        """INSERT INTO csat_surveys (customer_id, conversation_id, contact_id, channel, resolved_event, status, reason,
                                     token_hash, message_id, handled_by)
           VALUES (%(c)s, %(conv)s, %(contact)s, %(ch)s, %(ev)s, %(st)s, %(why)s, %(tok)s, %(msg)s, %(by)s)
           ON CONFLICT (conversation_id, resolved_event) DO NOTHING RETURNING *""",
        {
            "c": conv["customer_id"],
            "conv": conv["id"],
            "contact": conv["contact_id"],
            "ch": conv["channel"],
            "ev": event_id,
            "st": status,
            "why": reason[:300],
            "tok": extra.get("token_hash"),
            "msg": extra.get("message_id"),
            "by": extra.get("handled_by", ""),
        },
    ).fetchone() or {"status": "duplicate"}


def send(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, event_id: str) -> dict:
    conv = conn.execute(
        "SELECT * FROM conversations WHERE id = %s AND customer_id = %s", (conversation_id, customer_id)
    ).fetchone()
    if conv is None:
        return {"status": "skipped"}
    if conn.execute(
        "SELECT 1 FROM csat_surveys WHERE conversation_id = %s AND resolved_event = %s", (conv["id"], event_id)
    ).fetchone():
        return {"status": "duplicate"}
    cfg = config(conn, customer_id)
    if not cfg["enabled"]:
        return _record(conn, conv, event_id, "skipped", "Surveys are switched off.")
    if conv["state"] != "resolved":
        return _record(conn, conv, event_id, "skipped", "The conversation was reopened before the survey went out.")
    if conv["channel"] in cfg["skip_channels"]:
        return _record(conn, conv, event_id, "skipped", f"No surveys on the {conv['channel']} channel.")
    if (
        conv["contact_id"]
        and conn.execute(
            """SELECT 1 FROM csat_surveys WHERE contact_id = %s AND status IN ('sent', 'answered')
           AND created_at > now() - make_interval(days => %s)""",
            (conv["contact_id"], int(cfg["cooldown_days"])),
        ).fetchone()
    ):
        return _record(conn, conv, event_id, "skipped", "This contact had a survey recently.")
    handled = conn.execute(
        """SELECT bool_or(author_kind = 'user') AS human, bool_or(author_kind = 'ai') AS ai FROM messages
           WHERE conversation_id = %s AND direction = 'out'""",
        (conv["id"],),
    ).fetchone()
    by = "human" if handled["human"] else ("ai" if handled["ai"] else "")
    token = secrets.token_urlsafe(24)
    body, _loc = i18n.text(
        conn, "csat.message", conv["language"], customer_id, business=_business(conn, customer_id), link=link(token)
    )
    try:
        with conn.transaction():
            msg = inbox.send(conn, customer_id, conv["id"], body, author_kind="system", author="Survey")
    except inbox.InboxError as e:
        return _record(conn, conv, event_id, "blocked", str(e), handled_by=by)
    row = _record(conn, conv, event_id, "sent", "", token_hash=_hash(token), message_id=msg["id"], handled_by=by)
    events.emit(conn, customer_id, "csat.sent", {"conversation_id": str(conv["id"])}, conv["id"])
    return row


def _business(conn, customer_id: Any) -> str:
    row = conn.execute("SELECT name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    return row["name"] if row else ""


def by_token(conn: psycopg.Connection, token: str) -> dict | None:
    if not token or len(token) > 100:
        return None
    return conn.execute(
        """SELECT s.*, c.language, cu.name AS business FROM csat_surveys s
           JOIN conversations c ON c.id = s.conversation_id JOIN customers cu ON cu.id = s.customer_id
           WHERE s.token_hash = %s AND s.status IN ('sent', 'answered')
             AND s.created_at > now() - interval '30 days'""",
        (_hash(token),),
    ).fetchone()


def answer(conn: psycopg.Connection, token: str, rating: int, comment: str = "") -> dict | None:
    s = by_token(conn, token)
    if s is None:
        return None
    if not 1 <= rating <= 5:
        raise ValueError("Choose a rating from 1 to 5.")
    row = conn.execute(
        """UPDATE csat_surveys SET status = 'answered', rating = %s, comment = %s, answered_at = now()
           WHERE id = %s RETURNING *""",
        (rating, (comment or "").strip()[:1000], s["id"]),
    ).fetchone()
    if s["status"] != "answered":
        events.emit(
            conn,
            s["customer_id"],
            "csat.answered",
            {"conversation_id": str(s["conversation_id"]), "rating": rating},
            s["conversation_id"],
        )
    return row


def summary(conn: psycopg.Connection, customer_id: Any, start: dt.datetime, end: dt.datetime) -> dict:
    """For the outcomes report: surveys sent in the period and their answers."""
    r = conn.execute(
        """SELECT count(*) FILTER (WHERE status IN ('sent', 'answered')) AS sent,
                  count(*) FILTER (WHERE status = 'blocked') AS blocked,
                  count(*) FILTER (WHERE status = 'answered') AS answered,
                  avg(rating) FILTER (WHERE status = 'answered') AS average,
                  count(*) FILTER (WHERE rating >= 4) AS satisfied,
                  avg(rating) FILTER (WHERE status = 'answered' AND handled_by = 'ai') AS ai_average,
                  avg(rating) FILTER (WHERE status = 'answered' AND handled_by = 'human') AS human_average
           FROM csat_surveys WHERE customer_id = %s AND created_at >= %s AND created_at < %s""",
        (customer_id, start, end),
    ).fetchone()
    answered = r["answered"] or 0
    return {
        "value": round(float(r["average"]), 2) if r["average"] is not None else None,
        "sent": r["sent"],
        "blocked": r["blocked"],
        "answered": answered,
        "response_rate": round(answered / r["sent"], 3) if r["sent"] else None,
        "satisfied_share": round(r["satisfied"] / answered, 3) if answered else None,
        "by_handler": {
            "ai": round(float(r["ai_average"]), 2) if r["ai_average"] is not None else None,
            "human": round(float(r["human_average"]), 2) if r["human_average"] is not None else None,
        },
        "source": "csat_surveys sent in the period (average rating from 1 to 5)",
    }


def recent(conn: psycopg.Connection, customer_id: Any, limit: int = 50) -> list[dict]:
    return conn.execute(
        """SELECT s.id, s.conversation_id, s.channel, s.status, s.reason, s.rating, s.comment, s.handled_by,
                  s.created_at, s.answered_at, c.subject FROM csat_surveys s
           JOIN conversations c ON c.id = s.conversation_id
           WHERE s.customer_id = %s ORDER BY s.created_at DESC LIMIT %s""",
        (customer_id, limit),
    ).fetchall()
