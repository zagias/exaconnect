"""Catalogue, rating, quotes (CPQ), sales orders, contracts and usage (ADR 0055).

ExaCarib admins run the catalogue, build and discount quotes, approve discounts
above an item's limit, and work orders. An organisation's people read the
catalogue, their quotes, orders and contract; its owners and admins can build
a quote at list price, accept or decline quotes. Carrier accounts get nothing
here. Every write is audited (commercial.*).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import db
from ..billing.core import BillingError
from ..billing.service import jsonable
from ..commercial import catalogue, contract, orders, quotes, rating
from .deps import AdminDep, ViewerDep, can_manage_org, check_customer

router = APIRouter(tags=["commercial"])

Product = Literal["connect", "commai"]


def _call(fn, *args, **kwargs):
    with db.tx() as conn:
        try:
            return jsonable(fn(conn, *args, **kwargs))
        except BillingError as e:
            raise HTTPException(e.status, str(e)) from e


def _staff(user) -> bool:
    return user.role == "admin"


def _scope(user, customer_id: Any | None) -> Any | None:
    """Customer users act for their own organisation; admins see all or name one."""
    if user.role == "customer":
        if user.customer_id is None:
            raise HTTPException(403, "Not available for this account.")
        if customer_id is not None:
            check_customer(user, customer_id)
        return user.customer_id
    return customer_id


def _manager(user, customer_id: Any) -> None:
    check_customer(user, customer_id)
    if not can_manage_org(user):
        raise HTTPException(403, "Only this organisation's owners and admins can do this.")


# ---- catalogue ---------------------------------------------------------------------------


class PriceIn(BaseModel):
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    effective_from: str | None = None
    model: Literal["per_unit", "flat", "tiered", "volume"] = "per_unit"
    unit_price: str | float = "0"
    tiers: list[dict] = Field(default=[], max_length=10)
    included_qty: str | float = "0"
    minimum: str | float = "0"
    term_discounts: list[dict] = Field(default=[], max_length=10)
    example: bool | None = None


class ItemIn(BaseModel):
    code: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    product: Product
    charge_type: Literal["recurring", "one_off", "usage"]
    family: Literal["hardware", "service", "seat", "support", "usage"] | None = None
    fulfilment: Literal["immediate", "manual"] | None = None
    unit: str = Field(default="each", min_length=1, max_length=30)
    meter: str | None = Field(default=None, max_length=64)
    max_discount_pct: float = Field(default=10, ge=0, le=100)
    example: bool = False
    position: int = Field(default=100, ge=0, le=10_000)
    price: PriceIn | None = None


class ItemPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    unit: str | None = Field(default=None, min_length=1, max_length=30)
    max_discount_pct: float | None = Field(default=None, ge=0, le=100)
    active: bool | None = None
    example: bool | None = None
    position: int | None = Field(default=None, ge=0, le=10_000)


@router.get("/catalogue/items")
def list_items(
    user: ViewerDep, product: Product | None = None, include_inactive: bool = False, currency: str = "USD"
) -> dict:
    return {"items": _call(catalogue.list_items, include_inactive and _staff(user), product, currency.upper()[:3])}


@router.get("/catalogue/items/{ref}")
def get_item(ref: str, user: ViewerDep) -> dict:
    return _call(catalogue.item_detail, ref)


@router.post("/catalogue/items", status_code=201)
def create_item(body: ItemIn, user: AdminDep) -> dict:
    values = body.model_dump(exclude_none=True)
    if body.price is not None:
        values["price"] = body.price.model_dump(exclude_none=True)
    return _call(catalogue.create_item, values, user.actor)


@router.patch("/catalogue/items/{ref}")
def update_item(ref: str, body: ItemPatch, user: AdminDep) -> dict:
    return _call(catalogue.update_item, ref, body.model_dump(exclude_none=True), user.actor)


@router.post("/catalogue/items/{ref}/prices", status_code=201)
def add_price(ref: str, body: PriceIn, user: AdminDep) -> dict:
    return _call(catalogue.add_price, ref, body.model_dump(exclude_none=True), user.actor)


# ---- rating ------------------------------------------------------------------------------


class RateIn(BaseModel):
    price: dict
    quantity: str | float = "1"
    term_months: int = Field(default=1, ge=1, le=120)
    discount_pct: float = Field(default=0, ge=0, le=100)


@router.post("/rating/preview")
def rate_preview(body: RateIn, user: ViewerDep) -> dict:
    """What the rating engine charges for a quantity on a price, with its working."""

    def run(_conn):
        price = rating.validate_price(body.price)
        rated = rating.rate(price, body.quantity)
        term = rating.term_discount_pct(price, body.term_months)
        return {
            "price": price,
            "price_text": rating.describe(price),
            **rated,
            "term_discount_pct": term,
            "discount_pct": body.discount_pct,
            "total_discount_pct": rating.combined_pct(term, body.discount_pct),
            "charge": rating.discounted(rated["amount"], term, body.discount_pct),
        }

    return _call(run)


# ---- quotes ------------------------------------------------------------------------------


class LineIn(BaseModel):
    item: str = Field(min_length=1, max_length=64)
    quantity: str | float = "1"
    discount_pct: float = Field(default=0, ge=0, le=100)
    config: dict = {}
    description: str = Field(default="", max_length=200)


class QuoteIn(BaseModel):
    customer_id: uuid.UUID | None = None
    title: str = Field(default="", max_length=120)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    term_months: int = Field(default=12, ge=1, le=120)
    valid_until: str | None = None
    notes: str = Field(default="", max_length=4000)
    lines: list[LineIn] = Field(default=[], max_length=quotes.MAX_LINES)


class QuotePatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    term_months: int | None = Field(default=None, ge=1, le=120)
    valid_until: str | None = None
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[LineIn] | None = Field(default=None, max_length=quotes.MAX_LINES)


def _quote_customer(user, customer_id: uuid.UUID | None) -> Any:
    if _staff(user):
        if customer_id is None:
            raise HTTPException(422, "Choose the organisation the quote is for.")
        return customer_id
    cid = _scope(user, customer_id)
    _manager(user, cid)
    return cid


def _values(body: BaseModel) -> dict:
    return body.model_dump(exclude_none=True, exclude={"customer_id"})


@router.post("/quotes/preview")
def preview_quote(body: QuoteIn, user: ViewerDep) -> dict:
    cid = body.customer_id if _staff(user) else _scope(user, body.customer_id)
    if cid is None:
        raise HTTPException(422, "Choose the organisation the quote is for.")
    return _call(quotes.preview, cid, _values(body), _staff(user))


@router.get("/quotes")
def list_quotes(user: ViewerDep, customer_id: uuid.UUID | None = None, status: str | None = None) -> dict:
    return {"quotes": _call(quotes.list_quotes, _scope(user, customer_id), _staff(user), status)}


@router.post("/quotes", status_code=201)
def create_quote(body: QuoteIn, user: ViewerDep) -> dict:
    cid = _quote_customer(user, body.customer_id)
    return _call(quotes.create, cid, _values(body), user.actor, _staff(user))


def _visible_quote(conn, user, quote_id: str) -> dict:
    q = quotes.get(conn, quote_id, None if _staff(user) else _scope(user, None))
    if not quotes.visible(conn, q, _staff(user)):
        raise BillingError("Quote not found.", 404)
    return q


@router.get("/quotes/{quote_id}")
def get_quote(quote_id: str, user: ViewerDep) -> dict:
    return _call(lambda conn: _visible_quote(conn, user, quote_id))


@router.get("/quotes/{quote_id}/document", response_class=HTMLResponse)
def quote_document(quote_id: str, user: ViewerDep) -> HTMLResponse:
    with db.tx() as conn:
        try:
            q = _visible_quote(conn, user, quote_id)
        except BillingError as e:
            raise HTTPException(e.status, str(e)) from e
        return HTMLResponse(
            quotes.document_html(q),
            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
        )


def _own(user) -> Any | None:
    return None if _staff(user) else _scope(user, None)


def _manage_quote(user) -> Any | None:
    cid = _own(user)
    if cid is not None:
        _manager(user, cid)
    return cid


@router.put("/quotes/{quote_id}")
def update_quote(quote_id: str, body: QuotePatch, user: ViewerDep) -> dict:
    cid = _manage_quote(user)
    return _call(quotes.update, quote_id, _values(body), user.actor, _staff(user), cid)


@router.post("/quotes/{quote_id}/copy", status_code=201)
def copy_quote(quote_id: str, user: ViewerDep) -> dict:
    cid = _manage_quote(user)
    return _call(quotes.copy, quote_id, user.actor, _staff(user), cid)


@router.post("/quotes/{quote_id}/submit")
def submit_quote(quote_id: str, user: AdminDep) -> dict:
    return _call(quotes.submit, quote_id, user.actor)


class ApproveIn(BaseModel):
    approve: bool = True
    note: str = Field(min_length=1, max_length=500)


@router.post("/quotes/{quote_id}/approve")
def approve_quote(quote_id: str, body: ApproveIn, user: AdminDep) -> dict:
    return _call(quotes.approve, quote_id, body.approve, body.note, user.actor)


@router.post("/quotes/{quote_id}/send")
def send_quote(quote_id: str, user: ViewerDep) -> dict:
    cid = _manage_quote(user)
    return _call(quotes.send, quote_id, user.actor, _staff(user), cid)


class AcceptIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)


@router.post("/quotes/{quote_id}/accept")
def accept_quote(quote_id: str, body: AcceptIn, user: ViewerDep) -> dict:
    """The organisation's owner or admin accepts (or ExaCarib, on their written word)."""
    cid = _manage_quote(user)
    return _call(quotes.accept, quote_id, body.name, user.actor, cid)


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/quotes/{quote_id}/decline")
def decline_quote(quote_id: str, body: ReasonIn, user: ViewerDep) -> dict:
    cid = _manage_quote(user)
    return _call(quotes.decline, quote_id, body.reason, user.actor, cid)


