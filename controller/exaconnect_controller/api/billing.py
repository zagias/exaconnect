"""Billing for Connect (ADR 0024).

Admins manage price lists, generate draft invoices, issue and void them, and
see carrier cost and margin. Customer users see their own month's running
charges and their own issued (and void) invoices, read only. Carrier users
see nothing here. Every write is audited (billing.service).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import db
from ..billing import service
from ..billing.core import BillingError, parse_period
from .deps import AdminDep, ViewerDep, check_customer

router = APIRouter(prefix="/billing", tags=["billing"])


def _err(e: BillingError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _period(period: str | None) -> dt.date:
    if not period:
        return dt.datetime.now(dt.UTC).date().replace(day=1)
    try:
        return parse_period(period)
    except BillingError as e:
        raise _err(e) from e


def _customer(user, customer_id: uuid.UUID | None) -> Any:
    """Customer users act for their own customer; admins name one."""
    if user.role == "customer":
        if customer_id is not None:
            check_customer(user, customer_id)
        return user.customer_id
    if customer_id is None:
        raise HTTPException(422, "Choose a customer.")
    return customer_id


# ---- price lists -----------------------------------------------------------------------


class CreditRow(BaseModel):
    below_pct: float = Field(gt=0, le=100)
    credit_pct: float = Field(gt=0, le=100)


class PriceListIn(BaseModel):
    customer_id: uuid.UUID | None = None  # None: ExaCarib's default list
    effective_from: dt.date
    label: str = Field(default="", max_length=120)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    site_monthly: str = Field(default="0", max_length=20)
    commit_per_mbps: str = Field(default="0", max_length=20)
    burst_per_mbps: str = Field(default="0", max_length=20)
    satellite_per_gb: str = Field(default="0", max_length=20)
    circuit_per_mbps_month: str | None = Field(default=None, max_length=20)
    tax_rate_pct: str = Field(default="0", max_length=20)
    sla_credits: list[CreditRow] = Field(default_factory=list, max_length=10)
    credit_cap_pct: str = Field(default="50", max_length=20)


@router.get("/price-lists")
def list_price_lists(user: AdminDep, customer_id: uuid.UUID | None = None) -> dict:
    """The default list's versions, and a customer's own versions when one is named."""
    with db.tx() as conn:
        out = {"default": service.price_lists(conn, None, default=True), "customer": []}
        if customer_id:
            out["customer"] = service.price_lists(conn, customer_id, default=False)
            today = dt.datetime.now(dt.UTC).date()
            out["in_effect"] = service.price_list_for(conn, customer_id, today.replace(day=1))
    return service.jsonable(out)


@router.post("/price-lists", status_code=201)
def create_price_list(body: PriceListIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        if (
            body.customer_id
            and not conn.execute("SELECT 1 FROM customers WHERE id = %s", (body.customer_id,)).fetchone()
        ):
            raise HTTPException(404, "Customer not found.")
        try:
            row = service.create_price_list(
                conn, body.customer_id, {**body.model_dump(exclude={"customer_id"})}, user.actor
            )
        except BillingError as e:
            raise _err(e) from e
    return service.jsonable(row)


# ---- charges ---------------------------------------------------------------------------


@router.get("/charges")
def charges(user: ViewerDep, customer_id: uuid.UUID | None = None, period: str | None = None) -> dict:
    """The month's charges and credits worked out now (default: this month, so far)."""
    cid = _customer(user, customer_id)
    start = _period(period)
    with db.tx() as conn:
        try:
            out = service.compute(conn, cid, start)
        except BillingError as e:
            raise _err(e) from e
    return service.jsonable(out)


# ---- invoices --------------------------------------------------------------------------


@router.get("/invoices")
def list_invoices(user: ViewerDep, customer_id: uuid.UUID | None = None, period: str | None = None) -> list[dict]:
    """Admins: every invoice, or one customer's. Customers: their own, without drafts."""
    if user.role == "customer":
        cid = _customer(user, customer_id)
    else:
        cid = customer_id
    start = _period(period) if period else None
    with db.tx() as conn:
        rows = service.list_invoices(conn, cid, include_drafts=user.role == "admin", period=start)
    return service.jsonable(rows)


def _one(user, invoice_id: str) -> dict:
    scope = None if user.role == "admin" else user.customer_id
    with db.tx() as conn:
        try:
            return service.get_invoice(conn, invoice_id, scope, include_drafts=user.role == "admin")
        except BillingError as e:
            raise _err(e) from e


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, user: ViewerDep) -> dict:
    return service.jsonable(_one(user, invoice_id))


@router.get("/invoices/{invoice_id}/csv")
def invoice_csv(invoice_id: str, user: ViewerDep) -> Response:
    inv = _one(user, invoice_id)
    name = inv["number"] or f"draft-{inv['period']}"
    return Response(
        service.invoice_csv(inv),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="exacarib-invoice-{name}.csv"'},
    )


class GenerateIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    customer_id: uuid.UUID | None = None  # None: every customer with sites


@router.post("/invoices/generate")
def generate(body: GenerateIn, user: AdminDep) -> dict:
    """Make or rebuild draft invoices for a month. Safe to run again while they are drafts."""
    start = _period(body.period)
    with db.tx() as conn:
        try:
            if body.customer_id:
                out = {"drafts": [service.generate_draft(conn, body.customer_id, start, user.actor)], "skipped": []}
            else:
                out = service.generate_all(conn, start, user.actor)
        except BillingError as e:
            raise _err(e) from e
    return service.jsonable(out)


@router.post("/invoices/{invoice_id}/issue")
def issue(invoice_id: str, user: AdminDep) -> dict:
    with db.tx() as conn:
        try:
            return service.jsonable(service.issue(conn, invoice_id, user.actor))
        except BillingError as e:
            raise _err(e) from e


class VoidIn(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.post("/invoices/{invoice_id}/void")
def void(invoice_id: str, body: VoidIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        try:
            return service.jsonable(service.void(conn, invoice_id, body.reason, user.actor))
        except BillingError as e:
            raise _err(e) from e


# ---- carrier cost and margin -----------------------------------------------------------


@router.get("/margin")
def margin(user: AdminDep, period: str | None = None) -> dict:
    """Read only: what each customer is billed, what ExaCarib owes carriers for
    that customer's links, and the margin."""
    start = _period(period)
    with db.tx() as conn:
        try:
            return service.jsonable(service.margin(conn, start))
        except BillingError as e:
            raise _err(e) from e
