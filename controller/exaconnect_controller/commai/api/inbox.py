"""Inbox API: contacts, conversations, messages, private notes, teams,
routing and settings (ADR 0016)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep, current_user
from .. import access, attachments, inbox
from .common import errors, page, page_after

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: inbox"])

CONV_SELECT = """
  SELECT c.*, ct.name AS contact_name, ct.email AS contact_email, ct.phone AS contact_phone,
         ci.address AS contact_address, ci.verified AS identity_verified,
         ua.email AS assignee_email, uh.email AS handler_email, t.name AS team_name,
         (SELECT body FROM messages m WHERE m.conversation_id = c.id ORDER BY m.created_at DESC LIMIT 1) AS preview,
         (c.first_reply_at IS NULL AND c.first_reply_due < now() AND c.state <> 'resolved') AS first_reply_overdue,
         (c.resolved_at IS NULL AND c.resolve_due < now() AND c.state <> 'resolved') AS resolve_overdue
  FROM conversations c
  LEFT JOIN contacts ct ON ct.id = c.contact_id
  LEFT JOIN contact_identities ci ON ci.id = c.identity_id
  LEFT JOIN users ua ON ua.id = c.assignee_id
  LEFT JOIN users uh ON uh.id = c.handler_user_id
  LEFT JOIN commai_teams t ON t.id = c.team_id
"""


# ---- contacts -------------------------------------------------------------------


class ContactIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=40)
    language: str = Field(default="", max_length=12)
    external_ref: str = Field(default="", max_length=200)


@router.get("/contacts")
def list_contacts(
    customer_id: str, user: UserDep, q: str = "", limit: int = Query(50, ge=1, le=200), before: str | None = None
) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT ct.*, (SELECT count(*) FROM conversations c WHERE c.contact_id = ct.id) AS conversations
               FROM contacts ct WHERE ct.customer_id = %(c)s
               AND (%(q)s = '' OR ct.name ILIKE %(like)s OR ct.email ILIKE %(like)s OR ct.phone ILIKE %(like)s)
               AND (%(before)s::timestamptz IS NULL OR ct.created_at < %(before)s::timestamptz)
               ORDER BY ct.created_at DESC LIMIT %(n)s""",
            {"c": customer_id, "q": q, "like": f"%{q}%", "before": before, "n": limit + 1},
        ).fetchall()
    return page(rows, limit, "created_at")


