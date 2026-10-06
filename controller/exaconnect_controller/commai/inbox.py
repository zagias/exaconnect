"""The shared inbox: contacts, conversations, messages, notes and who handles
what (ADR 0016).

A conversation is the unit of work. It has one state, one owner (a person, a
team or a queue) and one active handler at a time: the AI or a person. A
person replying takes over from the AI at once; the AI may only reply while it
is the handler, checked under a row lock when its reply is written, so a
takeover always wins.

Customer-facing messages and private notes are separate tables. Nothing in
this module that returns customer-facing data ever reads commai_notes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import channels, events, jobs

STATES = ("open", "awaiting_customer", "awaiting_internal", "snoozed", "resolved", "reopened")
PRIORITIES = ("low", "normal", "high", "urgent")
REOPEN_WITHIN = dt.timedelta(days=7)
STATUS_ORDER = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}


class InboxError(Exception):
    """A request the inbox refuses. `code` maps to an HTTP status."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


class NotHandler(InboxError):
    """The AI tried to reply after a person took over (or handed over)."""

    def __init__(self, message: str = "The AI is no longer handling this conversation."):
        super().__init__(message, 409)


# ---- settings -----------------------------------------------------------------


def settings(conn: psycopg.Connection, customer_id: Any) -> dict:
    conn.execute("INSERT INTO commai_settings (customer_id) VALUES (%s) ON CONFLICT DO NOTHING", (customer_id,))
    return conn.execute("SELECT * FROM commai_settings WHERE customer_id = %s", (customer_id,)).fetchone()


def ai_available() -> bool:
    """True once an AI module has registered its reply job."""
    return "ai.respond" in jobs._handlers


# ---- contacts -----------------------------------------------------------------


def find_or_create_identity(
    conn: psycopg.Connection,
    customer_id: Any,
    channel: str,
    address: str,
    *,
    name: str = "",
    verified: bool = False,
    contact_id: Any = None,
) -> dict:
    """The identity for this channel address, creating the contact on first contact.
    Returns the identity row joined with contact fields (contact_name...)."""
    address = address.strip()
    if not address:
        raise InboxError("A channel address is required.")
    row = conn.execute(
        "SELECT * FROM contact_identities WHERE customer_id = %s AND channel = %s AND address = %s",
        (customer_id, channel, address),
    ).fetchone()
    if row is None:
        if contact_id is None:
            email = address if channel == "email" else ""
            phone = address if channel in ("whatsapp", "sms") else ""
            c = conn.execute(
                "INSERT INTO contacts (customer_id, name, email, phone) VALUES (%s, %s, %s, %s) RETURNING id",
                (customer_id, name, email, phone),
            ).fetchone()
            contact_id = c["id"]
            events.emit(conn, customer_id, "contact.created", {"contact_id": str(contact_id)}, contact_id)
        row = conn.execute(
            """INSERT INTO contact_identities (customer_id, contact_id, channel, address, verified)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, channel, address) DO UPDATE SET address = EXCLUDED.address
               RETURNING *""",
            (customer_id, contact_id, channel, address, verified),
        ).fetchone()
    elif verified and not row["verified"]:
        row = conn.execute(
            "UPDATE contact_identities SET verified = true WHERE id = %s RETURNING *", (row["id"],)
        ).fetchone()
    if name:
        conn.execute("UPDATE contacts SET name = %s WHERE id = %s AND name = ''", (name, row["contact_id"]))
    return row


# ---- conversations ------------------------------------------------------------