@router.post("/quotes/{quote_id}/cancel")
def cancel_quote(quote_id: str, user: ViewerDep) -> dict:
    cid = _manage_quote(user)
    return _call(quotes.cancel, quote_id, user.actor, _staff(user), cid)


# ---- sales orders ------------------------------------------------------------------------


@router.get("/sales-orders")
def list_orders(user: ViewerDep, customer_id: uuid.UUID | None = None, status: str | None = None) -> dict:
    return {"orders": _call(orders.list_orders, _scope(user, customer_id), status)}


@router.get("/sales-orders/{order_id}")
def get_order(order_id: str, user: ViewerDep) -> dict:
    return _call(orders.get, order_id, _own(user))


@router.post("/sales-orders/{order_id}/run")
def run_order(order_id: str, user: AdminDep) -> dict:
    return _call(orders.run, order_id, user.actor)


class CancelIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@router.post("/sales-orders/{order_id}/cancel")
def cancel_order(order_id: str, body: CancelIn, user: AdminDep) -> dict:
    return _call(orders.cancel, order_id, body.reason, user.actor)


class DoneIn(BaseModel):
    note: str = Field(default="", max_length=300)


@router.post("/sales-orders/items/{item_id}/done")
def item_done(item_id: str, body: DoneIn, user: AdminDep) -> dict:
    return _call(orders.mark_done, item_id, body.note, user.actor)


