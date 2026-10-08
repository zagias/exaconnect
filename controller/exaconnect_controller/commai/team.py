"""Staff-only inbox features (ADR 0032): notifications, mentions on notes,
files on notes, saved views and staff chat.

Everything here is internal. Note files live in note_files and staff chat in
staff_chat_* tables: no customer path (the widget, channel webhooks,
customer-facing API keys, exports, the customer AI) reads either. Staff chat
and notifications are for people signed in; API keys are refused.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import events
from .ai import attachments


class TeamError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


# ---- notifications ----------------------------------------------------------------------


def notify(
    conn: psycopg.Connection,
    customer_id: Any,
    user_id: Any,
    kind: str,
    title: str,
    body: str = "",
    *,
    conversation_id: Any = None,
    ref: str = "",
) -> None:
    """Tell one person. With a ref, a repeat re-opens the same notification."""
    conn.execute(
        """INSERT INTO commai_notifications (customer_id, user_id, kind, title, body, conversation_id, ref)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, kind, ref) WHERE ref <> '' DO UPDATE SET title = EXCLUDED.title,
             body = EXCLUDED.body, read_at = NULL, created_at = now()""",
        (customer_id, user_id, kind, title[:200], body[:500], conversation_id, ref),
    )


def notifications(conn: psycopg.Connection, customer_id: Any, user_id: Any, unread_only: bool = False) -> list[dict]:
    return conn.execute(
        """SELECT id, kind, title, body, conversation_id, ref, read_at, created_at FROM commai_notifications
           WHERE customer_id = %s AND user_id = %s AND (NOT %s OR read_at IS NULL)
           ORDER BY (read_at IS NULL) DESC, created_at DESC LIMIT 100""",
        (customer_id, user_id, unread_only),
    ).fetchall()


def mark_read(conn: psycopg.Connection, customer_id: Any, user_id: Any, notification_id: Any | None) -> int:
    cur = conn.execute(
        """UPDATE commai_notifications SET read_at = now() WHERE customer_id = %s AND user_id = %s
           AND read_at IS NULL AND (%s::uuid IS NULL OR id = %s::uuid)""",
        (customer_id, user_id, notification_id, notification_id),
    )
    return cur.rowcount


def people(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """The business's own people: its accounts and its Jibsy members."""
    return conn.execute(
        """SELECT DISTINCT u.id, u.email FROM users u
           LEFT JOIN commai_members m ON m.user_id = u.id AND m.customer_id = %s
           WHERE u.customer_id = %s OR m.user_id IS NOT NULL ORDER BY u.email""",
        (customer_id, customer_id),
    ).fetchall()


def _person(conn, customer_id: Any, ref: str) -> dict | None:
    ref = ref.strip().lstrip("@").lower()
    if not ref:
        return None
    for p in people(conn, customer_id):
        if str(p["id"]) == ref or p["email"].lower() == ref:
            return p
    matches = [p for p in people(conn, customer_id) if p["email"].lower().split("@")[0] == ref]
    return matches[0] if len(matches) == 1 else None


# ---- mentions on notes ------------------------------------------------------------------

MENTION = re.compile(r"(?<![\w.])@([\w.+-]+(?:@[\w-]+(?:\.[\w-]+)+)?)")


def _on_note(conn: psycopg.Connection, customer_id: Any, _type: str, data: dict, _event_id: str) -> None:
    """Resolve @mentions on a new note to people in the business and notify them."""
    note = conn.execute(
        "SELECT id, conversation_id, author, body, mentions FROM commai_notes WHERE id = %s AND customer_id = %s",
        (data.get("note_id"), customer_id),
    ).fetchone()
    if note is None:
        return
    refs = list(note["mentions"] or []) + MENTION.findall(note["body"])
    found: dict[str, dict] = {}
    for r in refs:
        p = _person(conn, customer_id, r)
        if p and p["email"].lower() != note["author"].lower():
            found[str(p["id"])] = p
    conn.execute(
        "UPDATE commai_notes SET mentions = %s WHERE id = %s", ([p["email"] for p in found.values()], note["id"])
    )
    for p in found.values():
        notify(
            conn,
            customer_id,
            p["id"],
            "mention",
            f"{note['author']} mentioned you in a note",
            " ".join(note["body"].split())[:200],
            conversation_id=note["conversation_id"],
            ref=f"note:{note['id']}",
        )


events.listen("note.created", _on_note)


# ---- files on notes ---------------------------------------------------------------------

NOTE_FILE_MAX = 5 * 1024 * 1024
NOTE_FILES_PER_NOTE = 5


