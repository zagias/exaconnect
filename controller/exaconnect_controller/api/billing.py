"""Billing (ADR 0022): plans per product, price lists, charges, invoices,
payments, accounting export, payables and margin.

Admins manage plans, subscriptions, price lists and invoices, and see what
ExaCarib owes carriers and partners and its margin. Customer users read
their own organisation's plans, running charges and issued (or void)
invoices, and may pay an issued invoice when online payment is switched on.
Carrier users get nothing here. Every write is audited (billing.service,
billing.plans, billing.payments). Gateway webhooks carry no session: they are
checked by signature instead.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .. import db
from ..billing import accounting, payments, plans, service
from ..billing.core import BillingError, parse_period
from .deps import AdminDep, ViewerDep, check_customer

router = APIRouter(prefix="/billing", tags=["billing"])
# Plans held by one organisation, beside the other per-customer endpoints.
customers_router = APIRouter(tags=["billing"])

Product = Literal["connect", "commai"]


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
    """Customer users act for their own organisation; admins name one."""
    if user.role == "customer":
        if customer_id is not None:
            check_customer(user, customer_id)
        return user.customer_id
    if customer_id is None:
        raise HTTPException(422, "Choose a customer.")
    return customer_id


def _call(fn, *args, **kwargs):
    """Run a billing call in one transaction, turning BillingError into HTTP."""
    with db.tx() as conn:
        try:
            return service.jsonable(fn(conn, *args, **kwargs))
        except BillingError as e:
            raise _err(e) from e


# ---- plans and subscriptions -----------------------------------------------------------


class PlanIn(BaseModel):
    product: Product
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class PlanPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    active: bool | None = None


@router.get("/plans")
def list_plans(user: AdminDep) -> list[dict]:
    """Every plan, with the price list each one has in effect today."""
    today = dt.datetime.now(dt.UTC).date()
    with db.tx() as conn:
        rows = plans.list_plans(conn)
        for r in rows:
            try:
                r["price_list"] = service.price_list_for(conn, r["id"], None, today)
            except BillingError:
                r["price_list"] = None
    return service.jsonable(rows)


@router.post("/plans", status_code=201)
def create_plan(body: PlanIn, user: AdminDep) -> dict:
    return _call(plans.create_plan, body.model_dump(), user.actor)


@router.patch("/plans/{plan_id}")
def update_plan(plan_id: str, body: PlanPatch, user: AdminDep) -> dict:
    return _call(plans.update_plan, plan_id, body.model_dump(), user.actor)


@customers_router.get("/customers/{customer_id}/plans")
def customer_plans(customer_id: uuid.UUID, user: ViewerDep) -> dict:
    """The products an organisation holds and every subscription it has had."""
    check_customer(user, customer_id)
    return _call(plans.summary, customer_id)


class SubscribeIn(BaseModel):
    plan_id: uuid.UUID
    starts_on: dt.date | None = None  # default: today


class ChangeIn(BaseModel):
    plan_id: uuid.UUID
    on: dt.date | None = None  # default: today


class EndIn(BaseModel):
    on: dt.date | None = None  # default: today (exclusive)


@customers_router.post("/customers/{customer_id}/plans", status_code=201)
def subscribe(customer_id: uuid.UUID, body: SubscribeIn, user: AdminDep) -> dict:
    return _call(plans.subscribe, customer_id, body.plan_id, body.starts_on, user.actor)


@customers_router.post("/customers/{customer_id}/plans/{subscription_id}/change")
def change_plan(customer_id: uuid.UUID, subscription_id: str, body: ChangeIn, user: AdminDep) -> dict:
    return _call(plans.change, customer_id, subscription_id, body.plan_id, body.on, user.actor)


@customers_router.post("/customers/{customer_id}/plans/{subscription_id}/end")
def end_plan(customer_id: uuid.UUID, subscription_id: str, body: EndIn, user: AdminDep) -> dict:
    return _call(plans.end, customer_id, subscription_id, body.on, user.actor)


# ---- price lists -----------------------------------------------------------------------


class CreditRow(BaseModel):
    below_pct: float = Field(gt=0, le=100)
    credit_pct: float = Field(gt=0, le=100)


Money = Field(default="0", max_length=20)


class PriceListIn(BaseModel):
    plan_id: uuid.UUID
    customer_id: uuid.UUID | None = None  # None: the plan's own list
    effective_from: dt.date
    label: str = Field(default="", max_length=120)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    tax_rate_pct: str = Money
    monthly_fee: str = Money
    site_monthly: str = Money
    commit_per_mbps: str = Money
    burst_per_mbps: str = Money
    satellite_per_gb: str = Money
    circuit_per_mbps_month: str | None = Field(default=None, max_length=20)
    meter_prices: dict[str, str] = Field(default_factory=dict)
    sla_credits: list[CreditRow] = Field(default_factory=list, max_length=10)
    credit_cap_pct: str = Field(default="50", max_length=20)


@router.get("/price-lists")
def list_price_lists(user: AdminDep, plan_id: uuid.UUID | None = None, customer_id: uuid.UUID | None = None) -> dict:
    """Plans' own lists (every version), and one organisation's own lists when named,
    with the list each of its plans has in effect this month."""
    with db.tx() as conn:
        out: dict[str, Any] = {"plans": service.price_lists(conn, plan_id, None), "customer": []}
        if customer_id:
            out["customer"] = service.price_lists(conn, plan_id, customer_id)
            month = dt.datetime.now(dt.UTC).date().replace(day=1)
            in_effect = []
            for s in plans.overlapping(conn, customer_id, month, (month + dt.timedelta(days=32)).replace(day=1)):
                try:
                    pl = service.price_list_for(conn, s["plan_id"], customer_id, max(month, s["starts_on"]))
                except BillingError:
                    pl = None
                in_effect.append({"plan": s["plan"], "product": s["product"], "price_list": pl})
            out["in_effect"] = in_effect
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
                conn, body.plan_id, body.customer_id, body.model_dump(exclude={"plan_id", "customer_id"}), user.actor
            )
        except BillingError as e:
            raise _err(e) from e
    return service.jsonable(row)


# ---- charges ---------------------------------------------------------------------------


@router.get("/charges")
def charges(user: ViewerDep, customer_id: uuid.UUID | None = None, period: str | None = None) -> dict:
    """The month's charges and credits per plan, worked out now (default: this month, so far)."""
    cid = _customer(user, customer_id)
    return _call(service.compute, cid, _period(period))


