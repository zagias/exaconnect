"""Staff-only inbox features API (ADR 0032): notifications, files on notes,
saved views, staff chat and satisfaction survey settings and results.

Staff chat and notifications belong to the person signed in: API keys are
refused. Note files need the commai:notes scope, like the notes themselves,
so a customer-facing key can never read one. Every write is audited.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import User, UserDep
from .. import access, csat, inbox, team
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: team"])


@contextmanager
def _errors() -> Iterator[None]:
    try:
        with errors():
            yield
    except team.TeamError as e:
        raise HTTPException(e.code, str(e)) from e


def _person(user: User, customer_id: str) -> None:
    """Staff chat and notifications: people only, never API keys."""
    access.check(user, customer_id, "commai:read")
    if user.via == "key":
        raise HTTPException(403, "Staff chat and notifications are for people signed in, not API keys.")


# ---- notifications ------------------------------------------------------------------------


@router.get("/notifications")
def my_notifications(customer_id: str, user: UserDep, unread: bool = False) -> dict:
    _person(user, customer_id)
    with db.tx() as conn:
        items = team.notifications(conn, customer_id, user.id, unread)
    return {"items": items, "unread": sum(1 for i in items if i["read_at"] is None)}


@router.post("/notifications/read")
def read_all(customer_id: str, user: UserDep) -> dict:
    _person(user, customer_id)
    with db.tx() as conn:
        return {"marked": team.mark_read(conn, customer_id, user.id, None)}


@router.post("/notifications/{notification_id}/read")
def read_one(customer_id: str, notification_id: str, user: UserDep) -> dict:
    _person(user, customer_id)
    with db.tx() as conn:
        return {"marked": team.mark_read(conn, customer_id, user.id, notification_id)}


@router.get("/people")
def list_people(customer_id: str, user: UserDep) -> list[dict]:
    """The business's people, for mentions and staff chat."""
    _person(user, customer_id)
    with db.tx() as conn:
        return team.people(conn, customer_id)


# ---- files on notes -----------------------------------------------------------------------


class NoteFileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(max_length=100)
    data: str = Field(description="base64", max_length=7_000_000)


@router.post("/notes/{note_id}/files", status_code=201)
def add_note_file(customer_id: str, note_id: str, body: NoteFileIn, user: UserDep) -> dict:
    """Attach a file to a private note (up to 5 MB: text, CSV, PDF, images, voice notes)."""
    access.check(user, customer_id, "commai:notes")
    with db.tx() as conn, _errors():
        item = team.add_note_file(
            conn, customer_id, note_id, name=body.name, content_type=body.type, data_b64=body.data, actor=user.email
        )
        audit.record(conn, user.actor, "commai.note.file.add", item["id"], customer_id, {"note_id": note_id})
        return item


@router.get("/note-files/{file_id}")
def get_note_file(customer_id: str, file_id: str, user: UserDep) -> Response:
    access.check(user, customer_id, "commai:notes")
    with db.tx() as conn:
        f = team.note_file(conn, customer_id, file_id)
    if f is None:
        raise HTTPException(404, "File not found.")
    safe = "".join(c for c in f["name"] if c.isalnum() or c in "._- ")[:100] or "file"
    return Response(
        bytes(f["data"]),
        media_type=f["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )


# ---- saved views --------------------------------------------------------------------------


class ViewIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    filters: dict = Field(default_factory=dict)
    shared: bool = False


class ViewPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    filters: dict | None = None
    shared: bool | None = None


@router.get("/saved-views")
def list_views(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return team.saved_views(conn, customer_id, user.id)


@router.post("/saved-views", status_code=201)
def create_view(customer_id: str, body: ViewIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        row = team.save_view(conn, customer_id, user.id, body.name, body.filters, body.shared)
        audit.record(conn, user.actor, "commai.saved_view.create", str(row["id"]), customer_id)
        return row


@router.patch("/saved-views/{view_id}")
def update_view(customer_id: str, view_id: str, body: ViewPatch, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        row = team.update_view(conn, customer_id, user.id, view_id, **body.model_dump())
        audit.record(conn, user.actor, "commai.saved_view.update", view_id, customer_id)
        return row


@router.delete("/saved-views/{view_id}", status_code=204)
def delete_view(customer_id: str, view_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        team.delete_view(conn, customer_id, user.id, view_id)
        audit.record(conn, user.actor, "commai.saved_view.delete", view_id, customer_id)


# ---- staff chat ---------------------------------------------------------------------------


class ChatIn(BaseModel):
    kind: str = Field(pattern="^(direct|group)$")
    members: list[str] = Field(min_length=1, max_length=50)
    name: str = Field(default="", max_length=80)


class PostIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.get("/staff-chat")
def list_chats(customer_id: str, user: UserDep) -> list[dict]:
    _person(user, customer_id)
    with db.tx() as conn:
        return team.chats(conn, customer_id, user.id)


@router.post("/staff-chat", status_code=201)
def open_chat(customer_id: str, body: ChatIn, user: UserDep) -> dict:
    _person(user, customer_id)
    with db.tx() as conn, _errors():
        chat = team.open_chat(
            conn, customer_id, user.id, user.actor, kind=body.kind, members=body.members, name=body.name
        )
        audit.record(conn, user.actor, "commai.staff_chat.open", str(chat["id"]), customer_id, {"kind": body.kind})
        return chat


@router.get("/staff-chat/{chat_id}/messages")
def chat_messages(customer_id: str, chat_id: str, user: UserDep) -> list[dict]:
    _person(user, customer_id)
    with db.tx() as conn, _errors():
        return team.chat_messages(conn, customer_id, chat_id, user.id)


@router.post("/staff-chat/{chat_id}/messages", status_code=201)
def post_message(customer_id: str, chat_id: str, body: PostIn, user: UserDep) -> dict:
    _person(user, customer_id)
    with db.tx() as conn, _errors():
        msg = team.post(conn, customer_id, chat_id, user.id, user.email, body.body)
        audit.record(conn, user.actor, "commai.staff_chat.post", str(msg["id"]), customer_id, {"chat_id": chat_id})
        return msg


# ---- satisfaction surveys -----------------------------------------------------------------


class CsatIn(BaseModel):
    enabled: bool | None = None
    delay_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    cooldown_days: int | None = Field(default=None, ge=0, le=365)


@router.get("/csat")
def csat_overview(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    import datetime as dt

    with db.tx() as conn:
        end = dt.datetime.now(dt.UTC)
        return {
            "settings": csat.config(conn, customer_id),
            "last_30_days": csat.summary(conn, customer_id, end - dt.timedelta(days=30), end),
            "recent": csat.recent(conn, customer_id),
        }


@router.put("/csat")
def csat_settings(customer_id: str, body: CsatIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        if access.seat(conn, user, customer_id) == "internal":
            raise HTTPException(403, "Your seat can't change settings.")
        current = (inbox.settings(conn, customer_id)["config"] or {}).get("csat") or {}
        new = {**current, **body.model_dump(exclude_none=True)}
        conn.execute(
            """UPDATE commai_settings SET config = jsonb_set(config, '{csat}', %s), updated_at = now()
               WHERE customer_id = %s""",
            (Jsonb(new), customer_id),
        )
        audit.record(conn, user.actor, "commai.csat.settings", "", customer_id, body.model_dump(exclude_none=True))
        return csat.config(conn, customer_id)