class RetryIn(BaseModel):
    config: dict | None = None


@router.post("/sales-orders/items/{item_id}/retry")
def item_retry(item_id: str, body: RetryIn, user: AdminDep) -> dict:
    return _call(orders.retry, item_id, user.actor, body.config)


@router.post("/sales-orders/items/{item_id}/cancel")
def item_cancel(item_id: str, body: CancelIn, user: AdminDep) -> dict:
    return _call(orders.cancel_item, item_id, body.reason, user.actor)


# ---- contract and usage ------------------------------------------------------------------


@router.get("/customers/{customer_id}/contract")
def get_contract(customer_id: uuid.UUID, user: ViewerDep) -> dict:
    check_customer(user, customer_id)
    return _call(contract.summary, customer_id)


class EndIn(BaseModel):
    ends_on: str | None = None


@router.post("/customers/{customer_id}/contract/{item_id}/end")
def end_contract_item(customer_id: uuid.UUID, item_id: str, body: EndIn, user: AdminDep) -> dict:
    return _call(contract.end_item, customer_id, item_id, body.ends_on, user.actor)


class UsageIn(BaseModel):
    customer_id: uuid.UUID
    events: list[dict] = Field(min_length=1, max_length=contract.MAX_EVENTS)


@router.post("/usage-events")
def post_usage(body: UsageIn, user: AdminDep) -> dict:
    """Usage for catalogue usage items (TMF635-style): meter, quantity, at, ref."""
    return _call(contract.record_usage, body.customer_id, body.events, user.actor, "api")


@router.get("/usage-events")
def get_usage(
    user: ViewerDep, customer_id: uuid.UUID | None = None, meter: str | None = None, limit: int = 200
) -> dict:
    cid = _scope(user, customer_id)
    if cid is None:
        raise HTTPException(422, "Choose a customer.")
    return {"events": _call(contract.usage_events, cid, meter, limit)}