def _log(conn, conv: dict, actor: str, kind: str, before: Any, after: Any, reason: str = "") -> None:
    conn.execute(
        """INSERT INTO conversation_log (customer_id, conversation_id, actor, kind, from_value, to_value, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (conv["customer_id"], conv["id"], actor, kind, str(before or ""), str(after or ""), reason),
    )


def get(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, *, lock: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = %s AND customer_id = %s" + (" FOR UPDATE" if lock else ""),
        (conversation_id, customer_id),
    ).fetchone()
    if row is None:
        raise InboxError("Conversation not found.", 404)
    return row


def _due(conn, customer_id: Any, priority: str) -> tuple[dt.datetime, dt.datetime]:
    s = settings(conn, customer_id)
    now = dt.datetime.now(dt.UTC)
    first = dt.timedelta(minutes=float(s["first_reply_minutes"].get(priority, 60)))
    resolve = dt.timedelta(hours=float(s["resolve_hours"].get(priority, 24)))
    return now + first, now + resolve


def route(conn: psycopg.Connection, conv: dict, text: str) -> dict:
    """Apply the business's routing rules, then pick the available team member
    with the fewest open conversations. Returns the updated conversation."""
    rules = conn.execute(
        "SELECT * FROM commai_routing_rules WHERE customer_id = %s AND enabled ORDER BY position, created_at",
        (conv["customer_id"],),
    ).fetchall()
    low = text.lower()
    team_id, queue, priority, rule_name = conv["team_id"], conv["queue"], conv["priority"], ""
    for r in rules:
        m = r["match"] or {}
        if m.get("channel") and m["channel"] != conv["channel"]:
            continue
        if m.get("language") and m["language"] != (conv["language"] or ""):
            continue
        if m.get("intent") and m["intent"] != (conv.get("intent") or ""):
            continue
        kws = [k.lower() for k in m.get("keywords") or [] if k]
        if kws and not any(k in low for k in kws):
            continue
        team_id = r["team_id"] or team_id
        queue = r["queue"] or queue
        priority = r["priority"] or priority
        rule_name = r["name"]
        break
    assignee = None
    if team_id:
        pick = conn.execute(
            """SELECT tm.user_id FROM commai_team_members tm
               JOIN commai_members m ON m.user_id = tm.user_id AND m.customer_id = %(c)s
               WHERE tm.team_id = %(t)s AND m.available AND m.seat = 'agent'
                 AND (%(lang)s = '' OR %(lang)s = ANY(m.languages))
               ORDER BY (SELECT count(*) FROM conversations c WHERE c.assignee_id = tm.user_id
                         AND c.state NOT IN ('resolved', 'snoozed')), tm.user_id
               LIMIT 1""",
            {"c": conv["customer_id"], "t": team_id, "lang": conv["language"] or ""},
        ).fetchone()
        assignee = pick["user_id"] if pick else None
    first_due, resolve_due = _due(conn, conv["customer_id"], priority)
    updated = conn.execute(
        """UPDATE conversations SET team_id = %s, queue = %s, priority = %s, assignee_id = %s,
                  first_reply_due = COALESCE(first_reply_due, %s), resolve_due = COALESCE(resolve_due, %s),
                  updated_at = now()
           WHERE id = %s RETURNING *""",
        (team_id, queue, priority, assignee, first_due, resolve_due, conv["id"]),
    ).fetchone()
    if team_id != conv["team_id"] or assignee != conv["assignee_id"]:
        _log(
            conn,
            conv,
            "system:routing",
            "assign",
            conv["assignee_id"] or conv["team_id"],
            assignee or team_id,
            f"rule: {rule_name}" if rule_name else "default routing",
        )
        events.emit(
            conn,
            conv["customer_id"],
            "conversation.assigned",
            {
                "conversation_id": str(conv["id"]),
                "team_id": str(team_id or ""),
                "assignee_id": str(assignee or ""),
                "rule": rule_name,
            },
            conv["id"],
        )
    return updated


def open_conversation(
    conn: psycopg.Connection,
    customer_id: Any,
    identity: dict,
    channel: str,
    *,
    subject: str = "",
    language: str = "",
    actor: str = "system",
) -> dict:
    s = settings(conn, customer_id)
    handler = "ai" if s["mode"] == "ai_first" and ai_available() else "none"
    conv = conn.execute(
        """INSERT INTO conversations (customer_id, contact_id, identity_id, channel, subject, language, handler)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, identity["contact_id"], identity["id"], channel, subject, language, handler),
    ).fetchone()
    _log(conn, conv, actor, "state", "", "open", "created")
    events.emit(
        conn,
        customer_id,
        "conversation.created",
        {"conversation_id": str(conv["id"]), "channel": channel, "contact_id": str(identity["contact_id"])},
        conv["id"],
    )
    return conv