def add_note_file(
    conn: psycopg.Connection, customer_id: Any, note_id: Any, *, name: str, content_type: str, data_b64: str, actor: str
) -> dict:
    note = conn.execute(
        "SELECT id, conversation_id, attachments FROM commai_notes WHERE id = %s AND customer_id = %s FOR UPDATE",
        (note_id, customer_id),
    ).fetchone()
    if note is None:
        raise TeamError("Note not found.", 404)
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise TeamError("The file couldn't be read.") from e
    if len(note["attachments"] or []) >= NOTE_FILES_PER_NOTE:
        raise TeamError(f"A note can hold up to {NOTE_FILES_PER_NOTE} files.")
    try:
        mime = attachments.check(name, content_type, data, max_bytes=NOTE_FILE_MAX)
    except attachments.AttachmentError as e:
        raise TeamError(str(e), e.code) from e
    row = conn.execute(
        """INSERT INTO note_files (customer_id, conversation_id, note_id, uploaded_by, name, content_type, size, data)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, name, content_type, size, created_at""",
        (customer_id, note["conversation_id"], note["id"], actor, name, mime, len(data), data),
    ).fetchone()
    item = {"id": str(row["id"]), "name": row["name"], "type": row["content_type"], "size": row["size"]}
    conn.execute("UPDATE commai_notes SET attachments = attachments || %s WHERE id = %s", (Jsonb([item]), note["id"]))
    return item


def note_file(conn: psycopg.Connection, customer_id: Any, file_id: Any) -> dict | None:
    return conn.execute(
        "SELECT * FROM note_files WHERE id = %s AND customer_id = %s", (file_id, customer_id)
    ).fetchone()


# ---- saved views ------------------------------------------------------------------------

VIEW_FILTERS = {
    "state": str,
    "view": str,
    "channel": str,
    "team_id": str,
    "tag": str,
    "q": str,
}
VIEWS = ("all", "mine", "unassigned", "ai", "overdue", "open")


def clean_filters(f: dict) -> dict:
    out = {}
    for k, v in (f or {}).items():
        if k not in VIEW_FILTERS:
            raise TeamError(f"A saved view can't filter on {k}.")
        if v in (None, ""):
            continue
        v = str(v).strip()[:200]
        if k == "view" and v not in VIEWS:
            raise TeamError(f"Unknown view {v}.")
        out[k] = v
    return out


