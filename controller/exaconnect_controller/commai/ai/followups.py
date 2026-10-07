"""Follow-up reminders from promises made in a conversation (ADR 0032).

When the business (a person or the AI) writes "I will call you tomorrow" or
"we'll email you the form by Friday", the promise becomes a reminder on the
conversation's owner: the assignee, else whoever is handling it, else the
person who wrote it. Detection is plain pattern matching, so it is
explainable; the job ("followups.scan") runs after each reply. A second job
("followups.due") tells the owner when it falls due, unless it is done.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from .. import events, inbox, jobs, team
from .model import parse_when

events.register("followup.created", "followup.due")

PROMISE = re.compile(
    r"\b(?:I|we)(?:'ll|\s+will|\s+shall|\s+am going to|\s+are going to|'m going to|'re going to)\s+"
    r"(?:(?:personally|definitely|also)\s+)?"
    r"(?:call|ring|phone|email|e-mail|text|message|send|get back|come back|follow up|check|look into|update|"
    r"reply|confirm|let you know|contact|visit|arrange)\b[^.!?\n]{0,120}",
    re.I,
)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
IN_N = re.compile(r"\bin\s+(\d{1,3}|an?|one|two|three)\s+(minute|hour|day|week)s?\b", re.I)
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}
WORK_HOUR = 9


def _tz(conn, customer_id: Any) -> ZoneInfo:
    try:
        return ZoneInfo(inbox.settings(conn, customer_id)["timezone"])
    except Exception:  # noqa: BLE001 - a bad timezone falls back to UTC
        return ZoneInfo("UTC")


def due_from(text: str, now: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """When a promise falls due, from its words. Defaults to a day later."""
    low = text.lower()
    exact = parse_when(text, now)
    if exact:
        # parse_when reads clock times as UTC; they are the business's local times.
        return exact.replace(tzinfo=tz).astimezone(dt.UTC)
    m = IN_N.search(low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else WORDS[m.group(1).lower()]
        unit = {"minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}[m.group(2).lower()]
        return now + dt.timedelta(**{unit: n})
    local = now.astimezone(tz)

    def at(day: dt.date, hour: int = WORK_HOUR) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(hour), tzinfo=tz).astimezone(dt.UTC)

    if "tomorrow" in low:
        return at(local.date() + dt.timedelta(days=1))
    if "next week" in low:
        return at(local.date() + dt.timedelta(days=7 - local.weekday()))
    for i, d in enumerate(WEEKDAYS):
        if re.search(rf"\b{d}\b", low):
            ahead = (i - local.weekday()) % 7 or 7
            return at(local.date() + dt.timedelta(days=ahead))
    if re.search(r"\b(today|this afternoon|later today|this evening|end of the day|by close)\b", low):
        end = at(local.date(), 17)
        return end if end > now else now + dt.timedelta(hours=2)
    if re.search(r"\b(shortly|soon|in a moment|right away|asap)\b", low):
        return now + dt.timedelta(hours=2)
    return now + dt.timedelta(days=1)


def detect(text: str) -> list[str]:
    seen, out = set(), []
    for m in PROMISE.finditer(text or ""):
        p = " ".join(m.group(0).split()).rstrip(",;: ")
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p[:200])
    return out[:5]


def _on_sent(conn: psycopg.Connection, customer_id: Any, _type: str, data: dict, _event_id: str) -> None:
    if data.get("author_kind") in ("user", "ai") and data.get("message_id"):
        jobs.enqueue(
            conn,
            "followups.scan",
            {"message_id": data["message_id"]},
            customer_id=customer_id,
            dedupe_key=f"followups:{data['message_id']}",
            max_attempts=3,
        )


events.listen("message.sent", _on_sent)


@jobs.handler("followups.scan")
def _scan_job(conn: psycopg.Connection, job: dict):
    scan(conn, job["customer_id"], job["payload"]["message_id"])
    return None


def scan(conn: psycopg.Connection, customer_id: Any, message_id: Any, now: dt.datetime | None = None) -> list[dict]:
    msg = conn.execute(
        """SELECT m.id, m.body, m.author, m.author_kind, m.conversation_id, m.created_at, c.assignee_id,
                  c.handler_user_id
           FROM messages m JOIN conversations c ON c.id = m.conversation_id
           WHERE m.id = %s AND m.customer_id = %s AND m.direction = 'out'""",
        (message_id, customer_id),
    ).fetchone()
    if msg is None or msg["author_kind"] not in ("user", "ai"):
        return []
    promises = detect(msg["body"])
    if not promises:
        return []
    now = now or msg["created_at"]
    tz = _tz(conn, customer_id)
    owner = msg["assignee_id"] or msg["handler_user_id"]
    if owner is None and msg["author_kind"] == "user":
        row = conn.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (msg["author"],)).fetchone()
        owner = row["id"] if row else None
    made = []
    for p in promises:
        due = due_from(p, now, tz)
        r = conn.execute(
            """INSERT INTO followup_reminders (customer_id, conversation_id, message_id, promise, due_at, owner_user_id)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (message_id, promise) DO NOTHING RETURNING *""",
            (customer_id, msg["conversation_id"], msg["id"], p, due, owner),
        ).fetchone()
        if r is None:
            continue
        made.append(r)
        events.emit(
            conn,
            customer_id,
            "followup.created",
            {"conversation_id": str(msg["conversation_id"]), "reminder_id": str(r["id"]), "due_at": due.isoformat()},
            msg["conversation_id"],
        )
        if owner:
            team.notify(
                conn,
                customer_id,
                owner,
                "reminder",
                "A promise to follow up",
                f'"{p}" (due {due.strftime("%d %b %H:%M UTC")})',
                conversation_id=msg["conversation_id"],
                ref=f"made:{r['id']}",
            )
        delay = max(0.0, (due - dt.datetime.now(dt.UTC)).total_seconds())
        jobs.enqueue(
            conn,
            "followups.due",
            {"reminder_id": str(r["id"])},
            customer_id=customer_id,
            dedupe_key=f"followup-due:{r['id']}",
            delay_s=delay,
        )
    return made


@jobs.handler("followups.due")
def _due_job(conn: psycopg.Connection, job: dict):
    r = conn.execute(
        "SELECT * FROM followup_reminders WHERE id = %s AND customer_id = %s",
        (job["payload"]["reminder_id"], job["customer_id"]),
    ).fetchone()
    if r is None or r["status"] != "open":
        return None
    events.emit(
        conn,
        r["customer_id"],
        "followup.due",
        {"conversation_id": str(r["conversation_id"]), "reminder_id": str(r["id"])},
        r["conversation_id"],
    )
    if r["owner_user_id"]:
        team.notify(
            conn,
            r["customer_id"],
            r["owner_user_id"],
            "reminder",
            "Follow-up due now",
            f'"{r["promise"]}"',
            conversation_id=r["conversation_id"],
            ref=f"due:{r['id']}",
        )
    return None


def reminders(conn: psycopg.Connection, customer_id: Any, *, status: str = "open", owner: Any = None) -> list[dict]:
    return conn.execute(
        """SELECT r.*, u.email AS owner, c.subject FROM followup_reminders r
           LEFT JOIN users u ON u.id = r.owner_user_id JOIN conversations c ON c.id = r.conversation_id
           WHERE r.customer_id = %s AND (%s = 'all' OR r.status = %s)
             AND (%s::uuid IS NULL OR r.owner_user_id = %s::uuid)
           ORDER BY r.due_at LIMIT 200""",
        (customer_id, status, status, owner, owner),
    ).fetchall()


def close(conn: psycopg.Connection, customer_id: Any, reminder_id: Any, status: str, actor: str) -> dict | None:
    if status not in ("done", "dismissed", "open"):
        raise ValueError("A reminder is open, done or dismissed.")
    return conn.execute(
        """UPDATE followup_reminders SET status = %s, done_by = CASE WHEN %s = 'open' THEN '' ELSE %s END,
                  done_at = CASE WHEN %s = 'open' THEN NULL ELSE now() END
           WHERE id = %s AND customer_id = %s RETURNING *""",
        (status, status, actor, status, reminder_id, customer_id),
    ).fetchone()