def receive(
    conn: psycopg.Connection,
    customer_id: Any,
    channel: str,
    address: str,
    body: str,
    *,
    external_id: str | None = None,
    name: str = "",
    attachments: list | None = None,
    subject: str = "",
    language: str = "",
    verified: bool = False,
    conversation_id: Any = None,
) -> dict:
    """An inbound message from a contact. Returns {"conversation", "message",
    "duplicate"}. The same external_id is only ever stored once."""
    if external_id:
        dup = conn.execute(
            "SELECT * FROM messages WHERE customer_id = %s AND external_id = %s", (customer_id, external_id)
        ).fetchone()
        if dup:
            conv = get(conn, customer_id, dup["conversation_id"])
            return {"conversation": conv, "message": dup, "duplicate": True}
    identity = find_or_create_identity(conn, customer_id, channel, address, name=name, verified=verified)
    conv = None
    if conversation_id:
        conv = get(conn, customer_id, conversation_id, lock=True)
        if conv["identity_id"] != identity["id"]:
            raise InboxError("That conversation belongs to someone else.", 403)
    if conv is None:
        conv = conn.execute(
            """SELECT * FROM conversations WHERE identity_id = %s AND channel = %s
               ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
            (identity["id"], channel),
        ).fetchone()
    created = False
    if conv is None or (
        conv["state"] == "resolved"
        and conv["resolved_at"] is not None
        and conv["resolved_at"] < dt.datetime.now(dt.UTC) - REOPEN_WITHIN
    ):
        conv = open_conversation(conn, customer_id, identity, channel, subject=subject, language=language)
        created = True
    elif conv["state"] in ("resolved", "awaiting_customer", "snoozed"):
        new_state = "reopened" if conv["state"] == "resolved" else "open"
        _log(conn, conv, "contact", "state", conv["state"], new_state, "customer wrote")
        conv = conn.execute(
            """UPDATE conversations SET state = %s, snoozed_until = NULL, resolved_at = NULL,
                      handler = CASE WHEN %s = 'reopened' AND handler = 'none' AND %s THEN 'ai' ELSE handler END
               WHERE id = %s RETURNING *""",
            (new_state, new_state, settings(conn, customer_id)["mode"] == "ai_first" and ai_available(), conv["id"]),
        ).fetchone()
        events.emit(
            conn,
            customer_id,
            "conversation.state_changed",
            {"conversation_id": str(conv["id"]), "from": "", "to": new_state},
            conv["id"],
        )
    msg = conn.execute(
        """INSERT INTO messages (customer_id, conversation_id, direction, author_kind, author, body,
                                 attachments, status, external_id)
           VALUES (%s, %s, 'in', 'contact', %s, %s, %s, 'received', %s) RETURNING *""",
        (customer_id, conv["id"], str(identity["contact_id"]), body, Jsonb(attachments or []), external_id),
    ).fetchone()
    conv = conn.execute(
        "UPDATE conversations SET last_inbound_at = now(), last_message_at = now(), updated_at = now()"
        " WHERE id = %s RETURNING *",
        (conv["id"],),
    ).fetchone()
    if created:
        conv = route(conn, conv, f"{subject} {body}")
    events.emit(
        conn,
        customer_id,
        "message.received",
        {"conversation_id": str(conv["id"]), "message_id": str(msg["id"]), "channel": channel},
        conv["id"],
    )
    if conv["handler"] == "ai":
        jobs.enqueue(
            conn,
            "ai.respond",
            {"conversation_id": str(conv["id"]), "message_id": str(msg["id"])},
            customer_id=customer_id,
            dedupe_key=f"ai:{msg['id']}",
        )
    return {"conversation": conv, "message": msg, "duplicate": False}


def set_handler(conn: psycopg.Connection, conv: dict, handler: str, user_id: Any, actor: str, reason: str) -> dict:
    if conv["handler"] == handler and str(conv["handler_user_id"] or "") == str(user_id or ""):
        return conv
    updated = conn.execute(
        "UPDATE conversations SET handler = %s, handler_user_id = %s, updated_at = now() WHERE id = %s RETURNING *",
        (handler, user_id, conv["id"]),
    ).fetchone()
    before = conv["handler"] + (f":{conv['handler_user_id']}" if conv["handler_user_id"] else "")
    after = handler + (f":{user_id}" if user_id else "")
    _log(conn, conv, actor, "handler", before, after, reason)
    events.emit(
        conn,
        conv["customer_id"],
        "conversation.handler_changed",
        {"conversation_id": str(conv["id"]), "from": conv["handler"], "to": handler, "user_id": str(user_id or "")},
        conv["id"],
    )
    return updated


def take_over(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, user_id: Any, actor: str) -> dict:
    """A person becomes the handler. The AI stops at once: its next reply is refused."""
    conv = get(conn, customer_id, conversation_id, lock=True)
    conv = set_handler(conn, conv, "human", user_id, actor, "taken over")
    if conv["assignee_id"] is None:
        conn.execute("UPDATE conversations SET assignee_id = %s WHERE id = %s", (user_id, conv["id"]))
        _log(conn, conv, actor, "assign", "", user_id, "took over")
        conv = get(conn, customer_id, conversation_id)
    return conv


def hand_to_ai(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, actor: str) -> dict:
    if not ai_available():
        raise InboxError("No AI agent is set up for this business yet.", 409)
    conv = get(conn, customer_id, conversation_id, lock=True)
    return set_handler(conn, conv, "ai", None, actor, "handed back to the AI")


def hand_over(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    *,
    reason: str,
    packet: dict,
    holding_reply: str | None = None,
    actor: str = "ai",
) -> dict:
    """The AI hands the conversation to people with what it collected, what it
    tried and why. The customer gets a holding reply, never silence."""
    conv = get(conn, customer_id, conversation_id, lock=True)
    if holding_reply:
        _insert_out(conn, conv, holding_reply, "system", "CommAI")
    conn.execute(
        "INSERT INTO handovers (customer_id, conversation_id, reason, packet) VALUES (%s, %s, %s, %s)",
        (customer_id, conv["id"], reason, Jsonb(packet)),
    )
    conv = set_handler(conn, conv, "none", None, actor, f"handed over: {reason}")
    if conv["state"] not in ("open", "reopened"):
        conv = set_state(conn, customer_id, conv["id"], "open", actor=actor, reason="handed over")
    if conv["assignee_id"] is None:
        text = " ".join(
            m["body"]
            for m in conn.execute(
                "SELECT body FROM messages WHERE conversation_id = %s AND direction = 'in' ORDER BY created_at LIMIT 5",
                (conv["id"],),
            ).fetchall()
        )
        conv = route(conn, conv, text)
    events.emit(
        conn,
        customer_id,
        "conversation.handed_over",
        {"conversation_id": str(conv["id"]), "reason": reason},
        conv["id"],
    )
    return conv


def assign(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    *,
    actor: str,
    assignee_id: Any = None,
    team_id: Any = None,
    queue: str | None = None,
    reason: str = "",
) -> dict:
    conv = get(conn, customer_id, conversation_id, lock=True)
    if assignee_id is not None:
        ok = conn.execute(
            """SELECT 1 FROM users u WHERE u.id = %s AND (u.customer_id = %s OR u.role = 'admin')
               AND NOT EXISTS (SELECT 1 FROM commai_members m WHERE m.user_id = u.id AND m.customer_id = %s
                               AND m.seat = 'internal')""",
            (assignee_id, customer_id, customer_id),
        ).fetchone()
        if not ok:
            raise InboxError("That person can't be assigned conversations for this business.")
    if team_id is not None:
        ok = conn.execute(
            "SELECT 1 FROM commai_teams WHERE id = %s AND customer_id = %s", (team_id, customer_id)
        ).fetchone()
        if not ok:
            raise InboxError("Team not found.", 404)
    new_assignee = assignee_id if assignee_id is not None else conv["assignee_id"]
    new_team = team_id if team_id is not None else conv["team_id"]
    if team_id is not None and assignee_id is None and team_id != conv["team_id"]:
        new_assignee = None  # moving to another team's queue
    updated = conn.execute(
        "UPDATE conversations SET assignee_id = %s, team_id = %s, queue = %s, updated_at = now() WHERE id = %s"
        " RETURNING *",
        (new_assignee, new_team, queue if queue is not None else conv["queue"], conv["id"]),
    ).fetchone()
    _log(
        conn,
        conv,
        actor,
        "assign",
        conv["assignee_id"] or conv["team_id"] or "",
        new_assignee or new_team or "",
        reason or "transfer",
    )
    events.emit(
        conn,
        customer_id,
        "conversation.assigned",
        {"conversation_id": str(conv["id"]), "assignee_id": str(new_assignee or ""), "team_id": str(new_team or "")},
        conv["id"],
    )
    if team_id is not None and assignee_id is None and new_assignee is None:
        updated = route_within_team(conn, updated)
    return updated


def route_within_team(conn, conv: dict) -> dict:
    pick = conn.execute(
        """SELECT tm.user_id FROM commai_team_members tm
           JOIN commai_members m ON m.user_id = tm.user_id AND m.customer_id = %s
           WHERE tm.team_id = %s AND m.available AND m.seat = 'agent'
           ORDER BY (SELECT count(*) FROM conversations c WHERE c.assignee_id = tm.user_id
                     AND c.state NOT IN ('resolved', 'snoozed')), tm.user_id LIMIT 1""",
        (conv["customer_id"], conv["team_id"]),
    ).fetchone()
    if not pick:
        return conv
    return conn.execute(
        "UPDATE conversations SET assignee_id = %s WHERE id = %s RETURNING *", (pick["user_id"], conv["id"])
    ).fetchone()


def set_state(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    state: str,
    *,
    actor: str,
    reason: str = "",
    snoozed_until: dt.datetime | None = None,
) -> dict:
    if state not in STATES:
        raise InboxError(f"Unknown state {state}.")
    conv = get(conn, customer_id, conversation_id, lock=True)
    if state == "snoozed" and snoozed_until is None:
        raise InboxError("Say until when to snooze.")
    if conv["state"] == state and state != "snoozed":
        return conv
    resolved_by = actor if state == "resolved" else ""
    updated = conn.execute(
        """UPDATE conversations SET state = %s, snoozed_until = %s,
                  resolved_at = CASE WHEN %s = 'resolved' THEN now() ELSE NULL END,
                  resolved_by = %s,
                  handler = CASE WHEN %s = 'resolved' THEN 'none' ELSE handler END,
                  handler_user_id = CASE WHEN %s = 'resolved' THEN NULL ELSE handler_user_id END,
                  updated_at = now()
           WHERE id = %s RETURNING *""",
        (state, snoozed_until if state == "snoozed" else None, state, resolved_by, state, state, conv["id"]),
    ).fetchone()
    _log(conn, conv, actor, "state", conv["state"], state, reason)
    events.emit(
        conn,
        customer_id,
        "conversation.state_changed",
        {"conversation_id": str(conv["id"]), "from": conv["state"], "to": state, "by": actor},
        conv["id"],
    )
    return updated


def set_fields(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    *,
    actor: str,
    priority: str | None = None,
    tags: list[str] | None = None,
    subject: str | None = None,
    language: str | None = None,
) -> dict:
    conv = get(conn, customer_id, conversation_id, lock=True)
    if priority is not None and priority not in PRIORITIES:
        raise InboxError(f"Unknown priority {priority}.")
    updated = conn.execute(
        """UPDATE conversations SET priority = COALESCE(%s, priority), tags = COALESCE(%s, tags),
                  subject = COALESCE(%s, subject), language = COALESCE(%s, language), updated_at = now()
           WHERE id = %s RETURNING *""",
        (
            priority,
            [t.strip()[:40] for t in tags if t.strip()] if tags is not None else None,
            subject,
            language,
            conv["id"],
        ),
    ).fetchone()
    if priority is not None and priority != conv["priority"]:
        _log(conn, conv, actor, "priority", conv["priority"], priority)
    if tags is not None and sorted(tags) != sorted(conv["tags"]):
        _log(conn, conv, actor, "tags", ",".join(conv["tags"]), ",".join(updated["tags"]))
    return updated


# ---- messages -----------------------------------------------------------------


def _insert_out(
    conn,
    conv: dict,
    body: str,
    author_kind: str,
    author: str,
    *,
    template: str = "",
    attachments: list | None = None,
    original_body: str = "",
    original_language: str = "",
) -> dict:
    ch = channels.get(conv["channel"])
    status = "queued" if ch.external else "sent"
    msg = conn.execute(
        """INSERT INTO messages (customer_id, conversation_id, direction, author_kind, author, body, template,
                                 attachments, status, original_body, original_language)
           VALUES (%s, %s, 'out', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            conv["customer_id"],
            conv["id"],
            author_kind,
            author,
            body,
            template,
            Jsonb(attachments or []),
            status,
            original_body,
            original_language,
        ),
    ).fetchone()
    conn.execute("UPDATE conversations SET last_message_at = now(), updated_at = now() WHERE id = %s", (conv["id"],))
    if ch.external:
        jobs.enqueue(
            conn,
            "message.send",
            {"message_id": str(msg["id"])},
            customer_id=conv["customer_id"],
            dedupe_key=f"send:{msg['id']}",
        )
    events.emit(
        conn,
        conv["customer_id"],
        "message.sent",
        {
            "conversation_id": str(conv["id"]),
            "message_id": str(msg["id"]),
            "author_kind": author_kind,
            "channel": conv["channel"],
        },
        conv["id"],
    )
    return msg


