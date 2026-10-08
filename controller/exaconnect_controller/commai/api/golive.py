"""Go-live registry API (ADR 0028). ExaCarib admins change it; anyone signed in reads it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import User, UserDep, require_admin
from .. import golive

router = APIRouter(prefix="/golive", tags=["commai: go-live"])


class CheckIn(BaseModel):
    met: bool
    evidence: str = Field(default="", max_length=2000)


class StatusIn(BaseModel):
    status: str = Field(pattern="^(off|pilot|on)$")
    pilots: list[str] | None = Field(default=None, max_length=200)


def _errors(e: golive.GoLiveError) -> HTTPException:
    return HTTPException(e.code, str(e))


@router.get("")
def list_capabilities(user: UserDep, kind: str | None = None) -> list[dict]:
    if user.role == "carrier":  # carrier accounts never reach Jibsy (access.py)
        raise HTTPException(403, "Not available for this account.")
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT c.kind, c.key, c.name, c.status, c.details, c.updated_by, c.updated_at,
                      count(k.*) FILTER (WHERE k.met) AS met, count(k.*) AS criteria
               FROM commai_capabilities c LEFT JOIN commai_capability_criteria k USING (kind, key)
               WHERE (%(kind)s::text IS NULL OR c.kind = %(kind)s)
               GROUP BY c.kind, c.key ORDER BY c.kind, c.key""",
            {"kind": kind},
        ).fetchall()
        if user.role != "admin":
            # Customers see what is available to them, not ExaCarib's internal checks.
            return [
                {"kind": r["kind"], "key": r["key"], "name": r["name"], "details": r["details"],
                 "available": golive.enabled(conn, r["kind"], r["key"], user.customer_id)}
                for r in rows
            ]  # fmt: skip
        return rows


@router.get("/{kind}/{key}")
def get_capability(kind: str, key: str, user: User = Depends(require_admin)) -> dict:
    with db.tx() as conn:
        row = golive.get(conn, kind, key)
    if not row:
        raise HTTPException(404, "No such capability.")
    return row


@router.put("/{kind}/{key}/criteria/{criterion}")
def check_criterion(kind: str, key: str, criterion: str, body: CheckIn, user: User = Depends(require_admin)) -> dict:
    try:
        with db.tx() as conn:
            golive.check(conn, kind, key, criterion, body.met, body.evidence, user.actor)
            audit.record(
                conn, user.actor, "commai.golive.check", f"{kind}/{key}/{criterion}", None,
                {"met": body.met, "evidence": body.evidence[:200]},
            )  # fmt: skip
            return golive.get(conn, kind, key)
    except golive.GoLiveError as e:
        raise _errors(e) from e


@router.put("/{kind}/{key}/status")
def set_status(kind: str, key: str, body: StatusIn, user: User = Depends(require_admin)) -> dict:
    try:
        with db.tx() as conn:
            golive.set_status(conn, kind, key, body.status, user.actor, body.pilots)
            audit.record(
                conn, user.actor, "commai.golive.status", f"{kind}/{key}", None,
                {"status": body.status, "pilots": body.pilots},
            )  # fmt: skip
            return golive.get(conn, kind, key)
    except golive.GoLiveError as e:
        raise _errors(e) from e
