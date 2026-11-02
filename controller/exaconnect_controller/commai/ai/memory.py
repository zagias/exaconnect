"""Customer memory (ADR 0019): short facts about a contact that the AI may use
next time, for verified identities only. Staff can see and delete every fact.

The AI never reads or writes memory for a conversation whose identity is not
verified: someone typing another customer's email address gets nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .. import inbox

MAX_FACTS = 50


def lock_subject(
    conn: psycopg.Connection, customer_id: Any, conversation: dict, *, require_active: bool = True
) -> None:
    """Guard a short mutation against erasure before taking call/conversation locks."""
    if not conversation.get("contact_id"):
        return
    contact = conn.execute(
        "SELECT anonymised_at FROM contacts WHERE id = %s AND customer_id = %s FOR KEY SHARE",
        (conversation["contact_id"], customer_id),
    ).fetchone()
    if require_active and (contact is None or contact["anonymised_at"] is not None):
        raise inbox.NotHandler("This contact is no longer available.")


def check_subject(before: dict, locked: dict) -> None:
    if before.get("contact_id") != locked.get("contact_id"):
        raise inbox.NotHandler("The conversation's contact changed.")


def identity_verified(conn: psycopg.Connection, conversation: dict) -> bool:
    if not conversation.get("identity_id"):
        return False
    row = conn.execute(
        "SELECT verified FROM contact_identities WHERE id = %s", (conversation["identity_id"],)
    ).fetchone()
    return bool(row and row["verified"])


def facts(conn: psycopg.Connection, customer_id: Any, contact_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT id, fact, source, conversation_id, created_by, created_at FROM contact_memory
           WHERE customer_id = %s AND contact_id = %s ORDER BY created_at DESC LIMIT %s""",
        (customer_id, contact_id, MAX_FACTS),
    ).fetchall()


def remember(
    conn: psycopg.Connection,
    customer_id: Any,
    contact_id: Any,
    fact: str,
    *,
    source: str = "ai",
    conversation_id: Any = None,
    created_by: str = "",
) -> dict | None:
    fact = " ".join((fact or "").split())[:300]
    if not fact:
        return None
    return conn.execute(
        """INSERT INTO contact_memory (customer_id, contact_id, fact, source, conversation_id, created_by)
           VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (contact_id, lower(fact)) DO NOTHING RETURNING *""",
        (customer_id, contact_id, fact, source, conversation_id, created_by),
    ).fetchone()


def forget(conn: psycopg.Connection, customer_id: Any, fact_id: Any) -> dict | None:
    return conn.execute(
        "DELETE FROM contact_memory WHERE id = %s AND customer_id = %s RETURNING id, contact_id",
        (fact_id, customer_id),
    ).fetchone()


def forget_all(conn: psycopg.Connection, customer_id: Any, contact_id: Any) -> int:
    cur = conn.execute(
        "DELETE FROM contact_memory WHERE customer_id = %s AND contact_id = %s", (customer_id, contact_id)
    )
    return cur.rowcount