def send(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    body: str,
    *,
    author_kind: str,
    author: str,
    user_id: Any = None,
    template: str = "",
    attachments: list | None = None,
    take_over: bool = False,
    original_body: str = "",
    original_language: str = "",
) -> dict:
    """A reply on the conversation's own channel, after the channel's rules.

    - A person replying while the AI handles the conversation takes over.
    - A person replying while someone else handles it is refused unless they
      take over explicitly, so a second person sees who is handling it first.
    - The AI may only reply while it is the handler.
    """
    body = (body or "").strip()
    if not body and not template:
        raise InboxError("Write a reply first.")
    conv = get(conn, customer_id, conversation_id, lock=True)
    if author_kind == "ai":
        if conv["handler"] != "ai":
            raise NotHandler()
    elif author_kind == "user":
        if conv["handler"] == "human" and str(conv["handler_user_id"]) != str(user_id) and not take_over:
            who = conn.execute("SELECT email FROM users WHERE id = %s", (conv["handler_user_id"],)).fetchone()
            raise InboxError(
                f"{who['email'] if who else 'Someone else'} is handling this conversation. Take over to reply.", 409
            )
        if conv["handler"] != "human" or str(conv["handler_user_id"]) != str(user_id):
            conv = set_handler(conn, conv, "human", user_id, author, "replied")
    ch = channels.get(conv["channel"])
    try:
        ch.check_send(conn, conv, body, template)
    except channels.SendBlocked as e:
        events.emit(
            conn,
            customer_id,
            "message.blocked",
            {"conversation_id": str(conv["id"]), "reason": str(e), "author_kind": author_kind},
            conv["id"],
        )
        raise InboxError(str(e), 422) from e
    msg = _insert_out(
        conn,
        conv,
        body,
        author_kind,
        author,
        template=template,
        attachments=attachments,
        original_body=original_body,
        original_language=original_language,
    )
    if author_kind in ("user", "ai"):
        conn.execute(
            """UPDATE conversations SET first_reply_at = COALESCE(first_reply_at, now()),
                      state = CASE WHEN state IN ('open', 'reopened') THEN 'awaiting_customer' ELSE state END
               WHERE id = %s""",
            (conv["id"],),
        )
        if conv["state"] in ("open", "reopened"):
            _log(conn, conv, author, "state", conv["state"], "awaiting_customer", "replied")
    return msg


