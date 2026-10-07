"""The single bill, rate cards, money budgets and the AI supplier side (ADR 0039).

- Businesses read their usage, bills and budgets; business admins set budgets.
- ExaCarib admins set prices (rate cards), build and issue bills, credit
  them, and see the supplier side and the margin.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, ai_supplier, bill, usage
from .common import page

router = APIRouter(tags=["commai: bill"])
C = "/customers/{customer_id}"


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except bill.BillError as e:
        raise HTTPException(e.code, str(e)) from e
    except ai_supplier.SupplierError as e:
        raise HTTPException(e.code, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _exacarib(user) -> None:
    if user.role != "admin":
        raise HTTPException(403, "ExaCarib sets prices and issues bills.")


def _period(text: str | None) -> dt.date:
    if not text:
        return bill.month_start(dt.datetime.now(dt.UTC))
    try:
        y, m = (int(x) for x in text.split("-")[:2])
        return dt.date(y, m, 1)
    except ValueError as e:
        raise HTTPException(422, "Give the period as YYYY-MM.") from e


def _money(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation as e:
        raise HTTPException(422, "Amounts must be numbers.") from e


# ---- rate cards ---------------------------------------------------------------------------


@router.get(C + "/bill/rate-cards")
def rate_cards(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return bill.cards(conn, customer_id)


class CardIn(BaseModel):
    prices: dict[str, str | int] = Field(min_length=1, max_length=60)
    label: str = Field(default="", max_length=120)
    effective_from: dt.datetime | None = None


@router.post(C + "/bill/rate-cards", status_code=201)
def new_rate_card(customer_id: str, body: CardIn, user: UserDep) -> dict:
    """ExaCarib: a new card version. Usage keeps the price of the card it was rated on."""
    _exacarib(user)
    with db.tx() as conn, _errors():
        row = bill.new_card(
            conn, customer_id, body.prices, user.actor, label_=body.label, effective_from=body.effective_from
        )
        audit.record(conn, user.actor, "commai.bill.rate_card", str(row["version"]), customer_id)
    return row


# ---- usage and bills ---------------------------------------------------------------------


@router.get(C + "/bill/usage")
def bill_usage(customer_id: str, user: UserDep, period: str | None = None) -> dict:
    """Messaging, AI, workflow and voice charges for a month, priced on the rate cards."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return bill.usage_summary(conn, customer_id, _period(period))


@router.get(C + "/bills")
def list_bills(
    customer_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(50, ge=1, le=200)
) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = bill.bills(conn, customer_id, after=cursor, limit=limit)
    return page(rows, limit, "created_at")


@router.get(C + "/bills/{bill_id}")
def get_bill(customer_id: str, bill_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        return bill.get(conn, customer_id, bill_id)


@router.get(C + "/bills/{bill_id}/lines/{line_id}/charges")
def line_charges(customer_id: str, bill_id: str, line_id: int, user: UserDep) -> list[dict]:
    """Every charge behind a line: each call, message, AI reply or workflow run."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        return bill.charge_trace(conn, customer_id, line_id)


class DraftIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")


@router.post(C + "/bills/draft", status_code=201)
def draft_bill(customer_id: str, body: DraftIn, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, _errors():
        out = bill.draft(conn, customer_id, _period(body.period), user.actor)
        audit.record(conn, user.actor, "commai.bill.draft", str(out["id"]), customer_id)
    return out


@router.post(C + "/bills/{bill_id}/issue")
def issue_bill(customer_id: str, bill_id: str, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, _errors():
        out = bill.issue(conn, customer_id, bill_id, user.actor)
        audit.record(conn, user.actor, "commai.bill.issue", out["number"], customer_id)
    return out


class CreditLine(BaseModel):
    line_id: int
    amount: str | None = None


class CreditIn(BaseModel):
    lines: list[CreditLine] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=300)


@router.post(C + "/bills/{bill_id}/credit", status_code=201)
def credit_bill(customer_id: str, bill_id: str, body: CreditIn, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, _errors():
        out = bill.credit_note(
            conn, customer_id, bill_id, [x.model_dump() for x in body.lines], body.reason, user.actor
        )
        audit.record(conn, user.actor, "commai.bill.credit", out["number"], customer_id, {"reason": body.reason})
    return out


# ---- money budgets -----------------------------------------------------------------------


@router.get(C + "/budgets")
def list_budgets(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return usage.budgets(conn, customer_id)


class BudgetIn(BaseModel):
    key: str = Field(default="", max_length=60)
    monthly_alert: str | int | None = None
    monthly_hard: str | int | None = None


@router.put(C + "/budgets/{scope}")
def set_budget(customer_id: str, scope: Literal["total", "channel", "ai", "workflow"], body: BudgetIn, user: UserDep):
    """A money budget: an alert level, and a hard limit that stops the work."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        if access.seat(conn, user, customer_id) == "internal":
            raise HTTPException(403, "Your seat can't change budgets.")
        row = usage.set_budget(
            conn, customer_id, scope, body.key, _money(body.monthly_alert), _money(body.monthly_hard), user.actor
        )
        audit.record(
            conn,
            user.actor,
            "commai.budget.set",
            f"{scope}:{body.key}",
            customer_id,
            {"alert": row["monthly_alert"], "hard": row["monthly_hard"]},
        )
    return row


@router.delete(C + "/budgets/{scope}", status_code=204)
def delete_budget(customer_id: str, scope: str, user: UserDep, key: str = "") -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        if access.seat(conn, user, customer_id) == "internal":
            raise HTTPException(403, "Your seat can't change budgets.")
        if not usage.delete_budget(conn, customer_id, scope, key):
            raise HTTPException(404, "No such budget.")
        audit.record(conn, user.actor, "commai.budget.delete", f"{scope}:{key}", customer_id)


# ---- AI supplier side (ExaCarib) -----------------------------------------------------------


class SupplierImportIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: Literal["simulated", "deepinfra", ""] = ""


@router.post("/exacarib/ai-supplier/import", status_code=201)
def ai_supplier_import(body: SupplierImportIn, user: UserDep) -> dict:
    """Import a month of model and speech costs from the supplier's usage."""
    _exacarib(user)
    with db.tx() as conn, _errors():
        out = ai_supplier.import_costs(conn, _period(body.period), ai_supplier.source(body.source), user.actor)
        audit.record(conn, user.actor, "commai.ai_supplier.import", out["id"], None, {"rows": out["rows"]})
    return out


@router.get(C + "/ai-supplier/reconcile")
def ai_supplier_reconcile(customer_id: str, user: UserDep, period: str | None = None) -> dict:
    """What the business was billed for AI against its share of the supplier's cost, and the margin."""
    _exacarib(user)
    with db.tx() as conn, _errors():
        return ai_supplier.reconcile(conn, customer_id, _period(period))