# ---- invoices --------------------------------------------------------------------------


@router.get("/invoices")
def list_invoices(
    user: ViewerDep, customer_id: uuid.UUID | None = None, period: str | None = None, product: Product | None = None
) -> list[dict]:
    """Admins: every invoice, or one organisation's. Customers: their own, without drafts."""
    cid = _customer(user, customer_id) if user.role == "customer" else customer_id
    start = _period(period) if period else None
    with db.tx() as conn:
        rows = service.list_invoices(conn, cid, include_drafts=user.role == "admin", period=start, product=product)
    return service.jsonable(rows)


def _one(user, invoice_id: str) -> dict:
    scope = None if user.role == "admin" else user.customer_id
    with db.tx() as conn:
        try:
            inv = service.get_invoice(conn, invoice_id, scope, include_drafts=user.role == "admin")
            inv["payments"] = payments.payments_for(conn, inv["id"])
            return inv
        except BillingError as e:
            raise _err(e) from e


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, user: ViewerDep) -> dict:
    return service.jsonable(_one(user, invoice_id))


def _filename(inv: dict) -> str:
    return inv["number"] or f"draft-{inv['period']}-{inv['product']}"


@router.get("/invoices/{invoice_id}/csv")
def invoice_csv(invoice_id: str, user: ViewerDep) -> Response:
    inv = _one(user, invoice_id)
    return Response(
        service.invoice_csv(inv),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="exacarib-invoice-{_filename(inv)}.csv"'},
    )


@router.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
def invoice_print(invoice_id: str, user: ViewerDep) -> HTMLResponse:
    """A printable page (the browser's print or save as PDF)."""
    return HTMLResponse(
        service.invoice_html(_one(user, invoice_id)),
        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
    )


class GenerateIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    customer_id: uuid.UUID | None = None  # None: every organisation with a plan that month
    product: Product | None = None


@router.post("/invoices/generate")
def generate(body: GenerateIn, user: AdminDep) -> dict:
    """Make or rebuild draft invoices for a month, one per plan held. Safe to run
    again while they are drafts: the same invoices get fresh lines."""
    return _call(service.generate, _period(body.period), user.actor, body.customer_id, body.product)


@router.post("/invoices/{invoice_id}/issue")
def issue(invoice_id: str, user: AdminDep) -> dict:
    return _call(service.issue, invoice_id, user.actor)


