"""AI quality and governance API (ADR 0032): quality criteria and reviews,
flags, the knowledge-gap report with drafted articles, follow-up reminders,
what the AI read from attachments, evaluation suites, candidates and daily
action limits.

Reads need commai:read; changing criteria, suites, limits and promoting
candidates needs commai:admin and a seat other than internal. The global
suite and model candidates are ExaCarib's (admins only). Every write is audited.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import User, UserDep, require_admin
from .. import access, inbox
from ..ai import attachments, followups, gaps, governance, quality, runtime
from ..ai.model import ModelError
from .common import errors

router = APIRouter(tags=["commai: quality and governance"])
B = "/customers/{customer_id}"


@contextmanager
def _errors() -> Iterator[None]:
    try:
        with errors():
            yield
    except (quality.QualityError, gaps.GapError, governance.GovernanceError) as e:
        raise HTTPException(e.code, str(e)) from e
    except ModelError as e:
        raise HTTPException(502, str(e)) from e
    except runtime.UsageLimit as e:
        raise HTTPException(429, str(e)) from e


def _admin(conn, user, customer_id: str) -> None:
    access.require_business_admin(user)
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change AI settings.")


# ---- quality criteria and reviews --------------------------------------------------------


class CriterionIn(BaseModel):
    text: str = Field(min_length=3, max_length=300)


class CriterionPatch(BaseModel):
    text: str | None = Field(default=None, min_length=3, max_length=300)
    enabled: bool | None = None


@router.get(B + "/quality/criteria")
def list_criteria(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return quality.criteria(conn, customer_id)


@router.post(B + "/quality/criteria", status_code=201)
def add_criterion(customer_id: str, body: CriterionIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = quality.add_criterion(conn, customer_id, body.text, user.actor)
        audit.record(conn, user.actor, "commai.quality.criterion.create", str(row["id"]), customer_id)
        return row


@router.patch(B + "/quality/criteria/{criterion_id}")
def update_criterion(customer_id: str, criterion_id: str, body: CriterionPatch, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = quality.update_criterion(conn, customer_id, criterion_id, text=body.text, enabled=body.enabled)
        audit.record(conn, user.actor, "commai.quality.criterion.update", criterion_id, customer_id)
        return row


@router.delete(B + "/quality/criteria/{criterion_id}", status_code=204)
def delete_criterion(customer_id: str, criterion_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        quality.delete_criterion(conn, customer_id, criterion_id)
        audit.record(conn, user.actor, "commai.quality.criterion.delete", criterion_id, customer_id)


class ReviewIn(BaseModel):
    sample_size: int = Field(default=20, ge=1, le=quality.MAX_SAMPLE)
    days: int = Field(default=30, ge=1, le=365)


@router.post(B + "/quality/reviews", status_code=202)
def start_review(customer_id: str, body: ReviewIn, user: UserDep) -> dict:
    """Queue a review of a sample of AI and human conversations against the criteria."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = quality.start_review(conn, customer_id, sample_size=body.sample_size, days=body.days, actor=user.actor)
        audit.record(conn, user.actor, "commai.quality.review.start", str(row["id"]), customer_id)
        return row


