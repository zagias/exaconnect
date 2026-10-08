"""Recorded events (ADR 0016).

Every change a business might react to is written to commai_events in the
same transaction as the change itself. Webhooks, live updates and reports
all read from there, so they agree with each other and with the data.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import jobs

# The event types a webhook can subscribe to. Modules add theirs here.
TYPES = {
    "conversation.created",
    "conversation.assigned",
    "conversation.state_changed",
    "conversation.handler_changed",
    "conversation.handed_over",
    "message.received",
    "message.sent",
    "message.status",
    "message.blocked",
    "note.created",
    "contact.created",
    "action.proposed",
    "action.succeeded",
    "action.failed",
    "booking.confirmed",
    "ai.replied",
    "ai.failed",
    "webhook.test",
}


def register(*types: str) -> None:
    TYPES.update(types)


# In-process listeners run inside emit, in the same transaction as the change.
# Keep them small (an insert, a queued job): a failing listener fails the change.
_listeners: dict[str, list] = {}


def listen(type_: str, fn) -> None:
    """Call fn(conn, customer_id, type, data, event_id) whenever this event is recorded."""
    _listeners.setdefault(type_, []).append(fn)


def emit(conn: psycopg.Connection, customer_id: Any, type_: str, data: dict | None = None, subject: str = "") -> str:
    """Record an event and queue its webhook fan-out. Returns the event id."""
    row = conn.execute(
        "INSERT INTO commai_events (customer_id, type, subject, data) VALUES (%s, %s, %s, %s) RETURNING id",
        (customer_id, type_, str(subject or ""), Jsonb(data or {})),
    ).fetchone()
    event_id = str(row["id"])
    for fn in _listeners.get(type_, ()):
        fn(conn, customer_id, type_, data or {}, event_id)
    has_hooks = conn.execute(
        "SELECT 1 FROM webhook_endpoints WHERE customer_id = %s AND active LIMIT 1", (customer_id,)
    ).fetchone()
    if has_hooks:
        jobs.enqueue(
            conn, "webhook.fanout", {"event_id": event_id}, customer_id=customer_id, dedupe_key=f"fanout:{event_id}"
        )
    return event_id