class VoidIn(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.post("/invoices/{invoice_id}/void")
def void(invoice_id: str, body: VoidIn, user: AdminDep) -> dict:
    return _call(service.void, invoice_id, body.reason, user.actor)


# ---- accounting export -----------------------------------------------------------------


@router.get("/invoices/{invoice_id}/export/{fmt}")
def export(invoice_id: str, fmt: Literal["xero", "quickbooks", "csv"], user: AdminDep) -> Response:
    """An issued invoice as a Xero or QuickBooks Online invoice payload, or Xero's
    import CSV. Nothing is sent anywhere (ADR 0022)."""
    inv = _one(user, invoice_id)
    try:
        if fmt == "csv":
            return Response(
                accounting.csv_rows(inv),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="accounting-{_filename(inv)}.csv"'},
            )
        body = accounting.xero(inv) if fmt == "xero" else accounting.quickbooks(inv)
    except BillingError as e:
        raise _err(e) from e
    import json

    return Response(json.dumps(service.jsonable(body), indent=2), media_type="application/json")


# ---- payments --------------------------------------------------------------------------


@router.get("/payments/status")
def payment_status(user: AdminDep) -> dict:
    """Which payment adapters are off, simulated or live, and which settings are
    missing (names only, never values)."""
    return payments.status()


class PayIn(BaseModel):
    provider: Literal["stripe", "hosted"]


@router.post("/invoices/{invoice_id}/pay", status_code=201)
def pay(invoice_id: str, body: PayIn, user: ViewerDep, request: Request) -> dict:
    """Start paying an issued invoice: returns the provider's page to send the payer to."""
    settings = request.app.state.settings
    portal = settings.public_url or str(request.base_url).rstrip("/")
    scope = None if user.role == "admin" else user.customer_id
    with db.tx() as conn:
        try:
            inv = service.get_invoice(conn, invoice_id, scope, include_drafts=user.role == "admin")
            return service.jsonable(payments.start(conn, inv, body.provider, user.actor, portal))
        except BillingError as e:
            raise _err(e) from e


class SimulateIn(BaseModel):
    approved: bool = True


@router.post("/payments/{payment_id}/simulate")
def simulate(payment_id: str, body: SimulateIn, user: ViewerDep) -> dict:
    """Simulated mode only: play the provider's signed 'paid' event through the
    webhook, as the provider would after the payer finished."""
    with db.tx() as conn:
        try:
            pay = conn.execute(
                "SELECT customer_id FROM billing_payments WHERE id = %s",
                (payment_id if service._is_uuid(payment_id) else None,),
            ).fetchone()
            if pay is None or (user.role != "admin" and str(pay["customer_id"]) != str(user.customer_id)):
                raise BillingError("Payment not found.", 404)
            headers, raw = payments.simulated_event(conn, payment_id, body.approved)
            provider = conn.execute("SELECT provider FROM billing_payments WHERE id = %s", (payment_id,)).fetchone()
            return service.jsonable(payments.webhook(conn, provider["provider"], headers, raw))
        except BillingError as e:
            raise _err(e) from e


@router.post("/webhooks/{provider}")
async def webhook(provider: Literal["stripe", "hosted"], request: Request) -> dict:
    """A payment provider's signed event. No session: the signature is the check."""
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, "Too large.")
    headers = dict(request.headers)
    with db.tx() as conn:
        try:
            return service.jsonable(payments.webhook(conn, provider, headers, raw))
        except BillingError as e:
            raise _err(e) from e


# ---- supplier side and margin ----------------------------------------------------------


@router.get("/payables")
def payables(user: AdminDep, period: str | None = None) -> dict:
    """What ExaCarib owes each carrier (commit and burst per link, from the settlement
    code) and each Fabric partner (per circuit) for a month."""
    return _call(service.payables, _period(period))


class PartnerCostIn(BaseModel):
    cost_per_mbps_month: str | None = Field(default=None, max_length=20)


@router.put("/partners/{partner_id}/cost")
def partner_cost(partner_id: int, body: PartnerCostIn, user: AdminDep) -> dict:
    return _call(service.set_partner_cost, partner_id, body.cost_per_mbps_month, user.actor)


@router.get("/margin")
def margin(user: AdminDep, period: str | None = None) -> dict:
    """Read only: per organisation and per service, what is billed, what ExaCarib
    owes carriers and partners for it, and the margin."""
    return _call(service.margin, _period(period))
