"""Inbox jobs and events the shared inbox needs on its own (ADR 0038).

- Snoozed conversations wake at `snoozed_until`: setting a snooze queues a
  durable job for that moment. The job wakes the conversation only if it is
  still snoozed until that same time, so a later snooze or a customer reply
  makes the old job a no-op.
- Built-in service targets: when a conversation gets its first-reply and
  resolution targets, jobs are queued for the reminder (by default when 80%
  of the time has gone) and for the target itself. A reminder is a private
  note naming the assignee and a `conversation.target_due_soon` event; a
  missed target is a `conversation.target_missed` event and, unless the
  business switched it off, an escalation: priority up one step, the
  conversation moved to the escalation team when one is set, a note, and a
  `conversation.escalated` event. Each alert goes out once per conversation
  (conversations.target_alerts).
- Tag and priority changes emit events, so webhooks and workflows can react.
- Language detection for every inbound message, before routing.

Settings (commai_settings.config["service_targets"]):
  {"reminders": true, "remind_percent": 80, "escalate": true,
   "escalate_team_id": null}
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from . import events, inbox, jobs

events.register(
    "conversation.woken",
    "conversation.target_due_soon",
    "conversation.target_missed",
    "conversation.escalated",
    "conversation.tags_changed",
    "conversation.priority_changed",
)

TARGETS = {"first_reply": ("first_reply_due", "first_reply_at"), "resolve": ("resolve_due", "resolved_at")}
DEFAULTS = {"reminders": True, "remind_percent": 80, "escalate": True, "escalate_team_id": None}
NEXT_PRIORITY = {"low": "normal", "normal": "high", "high": "urgent", "urgent": "urgent"}


def detect_language(text: str) -> str:
    """The language of an inbound message, or '' when unsure (ADR 0019 detector)."""
    from .ai import language

    return language.detect(text, default="")


def target_settings(conn: psycopg.Connection, customer_id: Any) -> dict:
    s = inbox.settings(conn, customer_id)
    return {**DEFAULTS, **((s["config"] or {}).get("service_targets") or {})}


def _delay(at: dt.datetime) -> float:
    return max(0.0, (at - dt.datetime.now(dt.UTC)).total_seconds())


# ---- snooze ------------------------------------------------------------------------


def schedule_wake(conn: psycopg.Connection, conv: dict) -> None:
    until = conv.get("snoozed_until")
    if until is None:
        return
    jobs.enqueue(
        conn,
        "inbox.wake",
        {"conversation_id": str(conv["id"]), "until": until.isoformat()},
        customer_id=conv["customer_id"],
        dedupe_key=f"wake:{conv['id']}:{until.isoformat()}",
        delay_s=_delay(until),
        max_attempts=20,
    )


@jobs.handler("inbox.wake")
def _wake_job(conn: psycopg.Connection, job: dict):
    p = job["payload"]
    conv = conn.execute("SELECT * FROM conversations WHERE id = %s FOR UPDATE", (p["conversation_id"],)).fetchone()
    if conv is None or conv["state"] != "snoozed" or conv["snoozed_until"] is None:
        return None
    if conv["snoozed_until"].isoformat() != p["until"]:
        return None  # snoozed again since: that snooze has its own job
    if conv["snoozed_until"] > dt.datetime.now(dt.UTC):
        return jobs.Later("not due yet", _delay(conv["snoozed_until"]))
    return wake(conn, conv)


def wake(conn: psycopg.Connection, conv: dict) -> None:
    inbox.set_state(conn, conv["customer_id"], conv["id"], "open", actor="system:snooze", reason="snooze ended")
    events.emit(
        conn,
        conv["customer_id"],
        "conversation.woken",
        {"conversation_id": str(conv["id"]), "assignee_id": str(conv["assignee_id"] or "")},
        conv["id"],
    )


# ---- service targets -------------------------------------------------------------------


def schedule_targets(conn: psycopg.Connection, conv: dict) -> None:
    """Queue the reminder and the deadline check for each target the
    conversation has. Safe to call again: each (target, due) is queued once."""
    for target, (due_col, _) in TARGETS.items():
        due = conv.get(due_col)
        if due is None:
            continue
        for stage in ("soon", "missed"):
            jobs.enqueue(
                conn,
                "inbox.target",
                {"conversation_id": str(conv["id"]), "target": target, "stage": stage, "due": due.isoformat()},
                customer_id=conv["customer_id"],
                dedupe_key=f"target:{conv['id']}:{target}:{stage}:{due.isoformat()}",
                delay_s=_delay(_remind_at(conn, conv, due) if stage == "soon" else due),
                max_attempts=20,
            )


def _remind_at(conn, conv: dict, due: dt.datetime) -> dt.datetime:
    pct = float(target_settings(conn, conv["customer_id"])["remind_percent"])
    start = conv["created_at"]
    return start + (due - start) * max(0.0, min(pct, 100.0)) / 100


def _met(conv: dict, target: str) -> bool:
    _, done_col = TARGETS[target]
    if conv[done_col] is not None:
        return True
    return target == "first_reply" and conv["state"] == "resolved"


@jobs.handler("inbox.target")
def _target_job(conn: psycopg.Connection, job: dict):
    p = job["payload"]
    conv = conn.execute("SELECT * FROM conversations WHERE id = %s FOR UPDATE", (p["conversation_id"],)).fetchone()
    if conv is None:
        return None
    due_col, _ = TARGETS[p["target"]]
    due = conv[due_col]
    if due is None or due.isoformat() != p["due"]:
        return None  # the target moved: its own jobs handle it
    if conv["state"] == "resolved" or _met(conv, p["target"]):
        return None
    now = dt.datetime.now(dt.UTC)
    when = _remind_at(conn, conv, due) if p["stage"] == "soon" else due
    if when > now:
        return jobs.Later("not due yet", _delay(when))
    check(conn, conv, p["target"], p["stage"])
    return None


def check(conn: psycopg.Connection, conv: dict, target: str, stage: str) -> str | None:
    """Send the reminder ("soon") or the missed-target alert and escalation
    ("missed") for one target, once. Returns what was done, or None."""
    alert = f"{target}:{stage}"
    if alert in (conv["target_alerts"] or []) or _met(conv, target):
        return None
    cfg = target_settings(conn, conv["customer_id"])
    if stage == "soon" and not cfg["reminders"]:
        return None
    if stage == "soon" and f"{target}:missed" in (conv["target_alerts"] or []):
        return None
    conn.execute(
        "UPDATE conversations SET target_alerts = array_append(target_alerts, %s) WHERE id = %s", (alert, conv["id"])
    )
    due_col, _ = TARGETS[target]
    what = "first reply" if target == "first_reply" else "resolution"
    owner = conn.execute("SELECT email FROM users WHERE id = %s", (conv["assignee_id"],)).fetchone()
    owner_email = owner["email"] if owner else ""
    data = {
        "conversation_id": str(conv["id"]),
        "target": target,
        "due": conv[due_col].isoformat(),
        "priority": conv["priority"],
        "assignee_id": str(conv["assignee_id"] or ""),
    }
    if stage == "soon":
        inbox.add_note(
            conn,
            conv["customer_id"],
            conv["id"],
            author="CommAI service targets",
            body=(f"@{owner_email} " if owner_email else "")
            + f"Reminder: the {what} target is due at {conv[due_col]:%H:%M} UTC.",
            mentions=[owner_email] if owner_email else [],
        )
        events.emit(conn, conv["customer_id"], "conversation.target_due_soon", data, conv["id"])
        return "reminded"
    inbox._log(conn, conv, "system:targets", "target", "", f"{target} missed", f"{what} target missed")
    events.emit(conn, conv["customer_id"], "conversation.target_missed", data, conv["id"])
    if not cfg["escalate"]:
        return "missed"
    return escalate(conn, conv, f"The {what} target was missed.", cfg.get("escalate_team_id"))


def escalate(conn: psycopg.Connection, conv: dict, reason: str, team_id: Any = None) -> str:
    actor = "system:targets"
    new_priority = NEXT_PRIORITY.get(conv["priority"], conv["priority"])
    if team_id:
        ok = conn.execute(
            "SELECT 1 FROM commai_teams WHERE id = %s AND customer_id = %s", (team_id, conv["customer_id"])
        ).fetchone()
        if ok and str(team_id) != str(conv["team_id"] or ""):
            inbox.assign(conn, conv["customer_id"], conv["id"], actor=actor, team_id=team_id, reason=reason)
    if new_priority != conv["priority"]:
        inbox.set_fields(conn, conv["customer_id"], conv["id"], actor=actor, priority=new_priority)
    inbox.add_note(
        conn,
        conv["customer_id"],
        conv["id"],
        author="CommAI service targets",
        body=f"Escalated: {reason} Priority is now {new_priority}.",
    )
    events.emit(
        conn,
        conv["customer_id"],
        "conversation.escalated",
        {"conversation_id": str(conv["id"]), "reason": reason, "priority": new_priority, "team_id": str(team_id or "")},
        conv["id"],
    )
    return "escalated"


# ---- tag and priority events ---------------------------------------------------------------


def tags_changed(conn: psycopg.Connection, before: dict, after: dict, actor: str) -> None:
    old, new = set(before["tags"] or []), set(after["tags"] or [])
    events.emit(
        conn,
        after["customer_id"],
        "conversation.tags_changed",
        {
            "conversation_id": str(after["id"]),
            "tags": sorted(new),
            "added": sorted(new - old),
            "removed": sorted(old - new),
            "by": actor,
        },
        after["id"],
    )


def priority_changed(conn: psycopg.Connection, before: dict, after: dict, actor: str) -> None:
    events.emit(
        conn,
        after["customer_id"],
        "conversation.priority_changed",
        {"conversation_id": str(after["id"]), "from": before["priority"], "to": after["priority"], "by": actor},
        after["id"],
    )
