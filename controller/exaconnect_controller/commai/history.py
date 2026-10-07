"""Customer history (ADR 0032): everything the business has on one contact,
in one place for the inbox and the API.

- conversations: every conversation, newest first
- calls: phone calls (voice CDRs) to or from any of the contact's numbers,
  and browser calls with the AI agent in their conversations
- open_requests: open conversations, actions waiting for approval or running,
  and workflow runs still waiting or running for their conversations
- linked_records: records created in the business's own systems (CRM
  contacts, deals, tickets...) by actions in their conversations
- bookings: confirmed bookings made in their conversations

Customer-facing paths never call this: it is for staff and API keys with
commai:read. It reads no private notes.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg

OPEN_ACTIONS = ("proposed", "awaiting_approval", "approved", "executing")
OPEN_RUNS = ("running", "waiting", "awaiting_approval", "held")
PHONE_CHANNELS = ("sms", "whatsapp", "voice", "phone")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def phone_numbers(conn: psycopg.Connection, contact: dict) -> list[str]:
    """The contact's phone numbers, as digits only (at least 7 of them)."""
    rows = conn.execute(
        "SELECT address FROM contact_identities WHERE contact_id = %s AND channel = ANY(%s)",
        (contact["id"], list(PHONE_CHANNELS)),
    ).fetchall()
    nums = {_digits(contact.get("phone") or "")} | {_digits(r["address"]) for r in rows}
    return sorted(n for n in nums if len(n) >= 7)


def contact_history(conn: psycopg.Connection, customer_id: Any, contact: dict, limit: int = 50) -> dict:
    cid, kid = customer_id, contact["id"]
    conversations = conn.execute(
        """SELECT id, channel, state, subject, intent, priority, created_at, last_message_at, resolved_at
           FROM conversations WHERE customer_id = %s AND contact_id = %s ORDER BY created_at DESC LIMIT %s""",
        (cid, kid, limit),
    ).fetchall()
    nums = phone_numbers(conn, contact)
    phone_calls = (
        conn.execute(
            """SELECT id, call_id, direction, from_number, to_number, started_at, ended_at, seconds, status
               FROM voice_cdrs WHERE customer_id = %s
                 AND (regexp_replace(from_number, '\\D', '', 'g') = ANY(%s)
                      OR regexp_replace(to_number, '\\D', '', 'g') = ANY(%s))
               ORDER BY started_at DESC LIMIT %s""",
            (cid, nums, nums, limit),
        ).fetchall()
        if nums
        else []
    )
    ai_calls = conn.execute(
        """SELECT a.id, a.conversation_id, a.started_at, a.ended_at, a.turns FROM ai_calls a
           JOIN conversations c ON c.id = a.conversation_id
           WHERE a.customer_id = %s AND c.contact_id = %s ORDER BY a.started_at DESC LIMIT %s""",
        (cid, kid, limit),
    ).fetchall()
    calls = [{"kind": "phone", **r} for r in phone_calls] + [
        {
            "kind": "ai_browser",
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "turns": r["turns"],
            "direction": "inbound",
            "seconds": int((r["ended_at"] - r["started_at"]).total_seconds()) if r["ended_at"] else None,
        }
        for r in ai_calls
    ]
    calls.sort(key=lambda r: r["started_at"], reverse=True)

    open_conversations = [
        {"kind": "conversation", "id": c["id"], "state": c["state"], "subject": c["subject"], "at": c["created_at"]}
        for c in conversations
        if c["state"] != "resolved"
    ]
    actions = conn.execute(
        """SELECT r.id, r.app, r.action, r.status, r.conversation_id, r.created_at FROM action_runs r
           JOIN conversations c ON c.id = r.conversation_id
           WHERE r.customer_id = %s AND c.contact_id = %s AND r.status = ANY(%s) AND NOT r.test
           ORDER BY r.created_at DESC LIMIT %s""",
        (cid, kid, list(OPEN_ACTIONS), limit),
    ).fetchall()
    runs = conn.execute(
        """SELECT r.id, w.name AS workflow, r.status, r.conversation_id, r.started_at FROM commai_workflow_runs r
           JOIN commai_workflows w ON w.id = r.workflow_id
           JOIN conversations c ON c.id = r.conversation_id
           WHERE r.customer_id = %s AND c.contact_id = %s AND r.status = ANY(%s) AND NOT r.test
           ORDER BY r.started_at DESC LIMIT %s""",
        (cid, kid, list(OPEN_RUNS), limit),
    ).fetchall()
    open_requests = (
        open_conversations
        + [
            {
                "kind": "action",
                "id": a["id"],
                "state": a["status"],
                "subject": f"{a['app']}: {a['action'].replace('_', ' ')}",
                "conversation_id": a["conversation_id"],
                "at": a["created_at"],
            }
            for a in actions
        ]
        + [
            {
                "kind": "workflow",
                "id": r["id"],
                "state": r["status"],
                "subject": r["workflow"],
                "conversation_id": r["conversation_id"],
                "at": r["started_at"],
            }
            for r in runs
        ]
    )

    linked = conn.execute(
        """SELECT o.app, o.object_type, o.object_id, o.created_at, r.action, r.conversation_id
           FROM commai_connector_objects o
           JOIN action_runs r ON r.customer_id = o.customer_id AND r.idempotency_key = o.idempotency_key
           JOIN conversations c ON c.id = r.conversation_id
           WHERE o.customer_id = %s AND c.contact_id = %s
           ORDER BY o.created_at DESC LIMIT %s""",
        (cid, kid, limit),
    ).fetchall()
    sim = conn.execute(
        """SELECT s.app, s.kind AS object_type, s.id::text AS object_id, s.created_at, r.action, r.conversation_id
           FROM sim_records s
           JOIN action_runs r ON r.customer_id = s.customer_id AND r.idempotency_key = s.idempotency_key
           JOIN conversations c ON c.id = r.conversation_id
           WHERE s.customer_id = %s AND c.contact_id = %s
           ORDER BY s.created_at DESC LIMIT %s""",
        (cid, kid, limit),
    ).fetchall()
    linked_records = [{**r, "simulated": False} for r in linked] + [{**r, "simulated": True} for r in sim]
    linked_records.sort(key=lambda r: r["created_at"], reverse=True)

    bookings = conn.execute(
        """SELECT r.id, r.app, r.action, r.inputs->>'start' AS start, r.inputs->>'end' AS "end",
                  r.conversation_id, r.finished_at AS confirmed_at
           FROM action_runs r JOIN conversations c ON c.id = r.conversation_id
           WHERE r.customer_id = %s AND c.contact_id = %s AND r.status = 'succeeded' AND NOT r.test
             AND r.inputs ? 'start'
           ORDER BY r.inputs->>'start' DESC LIMIT %s""",
        (cid, kid, limit),
    ).fetchall()
    return {
        "contact_id": str(kid),
        "conversations": conversations,
        "calls": calls[:limit],
        "open_requests": open_requests,
        "linked_records": linked_records[:limit],
        "bookings": bookings,
    }
