"""Support cases reach ExaCarib (ADR 0033).

A business opens a case from the assistant (assistant.open_case attaches the
configuration, redacted errors, diagnostics and correlation ids). ExaCarib
admins work a queue of every business's cases: filter, assign, set status
and priority, and reply. A reply can be internal (an ExaCarib note the
business never sees). The business sees the other replies and can answer;
its answer moves a case waiting on it back to open.

Statuses: open -> in_progress -> waiting_on_customer -> closed (any order).
"""

from __future__ import annotations

from typing import Any

import psycopg

from .. import events
from .redact import redact

STATUSES = ("open", "in_progress", "waiting_on_customer", "closed")
PRIORITIES = ("low", "normal", "high", "urgent")

events.register("support_case.updated", "support_case.replied")


class SupportError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def queue(
    conn: psycopg.Connection,
    *,
    status: str | None = None,
    assignee: str | None = None,
    customer_id: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    after: tuple[str, list] = ("", []),
    limit: int = 100,
) -> list[dict]:
    """ExaCarib's queue across every business, newest first. `after` is a
    cursor condition on c.created_at and c.id (see api/paging.py)."""
    cond, args = ["TRUE"], []
    if status == "active":
        cond.append("c.status <> 'closed'")
    elif status:
        cond.append("c.status = %s")
        args.append(status)
    if assignee is not None:
        cond.append("c.assignee = %s")
        args.append(assignee)
    if customer_id:
        cond.append("c.customer_id::text = %s")
        args.append(customer_id)
    if priority:
        cond.append("c.priority = %s")
        args.append(priority)
    if q:
        cond.append("(c.reference ILIKE %s OR c.subject ILIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    return conn.execute(
        f"""SELECT c.id, c.customer_id, cu.name AS business, c.reference, c.subject, c.status, c.priority,
                   c.assignee, c.created_by, c.created_at, c.updated_at,
                   (SELECT count(*) FROM commai_support_replies r WHERE r.case_id = c.id) AS replies,
                   (SELECT max(r.created_at) FROM commai_support_replies r
                     WHERE r.case_id = c.id AND NOT r.from_exacarib) AS last_customer_reply
            FROM commai_support_cases c JOIN customers cu ON cu.id = c.customer_id
            WHERE {" AND ".join(cond)}{after[0]}
            ORDER BY c.created_at DESC, c.id::text DESC LIMIT %s""",
        [*args, *after[1], limit],
    ).fetchall()


def get(conn: psycopg.Connection, case_id: Any, *, customer_id: Any = None, exacarib: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM commai_support_cases WHERE id::text = %s AND (%s::uuid IS NULL OR customer_id = %s::uuid)",
        (str(case_id), customer_id, customer_id),
    ).fetchone()
    if row is None:
        raise SupportError("Support case not found.", 404)
    row["replies"] = conn.execute(
        """SELECT id, author, from_exacarib, internal, body, created_at FROM commai_support_replies
           WHERE case_id = %s AND (%s OR NOT internal) ORDER BY id""",
        (row["id"], exacarib),
    ).fetchall()
    return row


def update(
    conn: psycopg.Connection,
    case_id: Any,
    *,
    actor: str,
    status: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
) -> dict:
    if status is not None and status not in STATUSES:
        raise SupportError(f"Status must be one of {', '.join(STATUSES)}.", 422)
    if priority is not None and priority not in PRIORITIES:
        raise SupportError(f"Priority must be one of {', '.join(PRIORITIES)}.", 422)
    row = conn.execute(
        """UPDATE commai_support_cases SET status = coalesce(%s, status), priority = coalesce(%s, priority),
             assignee = coalesce(%s, assignee), updated_at = now()
           WHERE id::text = %s RETURNING *""",
        (status, priority, assignee, str(case_id)),
    ).fetchone()
    if row is None:
        raise SupportError("Support case not found.", 404)
    changed = {k: v for k, v in (("status", status), ("priority", priority), ("assignee", assignee)) if v is not None}
    events.emit(
        conn,
        row["customer_id"],
        "support_case.updated",
        {"case": row["reference"], **changed, "by": actor},
        str(row["id"]),
    )
    return row


def reply(
    conn: psycopg.Connection,
    case_id: Any,
    body: str,
    *,
    actor: str,
    from_exacarib: bool,
    internal: bool = False,
    customer_id: Any = None,
    status: str | None = None,
) -> dict:
    body = redact((body or "").strip(), 4000)
    if not body:
        raise SupportError("Write a reply first.", 422)
    if internal and not from_exacarib:
        raise SupportError("Only ExaCarib can add internal notes.", 403)
    case = conn.execute(
        """SELECT * FROM commai_support_cases WHERE id::text = %s AND (%s::uuid IS NULL OR customer_id = %s::uuid)
           FOR UPDATE""",
        (str(case_id), customer_id, customer_id),
    ).fetchone()
    if case is None:
        raise SupportError("Support case not found.", 404)
    r = conn.execute(
        """INSERT INTO commai_support_replies (case_id, customer_id, author, from_exacarib, internal, body)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (case["id"], case["customer_id"], actor, from_exacarib, internal, body),
    ).fetchone()
    new_status = status
    if not from_exacarib and case["status"] in ("waiting_on_customer", "closed"):
        new_status = "open"  # the business answered, or reopened it
    elif from_exacarib and not internal and status is None and case["status"] == "open":
        new_status = "in_progress"
    if new_status and new_status not in STATUSES:
        raise SupportError(f"Status must be one of {', '.join(STATUSES)}.", 422)
    conn.execute(
        "UPDATE commai_support_cases SET status = coalesce(%s, status), updated_at = now() WHERE id = %s",
        (new_status, case["id"]),
    )
    if not internal:
        # Internal notes raise no event: events reach the business's webhooks.
        events.emit(
            conn,
            case["customer_id"],
            "support_case.replied",
            {"case": case["reference"], "from_exacarib": from_exacarib, "status": new_status or case["status"]},
            str(case["id"]),
        )
    return r