def add_note(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    *,
    author: str,
    body: str,
    mentions: list[str] | None = None,
    attachments: list | None = None,
) -> dict:
    body = (body or "").strip()
    if not body:
        raise InboxError("Write a note first.")
    conv = get(conn, customer_id, conversation_id)
    note = conn.execute(
        """INSERT INTO commai_notes (customer_id, conversation_id, author, body, mentions, attachments)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, conv["id"], author, body, mentions or [], Jsonb(attachments or [])),
    ).fetchone()
    # The event names the note but never carries its text: webhooks can reach
    # customer-facing systems.
    events.emit(
        conn,
        customer_id,
        "note.created",
        {"conversation_id": str(conv["id"]), "note_id": str(note["id"]), "mentions": mentions or []},
        conv["id"],
    )
    return note


def messages(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, after: Any = None) -> list[dict]:
    """Customer-facing messages only."""
    return conn.execute(
        """SELECT * FROM messages WHERE customer_id = %s AND conversation_id = %s
           AND (%s::timestamptz IS NULL OR created_at > %s::timestamptz) ORDER BY created_at, id""",
        (customer_id, conversation_id, after, after),
    ).fetchall()


def update_status(conn: psycopg.Connection, provider_ref: str, status: str, error: str = "") -> dict | None:
    """Delivery and read receipts. A status never goes backwards (a late
    'delivered' after 'read' is ignored); 'failed' always lands."""
    row = conn.execute("SELECT * FROM messages WHERE provider_ref = %s FOR UPDATE", (provider_ref,)).fetchone()
    if row is None:
        return None
    if status != "failed" and STATUS_ORDER.get(status, -1) <= STATUS_ORDER.get(row["status"], -1):
        return row
    row = conn.execute(
        "UPDATE messages SET status = %s, error = %s, updated_at = now() WHERE id = %s RETURNING *",
        (status, error, row["id"]),
    ).fetchone()
    events.emit(
        conn,
        row["customer_id"],
        "message.status",
        {"conversation_id": str(row["conversation_id"]), "message_id": str(row["id"]), "status": status},
        row["conversation_id"],
    )
    return row


@jobs.handler("message.send")
def _send_job(conn: psycopg.Connection, job: dict):
    msg = conn.execute("SELECT * FROM messages WHERE id = %s FOR UPDATE", (job["payload"]["message_id"],)).fetchone()
    if msg is None or msg["status"] != "queued":
        return None  # already sent: a retry must not send twice
    conv = conn.execute("SELECT * FROM conversations WHERE id = %s", (msg["conversation_id"],)).fetchone()
    ch = channels.get(conv["channel"])
    try:
        out = ch.deliver(conn, conv, msg)
    except channels.SendBlocked as e:
        conn.execute("UPDATE messages SET status = 'blocked', error = %s WHERE id = %s", (str(e), msg["id"]))
        return None
    except Exception as e:  # noqa: BLE001 - the provider failed; try again later
        conn.execute("UPDATE messages SET error = %s WHERE id = %s", (f"{type(e).__name__}: {e}"[:500], msg["id"]))
        return jobs.Later(str(e), delay_s=10)
    conn.execute(
        "UPDATE messages SET status = %s, provider_ref = %s, error = '', updated_at = now() WHERE id = %s",
        (out.get("status", "sent"), out.get("provider_ref", ""), msg["id"]),
    )
    return None


@jobs.on_dead("message.send")
def _send_dead(job: dict, error: str) -> None:
    from .. import db

    with db.tx() as conn:
        row = conn.execute(
            "UPDATE messages SET status = 'failed', error = %s WHERE id = %s AND status = 'queued' RETURNING *",
            (error[:500], job["payload"]["message_id"]),
        ).fetchone()
        if row:
            events.emit(
                conn,
                row["customer_id"],
                "message.status",
                {"conversation_id": str(row["conversation_id"]), "message_id": str(row["id"]), "status": "failed"},
                row["conversation_id"],
            )