@router.get(B + "/quality/reviews")
def list_reviews(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return quality.reviews(conn, customer_id)


@router.get(B + "/quality/reviews/{review_id}")
def get_review(customer_id: str, review_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM quality_reviews WHERE id = %s AND customer_id = %s", (review_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Review not found.")
        return {**row, "results": quality.results(conn, customer_id, review_id)}


@router.get(B + "/quality/flags")
def list_flags(customer_id: str, user: UserDep, status: str = "open") -> list[dict]:
    access.check(user, customer_id, "commai:read")
    if status not in ("open", "reviewed", "all"):
        raise HTTPException(422, "Status is open, reviewed or all.")
    with db.tx() as conn:
        return quality.flags(conn, customer_id, status)


class FlagIn(BaseModel):
    note: str = Field(default="", max_length=1000)


@router.post(B + "/quality/flags/{result_id}/review")
def review_flag(customer_id: str, result_id: str, body: FlagIn, user: UserDep) -> dict:
    """A person has looked at a flagged conversation."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, _errors():
        row = quality.review_flag(conn, customer_id, result_id, user.actor, body.note)
        audit.record(conn, user.actor, "commai.quality.flag.review", result_id, customer_id)
        return row


# ---- knowledge gaps -----------------------------------------------------------------------


@router.get(B + "/ai/gaps/report")
def gap_report(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return gaps.report(conn, customer_id)


class DraftIn(BaseModel):
    gap_ids: list[str] = Field(min_length=1, max_length=50)


@router.post(B + "/ai/gaps/draft", status_code=201)
def draft_article(customer_id: str, body: DraftIn, user: UserDep) -> dict:
    """Draft an article answering these questions. It is not used until approved."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = gaps.draft_article(conn, customer_id, body.gap_ids, user.actor)
        audit.record(conn, user.actor, "commai.ai.gaps.draft", str(row["id"]), customer_id, {"gaps": body.gap_ids})
        return row


@router.post(B + "/ai/gaps/drafts/{source_id}/approve")
def approve_draft(customer_id: str, source_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = gaps.approve_draft(conn, customer_id, source_id, user.actor)
        audit.record(conn, user.actor, "commai.ai.gaps.approve", source_id, customer_id)
        return row


# ---- follow-up reminders ------------------------------------------------------------------


@router.get(B + "/followups")
def list_followups(customer_id: str, user: UserDep, status: str = "open", mine: bool = False) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    if status not in ("open", "done", "dismissed", "all"):
        raise HTTPException(422, "Status is open, done, dismissed or all.")
    with db.tx() as conn:
        return followups.reminders(conn, customer_id, status=status, owner=user.id if mine else None)


class FollowupIn(BaseModel):
    status: str = Field(pattern="^(open|done|dismissed)$")


@router.post(B + "/followups/{reminder_id}")
def close_followup(customer_id: str, reminder_id: str, body: FollowupIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        row = followups.close(conn, customer_id, reminder_id, body.status, user.actor)
        if row is None:
            raise HTTPException(404, "Reminder not found.")
        audit.record(conn, user.actor, "commai.followup." + body.status, reminder_id, customer_id)
        return row


# ---- attachments --------------------------------------------------------------------------


@router.get(B + "/conversations/{conversation_id}/attachments")
def conversation_attachments(customer_id: str, conversation_id: str, user: UserDep) -> list[dict]:
    """What the AI read from each file the customer sent."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        inbox.get(conn, customer_id, conversation_id)
        return attachments.readings(conn, customer_id, conversation_id)


@router.post(B + "/files/{file_id}/read")
def read_again(customer_id: str, file_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        row = attachments.read_file(conn, customer_id, file_id, again=True)
        if row is None:
            raise HTTPException(404, "File not found.")
        audit.record(conn, user.actor, "commai.attachment.read", file_id, customer_id)
        return row


# ---- governance: business suite, candidates, limits --------------------------------------


class CaseIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    expected: str = Field(default="", max_length=300)
    forbidden: str = Field(default="", max_length=300)


@router.get(B + "/ai/governance")
def governance_overview(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        live = conn.execute("SELECT model, promoted_by, promoted_at FROM ai_live_model WHERE id = 1").fetchone()
        return {
            "cases": governance.cases(conn, customer_id),
            "global_cases": len([c for c in governance.cases(conn, None) if c["enabled"]]),
            "candidates": governance.candidates(conn, customer_id),
            "limits": governance.action_limits(conn, customer_id),
            "model": {**runtime.model_status(), "live_model": live},
        }


@router.post(B + "/ai/governance/cases", status_code=201)
def add_case(customer_id: str, body: CaseIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = governance.add_case(conn, customer_id, body.question, body.expected, body.forbidden, user.actor)
        audit.record(conn, user.actor, "commai.ai.eval_case.create", str(row["id"]), customer_id)
        return row


@router.delete(B + "/ai/governance/cases/{case_id}", status_code=204)
def delete_case(customer_id: str, case_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        governance.delete_case(conn, customer_id, case_id)
        audit.record(conn, user.actor, "commai.ai.eval_case.delete", case_id, customer_id)


@router.get(B + "/ai/governance/candidates/{candidate_id}")
def get_candidate(customer_id: str, candidate_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        cand = governance.get_candidate(conn, candidate_id, customer_id)
        return {**cand, "runs": governance.runs(conn, candidate_id)}


@router.post(B + "/ai/governance/candidates/{candidate_id}/{verb}")
def act_on_candidate(customer_id: str, candidate_id: str, verb: str, user: UserDep) -> dict:
    """promote (only after its latest run passed every case), rerun or withdraw."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        if verb == "promote":
            row = governance.promote(conn, candidate_id, customer_id, user.actor)
        elif verb == "withdraw":
            row = governance.withdraw(conn, candidate_id, customer_id, user.actor)
        elif verb == "rerun":
            governance.rerun(conn, candidate_id, customer_id)
            row = governance.get_candidate(conn, candidate_id, customer_id)
        else:
            raise HTTPException(404, "Promote, rerun or withdraw.")
        audit.record(conn, user.actor, f"commai.ai.candidate.{verb}", candidate_id, customer_id)
        return row


class LimitIn(BaseModel):
    daily_limit: int | None = Field(default=None, ge=0, le=100_000)


@router.put(B + "/ai/governance/limits/{role}")
def set_limit(customer_id: str, role: str, body: LimitIn, user: UserDep) -> list[dict]:
    """How many actions this role may propose per day (UTC). null removes the limit."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        governance.set_action_limit(conn, customer_id, role, body.daily_limit)
        audit.record(conn, user.actor, "commai.ai.action_limit.set", role, customer_id, {"limit": body.daily_limit})
        return governance.action_limits(conn, customer_id)


# ---- governance: ExaCarib's global suite and model candidates ------------------------------


@router.get("/ai-governance")
def global_governance(user: User = Depends(require_admin)) -> dict:
    with db.tx() as conn:
        return {
            "cases": governance.cases(conn, None),
            "candidates": governance.candidates(conn, None),
            "live_model": conn.execute("SELECT * FROM ai_live_model WHERE id = 1").fetchone(),
        }


@router.post("/ai-governance/cases", status_code=201)
def add_global_case(body: CaseIn, user: User = Depends(require_admin)) -> dict:
    with db.tx() as conn, _errors():
        row = governance.add_case(conn, None, body.question, body.expected, body.forbidden, user.actor)
        audit.record(conn, user.actor, "commai.ai.eval_case.create", str(row["id"]), None, {"suite": "global"})
        return row


@router.delete("/ai-governance/cases/{case_id}", status_code=204)
def delete_global_case(case_id: str, user: User = Depends(require_admin)) -> None:
    with db.tx() as conn, _errors():
        governance.delete_case(conn, None, case_id)
        audit.record(conn, user.actor, "commai.ai.eval_case.delete", case_id, None, {"suite": "global"})


@router.get("/ai-governance/candidates/{candidate_id}")
def get_model_candidate(candidate_id: str, user: User = Depends(require_admin)) -> dict:
    with db.tx() as conn, _errors():
        cand = governance.get_candidate(conn, candidate_id, None)
        return {**cand, "runs": governance.runs(conn, candidate_id)}


@router.post("/ai-governance/candidates/{candidate_id}/{verb}")
def act_on_model_candidate(candidate_id: str, verb: str, user: User = Depends(require_admin)) -> dict:
    with db.tx() as conn, _errors():
        if verb == "promote":
            row = governance.promote(conn, candidate_id, None, user.actor)
        elif verb == "withdraw":
            row = governance.withdraw(conn, candidate_id, None, user.actor)
        elif verb == "rerun":
            governance.rerun(conn, candidate_id, None)
            row = governance.get_candidate(conn, candidate_id, None)
        else:
            raise HTTPException(404, "Promote, rerun or withdraw.")
        audit.record(conn, user.actor, f"commai.ai.model_candidate.{verb}", candidate_id, None)
        return row
