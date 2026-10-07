"""Inbox, contact and widget extras (ADR 0032): service-target settings,
contact identities and history, typing and presence, staff attachments, and
the website visitor's AI browser call."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, inbox, inbox_jobs

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: inbox"])
public = APIRouter(tags=["commai: channels (public)"])


def _no_internal(conn, user, customer_id) -> None:
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change settings.")


# ---- service targets ---------------------------------------------------------------


class TargetsIn(BaseModel):
    reminders: bool = True
    remind_percent: int = Field(default=80, ge=10, le=99, description="Remind when this share of the time has gone")
    escalate: bool = True
    escalate_team_id: str | None = None


@router.get("/service-targets")
def get_service_targets(customer_id: str, user: UserDep) -> dict:
    """How reminders and escalations work for missed first-reply and resolution targets."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return inbox_jobs.target_settings(conn, customer_id)


@router.put("/service-targets")
def set_service_targets(customer_id: str, body: TargetsIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        if (
            body.escalate_team_id
            and not conn.execute(
                "SELECT 1 FROM commai_teams WHERE id = %s AND customer_id = %s", (body.escalate_team_id, customer_id)
            ).fetchone()
        ):
            raise HTTPException(404, "Team not found.")
        inbox.settings(conn, customer_id)
        conn.execute(
            """UPDATE commai_settings SET config = jsonb_set(config, '{service_targets}', %s), updated_at = now()
               WHERE customer_id = %s""",
            (Jsonb(body.model_dump()), customer_id),
        )
        audit.record(conn, user.actor, "commai.service_targets.update", "", customer_id, body.model_dump())
        return inbox_jobs.target_settings(conn, customer_id)