def saved_views(conn: psycopg.Connection, customer_id: Any, user_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT v.id, v.name, v.filters, v.shared, v.created_at, u.email AS owner,
                  (v.owner_user_id = %s) AS mine
           FROM commai_saved_views v JOIN users u ON u.id = v.owner_user_id
           WHERE v.customer_id = %s AND (v.owner_user_id = %s OR v.shared) ORDER BY v.name""",
        (user_id, customer_id, user_id),
    ).fetchall()


def save_view(conn, customer_id: Any, user_id: Any, name: str, filters: dict, shared: bool) -> dict:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise TeamError("Name the view.")
    try:
        return conn.execute(
            """INSERT INTO commai_saved_views (customer_id, owner_user_id, name, filters, shared)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (customer_id, user_id, name, Jsonb(clean_filters(filters)), shared),
        ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        raise TeamError("You already have a view with that name.", 409) from e


def update_view(conn, customer_id: Any, user_id: Any, view_id: Any, **fields: Any) -> dict:
    if "filters" in fields and fields["filters"] is not None:
        fields["filters"] = Jsonb(clean_filters(fields["filters"]))
    row = conn.execute(
        """UPDATE commai_saved_views SET name = COALESCE(%(name)s, name), filters = COALESCE(%(filters)s, filters),
                  shared = COALESCE(%(shared)s, shared)
           WHERE id = %(id)s AND customer_id = %(c)s AND owner_user_id = %(u)s RETURNING *""",
        {
            "name": fields.get("name"),
            "filters": fields.get("filters"),
            "shared": fields.get("shared"),
            "id": view_id,
            "c": customer_id,
            "u": user_id,
        },
    ).fetchone()
    if row is None:
        raise TeamError("You can change only your own views.", 404)
    return row


def delete_view(conn, customer_id: Any, user_id: Any, view_id: Any) -> None:
    cur = conn.execute(
        "DELETE FROM commai_saved_views WHERE id = %s AND customer_id = %s AND owner_user_id = %s",
        (view_id, customer_id, user_id),
    )
    if cur.rowcount == 0:
        raise TeamError("You can delete only your own views.", 404)


# ---- staff chat -------------------------------------------------------------------------


def _is_person(conn, customer_id: Any, user_id: Any) -> bool:
    return any(str(p["id"]) == str(user_id) for p in people(conn, customer_id))


def chats(conn: psycopg.Connection, customer_id: Any, user_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT c.id, c.kind, c.name, c.created_at, c.last_message_at,
                  array(SELECT u.email FROM staff_chat_members m2 JOIN users u ON u.id = m2.user_id
                        WHERE m2.chat_id = c.id ORDER BY u.email) AS members,
                  (SELECT count(*) FROM staff_chat_messages x WHERE x.chat_id = c.id
                     AND x.created_at > COALESCE(m.last_read_at, '-infinity') AND x.author_user_id <> %s) AS unread
           FROM staff_chats c JOIN staff_chat_members m ON m.chat_id = c.id AND m.user_id = %s
           WHERE c.customer_id = %s ORDER BY COALESCE(c.last_message_at, c.created_at) DESC""",
        (user_id, user_id, customer_id),
    ).fetchall()


def open_chat(
    conn: psycopg.Connection,
    customer_id: Any,
    user_id: Any,
    actor: str,
    *,
    kind: str,
    members: list[str],
    name: str = "",
) -> dict:
    """A direct chat with one other person (one per pair) or a named group."""
    ids = {str(user_id)}
    for m in members:
        p = _person(conn, customer_id, m)
        if p is None:
            raise TeamError(f"{m} isn't in this business.", 404)
        ids.add(str(p["id"]))
    if not _is_person(conn, customer_id, user_id):
        raise TeamError("Staff chat is for the business's own people.", 403)
    if kind == "direct":
        if len(ids) != 2:
            raise TeamError("A direct chat is between you and one other person.")
        key = ":".join(sorted(ids))
        existing = conn.execute(
            "SELECT * FROM staff_chats WHERE customer_id = %s AND direct_key = %s", (customer_id, key)
        ).fetchone()
        if existing:
            return existing
        chat = conn.execute(
            """INSERT INTO staff_chats (customer_id, kind, direct_key, created_by) VALUES (%s, 'direct', %s, %s)
               RETURNING *""",
            (customer_id, key, actor),
        ).fetchone()
    elif kind == "group":
        name = " ".join(name.split())[:80]
        if not name:
            raise TeamError("Name the group.")
        if len(ids) < 2:
            raise TeamError("Add at least one other person.")
        chat = conn.execute(
            "INSERT INTO staff_chats (customer_id, kind, name, created_by) VALUES (%s, 'group', %s, %s) RETURNING *",
            (customer_id, name, actor),
        ).fetchone()
    else:
        raise TeamError("A chat is direct or group.")
    for uid in ids:
        conn.execute("INSERT INTO staff_chat_members (chat_id, user_id) VALUES (%s, %s)", (chat["id"], uid))
    return chat


def member_chat(conn: psycopg.Connection, customer_id: Any, chat_id: Any, user_id: Any) -> dict:
    """The chat, only for its members (anyone else gets 'not found')."""
    row = conn.execute(
        """SELECT c.* FROM staff_chats c JOIN staff_chat_members m ON m.chat_id = c.id AND m.user_id = %s
           WHERE c.id = %s AND c.customer_id = %s""",
        (user_id, chat_id, customer_id),
    ).fetchone()
    if row is None:
        raise TeamError("Chat not found.", 404)
    return row


def chat_messages(conn: psycopg.Connection, customer_id: Any, chat_id: Any, user_id: Any) -> list[dict]:
    member_chat(conn, customer_id, chat_id, user_id)
    conn.execute(
        "UPDATE staff_chat_members SET last_read_at = now() WHERE chat_id = %s AND user_id = %s", (chat_id, user_id)
    )
    return conn.execute(
        """SELECT id, author, author_user_id, body, created_at FROM staff_chat_messages
           WHERE chat_id = %s ORDER BY created_at, id""",
        (chat_id,),
    ).fetchall()


def post(conn: psycopg.Connection, customer_id: Any, chat_id: Any, user_id: Any, author: str, body: str) -> dict:
    chat = member_chat(conn, customer_id, chat_id, user_id)
    body = (body or "").strip()
    if not body:
        raise TeamError("Write a message first.")
    if len(body) > 4000:
        raise TeamError("Keep a message under 4,000 characters.")
    msg = conn.execute(
        """INSERT INTO staff_chat_messages (customer_id, chat_id, author_user_id, author, body)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, chat_id, user_id, author, body),
    ).fetchone()
    conn.execute("UPDATE staff_chats SET last_message_at = now() WHERE id = %s", (chat_id,))
    conn.execute(
        "UPDATE staff_chat_members SET last_read_at = now() WHERE chat_id = %s AND user_id = %s", (chat_id, user_id)
    )
    title = f"{author} in {chat['name']}" if chat["kind"] == "group" else f"Message from {author}"
    for m in conn.execute(
        "SELECT user_id FROM staff_chat_members WHERE chat_id = %s AND user_id <> %s", (chat_id, user_id)
    ).fetchall():
        notify(conn, customer_id, m["user_id"], "staff_chat", title, body[:200], ref=f"chat:{chat_id}")
    # No event is recorded: events feed webhooks, and staff chat stays inside.
    return msg