@router.post("/contacts", status_code=201)
def create_contact(customer_id: str, body: ContactIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        row = conn.execute(
            """INSERT INTO contacts (customer_id, name, email, phone, language, external_ref)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (customer_id, body.name, body.email, body.phone, body.language, body.external_ref),
        ).fetchone()
        for channel, address in (("email", body.email), ("sms", body.phone)):
            if address:
                conn.execute(
                    """INSERT INTO contact_identities (customer_id, contact_id, channel, address)
                       VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (customer_id, row["id"], channel, address),
                )
        audit.record(conn, user.actor, "commai.contact.create", str(row["id"]), customer_id)
    return row


@router.get("/contacts/{contact_id}")
def get_contact(customer_id: str, contact_id: str, user: UserDep) -> dict:
    """The contact, their verified identities and their conversation history."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE id = %s AND customer_id = %s", (contact_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Contact not found.")
        row["identities"] = conn.execute(
            "SELECT id, channel, address, verified, opted_out FROM contact_identities WHERE contact_id = %s",
            (contact_id,),
        ).fetchall()
        row["conversations"] = conn.execute(
            """SELECT id, channel, state, subject, created_at, last_message_at, resolved_at
               FROM conversations WHERE contact_id = %s ORDER BY created_at DESC LIMIT 50""",
            (contact_id,),
        ).fetchall()
    return row


@router.patch("/contacts/{contact_id}")
def update_contact(customer_id: str, contact_id: str, body: ContactIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    fields = body.model_dump(exclude_unset=True)
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE contacts SET name = COALESCE(%(name)s, name), email = COALESCE(%(email)s, email),
                      phone = COALESCE(%(phone)s, phone), language = COALESCE(%(language)s, language),
                      external_ref = COALESCE(%(external_ref)s, external_ref)
               WHERE id = %(id)s AND customer_id = %(c)s RETURNING *""",
            {**{k: fields.get(k) for k in ContactIn.model_fields}, "id": contact_id, "c": customer_id},
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Contact not found.")
        audit.record(conn, user.actor, "commai.contact.update", contact_id, customer_id, {"fields": sorted(fields)})
    return row


# ---- conversations --------------------------------------------------------------


@router.get("/conversations")
def list_conversations(
    customer_id: str,
    user: UserDep,
    state: str | None = None,
    view: Literal["all", "mine", "unassigned", "ai", "overdue", "open"] = "all",
    channel: str | None = None,
    team_id: str | None = None,
    tag: str | None = None,
    q: str = "",
    limit: int = Query(50, ge=1, le=200),
    before: str | None = None,
) -> dict:
    """Newest activity first. `before` is the `next` cursor of the previous page."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            CONV_SELECT
            + """ WHERE c.customer_id = %(c)s
               AND (%(state)s::text IS NULL OR c.state = %(state)s)
               AND (%(channel)s::text IS NULL OR c.channel = %(channel)s)
               AND (%(team)s::uuid IS NULL OR c.team_id = %(team)s::uuid)
               AND (%(tag)s::text IS NULL OR %(tag)s = ANY(c.tags))
               AND (%(view)s <> 'mine' OR c.assignee_id = %(me)s)
               AND (%(view)s <> 'unassigned' OR (c.assignee_id IS NULL AND c.state <> 'resolved'))
               AND (%(view)s <> 'ai' OR c.handler = 'ai')
               AND (%(view)s <> 'open' OR c.state <> 'resolved')
               AND (%(view)s <> 'overdue' OR (c.state <> 'resolved' AND (
                    (c.first_reply_at IS NULL AND c.first_reply_due < now()) OR c.resolve_due < now())))
               AND (%(q)s = '' OR ct.name ILIKE %(like)s OR c.subject ILIKE %(like)s OR ci.address ILIKE %(like)s)
               AND (%(before)s::timestamptz IS NULL
                    OR COALESCE(c.last_message_at, c.created_at) < %(before)s::timestamptz)
               ORDER BY COALESCE(c.last_message_at, c.created_at) DESC LIMIT %(n)s""",
            {
                "c": customer_id,
                "state": state,
                "channel": channel,
                "team": team_id,
                "tag": tag,
                "view": view,
                "me": user.id,
                "q": q,
                "like": f"%{q}%",
                "before": before,
                "n": limit + 1,
            },
        ).fetchall()
    for r in rows:
        r["cursor"] = r["last_message_at"] or r["created_at"]
    return page(rows, limit, "cursor")


class ConversationIn(BaseModel):
    channel: str = Field(default="api", max_length=20)
    address: str = Field(min_length=1, max_length=255, description="The contact's address on that channel")
    name: str = Field(default="", max_length=200)
    subject: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=10000, description="First message from the contact, if any")
    external_id: str | None = Field(default=None, max_length=200)


@router.post("/conversations", status_code=201)
def create_conversation(customer_id: str, body: ConversationIn, user: UserDep) -> dict:
    """Start a conversation for a contact (from your own app, a form...). With
    `body` it records the contact's first message, exactly as if it had
    arrived on the channel."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        if body.body:
            out = inbox.receive(
                conn,
                customer_id,
                body.channel,
                body.address,
                body.body,
                external_id=body.external_id,
                name=body.name,
                subject=body.subject,
            )
            conv = out["conversation"]
        else:
            ident = inbox.find_or_create_identity(conn, customer_id, body.channel, body.address, name=body.name)
            conv = inbox.open_conversation(
                conn, customer_id, ident, body.channel, subject=body.subject, actor=user.actor
            )
            conv = inbox.route(conn, conv, body.subject)
        audit.record(conn, user.actor, "commai.conversation.create", str(conv["id"]), customer_id)
        return conn.execute(CONV_SELECT + " WHERE c.id = %s", (conv["id"],)).fetchone()


@router.get("/conversations/{conversation_id}")
def get_conversation(customer_id: str, conversation_id: str, user: UserDep) -> dict:
    """The conversation, its customer-facing messages and its history log.
    Private notes are not included: read them from /notes."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        row = conn.execute(
            CONV_SELECT + " WHERE c.id = %s AND c.customer_id = %s", (conversation_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Conversation not found.")
        row["messages"] = inbox.messages(conn, customer_id, conversation_id)
        row["log"] = conn.execute(
            "SELECT at, actor, kind, from_value, to_value, reason FROM conversation_log"
            " WHERE conversation_id = %s ORDER BY id",
            (conversation_id,),
        ).fetchall()
        row["handovers"] = conn.execute(
            "SELECT at, reason, packet FROM handovers WHERE conversation_id = %s ORDER BY id", (conversation_id,)
        ).fetchall()
        row["your_seat"] = access.seat(conn, user, customer_id)
    return row


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    customer_id: str,
    conversation_id: str,
    user: UserDep,
    after: str | None = None,
    cursor: str | None = Query(None, description="Page through: '' for the first page, then `next`"),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict] | dict:
    """Oldest first. Without `cursor`, every message (after `after`); with it, {"items", "next"}."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        inbox.get(conn, customer_id, conversation_id)
        if cursor is None:
            return inbox.messages(conn, customer_id, conversation_id, after)
        at, _, mid = cursor.partition("|")
        try:
            at_ts = dt.datetime.fromisoformat(at) if cursor else None
        except ValueError as e:
            raise HTTPException(400, "That cursor is not valid.") from e
        rows = conn.execute(
            """SELECT * FROM messages WHERE customer_id = %(c)s AND conversation_id = %(v)s
               AND (%(after)s::timestamptz IS NULL OR created_at > %(after)s::timestamptz)
               AND (%(at)s::timestamptz IS NULL OR (created_at, id) > (%(at)s::timestamptz, %(id)s::uuid))
               ORDER BY created_at, id LIMIT %(n)s""",
            {"c": customer_id, "v": conversation_id, "after": after, "at": at_ts, "id": mid or None, "n": limit + 1},
        ).fetchall()
    for r in rows:
        r["cursor"] = f"{r['created_at'].isoformat()}|{r['id']}"
    return page(rows, limit, "cursor")


@router.get("/conversations/{conversation_id}/export")
def export_conversation(customer_id: str, conversation_id: str, user: UserDep) -> dict:
    """A transcript to share with the contact: customer-facing messages only."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        conv = inbox.get(conn, customer_id, conversation_id)
        msgs = inbox.messages(conn, customer_id, conversation_id)
        audit.record(conn, user.actor, "commai.conversation.export", conversation_id, customer_id)
    return {
        "conversation_id": str(conv["id"]),
        "channel": conv["channel"],
        "subject": conv["subject"],
        "messages": [
            {"at": m["created_at"], "from": "customer" if m["direction"] == "in" else "business", "text": m["body"]}
            for m in msgs
        ],
    }


class ReplyIn(BaseModel):
    body: str = Field(default="", max_length=10000)
    template: str = Field(default="", max_length=200)
    take_over: bool = False
    attachments: list[str] = Field(
        default_factory=list, max_length=5, description="Ids of files uploaded to POST .../files"
    )


@router.post("/conversations/{conversation_id}/messages", status_code=201)
def reply(customer_id: str, conversation_id: str, body: ReplyIn, user: UserDep) -> dict:
    """Reply on the conversation's own channel. Replying takes over from the AI.
    Send an Idempotency-Key header so a retried request never sends twice."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        files = attachments.claim(conn, inbox.get(conn, customer_id, conversation_id), user.id, body.attachments)
        msg = inbox.send(
            conn,
            customer_id,
            conversation_id,
            body.body,
            author_kind="user",
            author=user.email,
            user_id=user.id,
            template=body.template,
            take_over=body.take_over,
            attachments=files,
        )
        attachments.attach(conn, inbox.get(conn, customer_id, conversation_id), files)
        audit.record(
            conn, user.actor, "commai.message.send", str(msg["id"]), customer_id, {"conversation_id": conversation_id}
        )
    return msg


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    mentions: list[str] = Field(default_factory=list, max_length=20)


@router.get("/conversations/{conversation_id}/notes")
def list_notes(
    customer_id: str,
    conversation_id: str,
    user: UserDep,
    cursor: str | None = Query(None, description="'' for the first page, then `next`"),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict] | dict:
    """Private notes. Never reachable with a customer-facing API key."""
    access.check(user, customer_id, "commai:notes")
    with db.tx() as conn, errors():
        inbox.get(conn, customer_id, conversation_id)
        rows = conn.execute(
            "SELECT * FROM commai_notes WHERE conversation_id = %s AND customer_id = %s ORDER BY created_at, id",
            (conversation_id, customer_id),
        ).fetchall()
    return rows if cursor is None else page_after(rows, cursor, limit)


@router.post("/conversations/{conversation_id}/notes", status_code=201)
def add_note(customer_id: str, conversation_id: str, body: NoteIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:notes")
    with db.tx() as conn, errors():
        note = inbox.add_note(
            conn, customer_id, conversation_id, author=user.email, body=body.body, mentions=body.mentions
        )
        audit.record(conn, user.actor, "commai.note.create", str(note["id"]), customer_id)
    return note


class AssignIn(BaseModel):
    assignee_id: str | None = None
    team_id: str | None = None
    queue: str | None = Field(default=None, max_length=60)
    reason: str = Field(default="", max_length=300)


@router.post("/conversations/{conversation_id}/assign")
def assign(customer_id: str, conversation_id: str, body: AssignIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        conv = inbox.assign(
            conn,
            customer_id,
            conversation_id,
            actor=user.actor,
            assignee_id=body.assignee_id,
            team_id=body.team_id,
            queue=body.queue,
            reason=body.reason,
        )
        audit.record(
            conn,
            user.actor,
            "commai.conversation.assign",
            conversation_id,
            customer_id,
            body.model_dump(exclude_none=True),
        )
    return conv


class StateIn(BaseModel):
    state: Literal["open", "awaiting_customer", "awaiting_internal", "snoozed", "resolved", "reopened"]
    snoozed_until: dt.datetime | None = None
    reason: str = Field(default="", max_length=300)


@router.post("/conversations/{conversation_id}/state")
def set_state(customer_id: str, conversation_id: str, body: StateIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        conv = inbox.set_state(
            conn,
            customer_id,
            conversation_id,
            body.state,
            actor=user.actor,
            reason=body.reason,
            snoozed_until=body.snoozed_until,
        )
        audit.record(conn, user.actor, "commai.conversation.state", conversation_id, customer_id, {"state": body.state})
    return conv


class FieldsIn(BaseModel):
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    subject: str | None = Field(default=None, max_length=200)
    intent: str | None = Field(default=None, max_length=60, description="What the contact wants, for routing")


@router.patch("/conversations/{conversation_id}")
def update_conversation(customer_id: str, conversation_id: str, body: FieldsIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        conv = inbox.set_fields(
            conn,
            customer_id,
            conversation_id,
            actor=user.actor,
            priority=body.priority,
            tags=body.tags,
            subject=body.subject,
            intent=body.intent,
        )
        audit.record(
            conn,
            user.actor,
            "commai.conversation.update",
            conversation_id,
            customer_id,
            body.model_dump(exclude_none=True),
        )
    return conv


@router.post("/conversations/{conversation_id}/takeover")
def take_over(customer_id: str, conversation_id: str, user: UserDep) -> dict:
    """Become the handler. The AI stops replying at once and the context stays."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        conv = inbox.take_over(conn, customer_id, conversation_id, user.id, user.actor)
        audit.record(conn, user.actor, "commai.conversation.takeover", conversation_id, customer_id)
    return conv


@router.post("/conversations/{conversation_id}/handback")
def hand_back(customer_id: str, conversation_id: str, user: UserDep) -> dict:
    """Give the conversation back to the AI agent."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        conv = inbox.hand_to_ai(conn, customer_id, conversation_id, user.actor)
        audit.record(conn, user.actor, "commai.conversation.handback", conversation_id, customer_id)
    return conv


# ---- teams, members, routing, settings ------------------------------------------


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    skills: list[str] = Field(default_factory=list, max_length=20)
    members: list[str] = Field(default_factory=list, max_length=200, description="user ids")


@router.get("/teams")
def list_teams(
    customer_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict] | dict:
    """Without `cursor`, every team; with it ('' for the first page), {"items", "next"}."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT t.*, COALESCE(array_agg(tm.user_id) FILTER (WHERE tm.user_id IS NOT NULL), '{}') AS members
               FROM commai_teams t LEFT JOIN commai_team_members tm ON tm.team_id = t.id
               WHERE t.customer_id = %s GROUP BY t.id ORDER BY t.name""",
            (customer_id,),
        ).fetchall()
    return rows if cursor is None else page_after(rows, cursor, limit)


def _set_members(conn, customer_id: str, team_id: Any, members: list[str]) -> None:
    for uid in members:
        ok = conn.execute(
            "SELECT 1 FROM users WHERE id = %s AND (customer_id = %s OR role = 'admin')", (uid, customer_id)
        ).fetchone()
        if not ok:
            raise HTTPException(400, "Every team member must belong to this business.")
    conn.execute("DELETE FROM commai_team_members WHERE team_id = %s", (team_id,))
    for uid in members:
        conn.execute("INSERT INTO commai_team_members (team_id, user_id) VALUES (%s, %s)", (team_id, uid))
        conn.execute(
            "INSERT INTO commai_members (customer_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (customer_id, uid),
        )


@router.post("/teams", status_code=201)
def create_team(customer_id: str, body: TeamIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        row = conn.execute(
            """INSERT INTO commai_teams (customer_id, name, skills) VALUES (%s, %s, %s)
               ON CONFLICT (customer_id, name) DO NOTHING RETURNING *""",
            (customer_id, body.name.strip(), body.skills),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "There is already a team with that name.")
        _set_members(conn, customer_id, row["id"], body.members)
        audit.record(conn, user.actor, "commai.team.create", body.name, customer_id)
    return {**row, "members": body.members}


@router.put("/teams/{team_id}")
def update_team(customer_id: str, team_id: str, body: TeamIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        row = conn.execute(
            "UPDATE commai_teams SET name = %s, skills = %s WHERE id = %s AND customer_id = %s RETURNING *",
            (body.name.strip(), body.skills, team_id, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Team not found.")
        _set_members(conn, customer_id, team_id, body.members)
        audit.record(conn, user.actor, "commai.team.update", body.name, customer_id)
    return {**row, "members": body.members}


@router.delete("/teams/{team_id}", status_code=204)
def delete_team(customer_id: str, team_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        row = conn.execute(
            "DELETE FROM commai_teams WHERE id = %s AND customer_id = %s RETURNING name", (team_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Team not found.")
        audit.record(conn, user.actor, "commai.team.delete", row["name"], customer_id)


@router.get("/members")
def list_members(
    customer_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict] | dict:
    """Everyone in the business, with their seat and availability. Page with `cursor` ('' first)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT u.id, u.email, COALESCE(m.seat, 'agent') AS seat, COALESCE(m.skills, '{}') AS skills,
                      COALESCE(m.languages, '{en}') AS languages, COALESCE(m.available, true) AS available,
                      (SELECT count(*) FROM conversations c WHERE c.assignee_id = u.id
                       AND c.state NOT IN ('resolved', 'snoozed')) AS open_conversations
               FROM users u LEFT JOIN commai_members m ON m.user_id = u.id AND m.customer_id = %(c)s
               WHERE u.customer_id = %(c)s ORDER BY u.email""",
            {"c": customer_id},
        ).fetchall()
    return rows if cursor is None else page_after(rows, cursor, limit)


class MemberIn(BaseModel):
    seat: Literal["agent", "internal"] = "agent"
    skills: list[str] = Field(default_factory=list, max_length=20)
    languages: list[str] = Field(default_factory=lambda: ["en"], max_length=20)
    available: bool = True


@router.put("/members/{user_id}")
def update_member(customer_id: str, user_id: str, body: MemberIn, user: UserDep) -> dict:
    """Set a person's seat. 'internal' seats read and write notes only."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        if str(user_id) != str(user.id):
            access.require_scope(user, "commai:admin")
            _no_internal(conn, user, customer_id)
        elif body.seat == "agent" and access.seat(conn, user, customer_id) == "internal":
            raise HTTPException(403, "Ask an administrator to change your seat.")
        ok = conn.execute("SELECT 1 FROM users WHERE id = %s AND customer_id = %s", (user_id, customer_id)).fetchone()
        if not ok:
            raise HTTPException(404, "Person not found in this business.")
        row = conn.execute(
            """INSERT INTO commai_members (customer_id, user_id, seat, skills, languages, available)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, user_id) DO UPDATE SET seat = EXCLUDED.seat, skills = EXCLUDED.skills,
                 languages = EXCLUDED.languages, available = EXCLUDED.available
               RETURNING *""",
            (customer_id, user_id, body.seat, body.skills, body.languages, body.available),
        ).fetchone()
        audit.record(conn, user.actor, "commai.member.update", str(user_id), customer_id, body.model_dump())
    return row


def _no_internal(conn, user, customer_id) -> None:
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your seat can't change settings.")


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    position: int = Field(default=100, ge=0, le=10000)
    match: dict = Field(default_factory=dict)
    team_id: str | None = None
    queue: str = Field(default="", max_length=60)
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    skills: list[str] = Field(default_factory=list, max_length=20, description="Skills the person picked must have")
    enabled: bool = True


def _check_match(m: dict) -> dict:
    allowed = {"channel", "keywords", "language", "intent"}
    extra = set(m) - allowed
    if extra:
        raise HTTPException(422, f"Rules can match on {', '.join(sorted(allowed))}; not {', '.join(sorted(extra))}.")
    if "keywords" in m and not (isinstance(m["keywords"], list) and all(isinstance(k, str) for k in m["keywords"])):
        raise HTTPException(422, "keywords must be a list of words.")
    return m


@router.get("/routing-rules")
def list_rules(
    customer_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict] | dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM commai_routing_rules WHERE customer_id = %s ORDER BY position, created_at", (customer_id,)
        ).fetchall()
    return rows if cursor is None else page_after(rows, cursor, limit)


@router.post("/routing-rules", status_code=201)
def create_rule(customer_id: str, body: RuleIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        if (
            body.team_id
            and not conn.execute(
                "SELECT 1 FROM commai_teams WHERE id = %s AND customer_id = %s", (body.team_id, customer_id)
            ).fetchone()
        ):
            raise HTTPException(404, "Team not found.")
        row = conn.execute(
            """INSERT INTO commai_routing_rules (customer_id, position, name, match, team_id, queue, priority, enabled,
                                                skills)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                customer_id,
                body.position,
                body.name,
                Jsonb(_check_match(body.match)),
                body.team_id,
                body.queue,
                body.priority,
                body.enabled,
                sorted({k.strip()[:40] for k in body.skills if k.strip()}),
            ),
        ).fetchone()
        audit.record(conn, user.actor, "commai.routing_rule.create", body.name, customer_id)
    return row


@router.delete("/routing-rules/{rule_id}", status_code=204)
def delete_rule(customer_id: str, rule_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        row = conn.execute(
            "DELETE FROM commai_routing_rules WHERE id = %s AND customer_id = %s RETURNING name", (rule_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Rule not found.")
        audit.record(conn, user.actor, "commai.routing_rule.delete", row["name"], customer_id)


class SettingsIn(BaseModel):
    mode: Literal["ai_first", "human_first", "human_only"] | None = None
    timezone: str | None = Field(default=None, max_length=60)
    first_reply_minutes: dict[str, float] | None = None
    resolve_hours: dict[str, float] | None = None


@router.get("/settings")
def get_settings(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        s = inbox.settings(conn, customer_id)
    return {**s, "ai_available": inbox.ai_available()}


@router.patch("/settings")
def update_settings(customer_id: str, body: SettingsIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    fields = body.model_dump(exclude_none=True)
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        inbox.settings(conn, customer_id)
        for k in ("first_reply_minutes", "resolve_hours"):
            if k in fields:
                if set(fields[k]) - set(inbox.PRIORITIES) or any(v <= 0 for v in fields[k].values()):
                    raise HTTPException(422, f"{k} takes positive numbers for low, normal, high and urgent.")
                fields[k] = Jsonb(fields[k])
        if fields:
            sets = ", ".join(f"{k} = %({k})s" for k in fields)
            conn.execute(
                f"UPDATE commai_settings SET {sets}, updated_at = now() WHERE customer_id = %(c)s",
                {**fields, "c": customer_id},
            )
        audit.record(conn, user.actor, "commai.settings.update", "", customer_id, {"fields": sorted(fields)})
        return inbox.settings(conn, customer_id)


# ---- live updates ---------------------------------------------------------------

live = APIRouter(tags=["commai: inbox"])


@live.websocket("/customers/{customer_id}/live")
async def live_updates(websocket: WebSocket, customer_id: str) -> None:
    """Live inbox events. Connect, then send {"token": "<session or API key>"}
    as the first message (or rely on the session cookie). Each event is sent
    as {"seq", "type", "subject", "data", "at"}; `after` resumes from a seq.
    Note events carry ids only, never note text."""
    await websocket.accept()
    try:
        first = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=10))
        token = first.get("token") or ""
        after = int(first.get("after") or 0)
        auth = f"Bearer {token}" if token else None
        cookie_token = websocket.cookies.get("exa_session")
        if not auth and cookie_token:
            auth = f"Bearer {cookie_token}"
        user = await asyncio.to_thread(_ws_user, websocket, auth)
        access.check(user, customer_id, "commai:read")
    except (HTTPException, ValueError, TimeoutError, json.JSONDecodeError):
        await websocket.close(code=4401)
        return
    try:
        if not after:
            with db.tx() as conn:
                row = conn.execute(
                    "SELECT coalesce(max(seq), 0) AS s FROM commai_events WHERE customer_id = %s", (customer_id,)
                ).fetchone()
            after = row["s"]
            await websocket.send_json({"type": "ready", "seq": after})
        while True:
            rows = await asyncio.to_thread(_events_after, customer_id, after)
            for r in rows:
                after = r["seq"]
                await websocket.send_json(
                    {
                        "seq": r["seq"],
                        "type": r["type"],
                        "subject": r["subject"],
                        "data": r["data"],
                        "at": r["at"].isoformat(),
                    }
                )
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


def _ws_user(websocket: WebSocket, auth: str | None):
    class _Req:  # current_user only reads the path
        url = websocket.url

    return current_user(_Req(), auth)  # type: ignore[arg-type]


def _events_after(customer_id: str, after: int) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            "SELECT seq, type, subject, data, at FROM commai_events WHERE customer_id = %s AND seq > %s"
            " ORDER BY seq LIMIT 200",
            (customer_id, after),
        ).fetchall()
