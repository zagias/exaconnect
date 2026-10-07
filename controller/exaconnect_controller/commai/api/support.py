"""Support cases: ExaCarib's queue, and replies on both sides (ADR 0039).

ExaCarib admins: /exacarib/support/cases (every business). A business's
admins: /customers/{id}/support-cases/{case}/thread and .../replies; they
never see ExaCarib's internal notes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access
from ..automation import support
from . import paging

router = APIRouter(tags=["commai: support"])
C = "/customers/{customer_id}/support-cases/{case_id}"
Q = "/exacarib/support/cases"


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except support.SupportError as e:
        raise HTTPException(e.code, str(e)) from e


def _exacarib(user) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Only ExaCarib staff work the support queue.")


@router.get(Q)
def case_queue(
    user: UserDep,
    status: str | None = Query(None, description="open, in_progress, waiting_on_customer, closed, or active"),
    assignee: str | None = None,
    customer_id: str | None = None,
    priority: str | None = None,
    q: str | None = Query(None, max_length=100),
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=200),
) -> Any:
    _exacarib(user)
    with db.tx() as conn:
        rows = support.queue(
            conn,
            status=status,
            assignee=assignee,
            customer_id=customer_id,
            priority=priority,
            q=q,
            after=paging.where(cursor, "c.created_at", "c.id"),
            limit=limit + 1,
        )
    return paging.result(rows, cursor, limit)


@router.get(Q + "/{case_id}")
def queue_case(case_id: str, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, _errors():
        return support.get(conn, case_id, exacarib=True)


class CasePatch(BaseModel):
    status: str | None = None
    priority: str | None = None
    assignee: str | None = Field(default=None, max_length=200)


@router.patch(Q + "/{case_id}")
def update_case(case_id: str, body: CasePatch, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, _errors():
        row = support.update(conn, case_id, actor=user.actor, **body.model_dump())
        audit.record(
            conn, user.actor, "commai.support_case.update", row["reference"], row["customer_id"], body.model_dump()
        )
    return row


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    internal: bool = False
    status: str | None = None


@router.post(Q + "/{case_id}/replies", status_code=201)
def exacarib_reply(case_id: str, body: ReplyIn, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, _errors():
        r = support.reply(
            conn,
            case_id,
            body.body,
            actor=user.actor,
            from_exacarib=True,
            internal=body.internal,
            status=body.status,
        )
        audit.record(
            conn, user.actor, "commai.support_case.reply", case_id, r["customer_id"], {"internal": body.internal}
        )
    return r


@router.get(C + "/thread")
def case_thread(customer_id: str, case_id: str, user: UserDep) -> dict:
    """The case with ExaCarib's replies (internal notes left out)."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        return support.get(conn, case_id, customer_id=customer_id, exacarib=user.role == "admin")


class BusinessReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post(C + "/replies", status_code=201)
def business_reply(customer_id: str, case_id: str, body: BusinessReplyIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        r = support.reply(
            conn,
            case_id,
            body.body,
            actor=user.actor,
            from_exacarib=user.role == "admin",
            customer_id=customer_id,
        )
        audit.record(conn, user.actor, "commai.support_case.reply", case_id, customer_id)
    return r
