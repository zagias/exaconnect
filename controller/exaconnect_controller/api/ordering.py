"""Partner directory and plain-English ordering (ADR 0011). Customers order
for their own organisation; admins for any, and complete orders waiting on
a service partner. Carrier users have no access."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import audit, db, ordering
from ..ai import ask as ask_mod
from .deps import AdminDep, UserDep, check_customer

router = APIRouter(tags=["ordering"])

CATEGORIES = Literal["cloud", "saas", "payments", "internet", "security", "content", "other"]
PARTNER_COLUMNS = (
    "id, slug, name, category, kind, provider, description, website, regions, prefixes::text[] AS prefixes,"
    " price_per_mbps_month::float AS price_per_mbps_month, listed, example"
)


class DraftIn(BaseModel):
    text: str = Field(min_length=3, max_length=500)
    engine: Literal["auto", "rules"] = "auto"


class FormIn(BaseModel):
    actions: list[dict[str, Any]] = Field(min_length=1, max_length=ordering.MAX_ACTIONS)


class ConfirmIn(BaseModel):
    inputs: list[dict[str, Any]] = Field(default_factory=list, max_length=ordering.MAX_ACTIONS)


class CompleteIn(BaseModel):
    peer_address: str = Field(max_length=45)
    peer_asn: int = Field(ge=1, le=4294967294)
    psk: str = Field(max_length=64)
    inside_cidr: str | None = Field(default=None, max_length=18)
    prefixes: list[str] | None = Field(default=None, max_length=50)


class PartnerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    category: CATEGORIES | None = None
    provider: Literal["aws", "azure", "gcp", "oracle"] | None = None
    description: str | None = Field(default=None, max_length=400)
    website: str | None = Field(default=None, max_length=200)
    regions: list[str] | None = Field(default=None, max_length=40)
    prefixes: list[str] | None = Field(default=None, max_length=50)
    price_per_mbps_month: float | None = Field(default=None, ge=0, le=1000)
    listed: bool | None = None


class PartnerIn(PartnerPatch):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")
    name: str = Field(min_length=2, max_length=80)
    category: CATEGORIES
    kind: Literal["cloud", "service"]


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _not_carrier(user) -> None:
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")


# ---- directory ------------------------------------------------------------


@router.get("/partners")
def list_partners(user: UserDep, category: CATEGORIES | None = None, q: str | None = None) -> list[dict]:
    _not_carrier(user)
    with db.tx() as conn:
        return conn.execute(
            f"""SELECT {PARTNER_COLUMNS} FROM partners
                WHERE listed AND (%(c)s::text IS NULL OR category = %(c)s)
                  AND (%(q)s::text IS NULL OR name ILIKE %(like)s OR description ILIKE %(like)s)
                ORDER BY kind, example, name""",
            {"c": category, "q": q or None, "like": f"%{(q or '').strip()}%"},
        ).fetchall()


@router.get("/partners/{slug}")
def get_partner(slug: str, user: UserDep) -> dict:
    _not_carrier(user)
    with db.tx() as conn:
        row = conn.execute(f"SELECT {PARTNER_COLUMNS} FROM partners WHERE slug = %s AND listed", (slug,)).fetchone()
    if row is None:
        raise HTTPException(404, "Partner not found.")
    return row


def _partner_values(body: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "cloud" and "provider" in body and not body["provider"]:
        raise HTTPException(400, "A cloud partner needs its cloud: AWS, Azure, Google Cloud or Oracle.")
    if body.get("prefixes") is not None:
        problems: list[str] = []
        body["prefixes"] = ordering.cidr_list(body["prefixes"], problems)
        if problems:
            raise HTTPException(400, problems[0])
    if body.get("website") and not str(body["website"]).startswith("https://"):
        raise HTTPException(400, "The website starts with https://.")
    return body


@router.get("/admin/partners")
def admin_partners(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(f"SELECT {PARTNER_COLUMNS} FROM partners ORDER BY kind, name").fetchall()


@router.post("/admin/partners", status_code=201)
def create_partner(body: PartnerIn, user: AdminDep) -> dict:
    data = body.model_dump()
    if data["kind"] == "cloud" and not data.get("provider"):
        raise HTTPException(400, "A cloud partner needs its cloud: AWS, Azure, Google Cloud or Oracle.")
    if data["kind"] == "service":
        data["provider"] = None
    with db.tx() as conn:
        data = _partner_values(data, data["kind"])
        if conn.execute("SELECT 1 FROM partners WHERE slug = %s", (data["slug"],)).fetchone():
            raise HTTPException(400, f"A partner called {data['slug']} already exists.")
        row = conn.execute(
            """INSERT INTO partners (slug, name, category, kind, provider, description, website, regions, prefixes,
                                     price_per_mbps_month, listed)
               VALUES (%(slug)s, %(name)s, %(category)s, %(kind)s, %(provider)s, %(description)s, %(website)s,
                       %(regions)s, %(prefixes)s::cidr[], %(price)s, %(listed)s) RETURNING id""",
            {
                **data,
                "description": data.get("description") or "",
                "website": data.get("website") or "",
                "regions": data.get("regions") or [],
                "prefixes": data.get("prefixes") or [],
                "price": data.get("price_per_mbps_month") if data.get("price_per_mbps_month") is not None else 2.0,
                "listed": data.get("listed") is not False,
            },
        ).fetchone()
        audit.record(conn, user.actor, "partner.create", data["slug"], None, {"id": row["id"]})
        return conn.execute(f"SELECT {PARTNER_COLUMNS} FROM partners WHERE id = %s", (row["id"],)).fetchone()


@router.patch("/admin/partners/{partner_id}")
def update_partner(partner_id: int, body: PartnerPatch, user: AdminDep) -> dict:
    changes = body.model_dump(exclude_unset=True)
    with db.tx() as conn:
        p = conn.execute("SELECT * FROM partners WHERE id = %s FOR UPDATE", (partner_id,)).fetchone()
        if p is None:
            raise HTTPException(404, "Partner not found.")
        if p["kind"] == "service":
            changes.pop("provider", None)
        changes = _partner_values(changes, p["kind"])
        for k, v in changes.items():
            if v is None and k not in ("provider",):
                continue
            cast = "::cidr[]" if k == "prefixes" else ""
            conn.execute(f"UPDATE partners SET {k} = %s{cast}, updated_at = now() WHERE id = %s", (v, partner_id))  # noqa: S608
        audit.record(conn, user.actor, "partner.update", p["slug"], None, {k: str(v) for k, v in changes.items()})
        return conn.execute(f"SELECT {PARTNER_COLUMNS} FROM partners WHERE id = %s", (partner_id,)).fetchone()


@router.delete("/admin/partners/{partner_id}", status_code=204)
def delete_partner(partner_id: int, user: AdminDep) -> Response:
    """Deletes a partner, or unlists it when circuits or orders refer to it."""
    with db.tx() as conn:
        p = conn.execute("SELECT * FROM partners WHERE id = %s FOR UPDATE", (partner_id,)).fetchone()
        if p is None:
            raise HTTPException(404, "Partner not found.")
        used = conn.execute(
            """SELECT EXISTS (SELECT 1 FROM circuits WHERE partner_id = %s)
                   OR EXISTS (SELECT 1 FROM orders WHERE actions @> %s::jsonb) AS used""",
            (partner_id, f'[{{"partner": "{p["slug"]}"}}]'),
        ).fetchone()["used"]
        if used:
            conn.execute("UPDATE partners SET listed = false, updated_at = now() WHERE id = %s", (partner_id,))
        else:
            conn.execute("DELETE FROM partners WHERE id = %s", (partner_id,))
        audit.record(conn, user.actor, "partner.delete", p["slug"], None, {"unlisted_only": used})
    return Response(status_code=204)


# ---- orders ---------------------------------------------------------------


def _order(conn, customer_id: str, order_id: int, lock: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM orders WHERE id = %s AND customer_id = %s" + (" FOR UPDATE" if lock else ""),
        (order_id, customer_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Order not found.")
    return row


def _view(conn, order_id: int) -> dict:
    return ordering.view(conn, conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,)).fetchone())


@router.post("/customers/{customer_id}/orders/draft", status_code=201)
async def draft(customer_id: str, body: DraftIn, user: UserDep, request: Request) -> dict:
    """Draft an order from plain English. Nothing changes until it is confirmed."""
    check_customer(user, customer_id)
    s = request.app.state.settings
    use_ai = body.engine == "auto" and bool(s.llm_api_key)

    def prepare() -> dict:
        with db.tx() as conn:
            if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
                raise HTTPException(404, "Customer not found.")
            if use_ai:
                used = conn.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE actor = %s AND action IN ('ai.ask', 'ai.order')"
                    " AND at > now() - interval '1 hour'",
                    (user.actor,),
                ).fetchone()["n"]
                if used >= s.llm_questions_per_hour:
                    raise HTTPException(429, "That's the limit of AI requests for this hour. Try again later.")
                audit.record(conn, user.actor, "ai.order", body.text[:200], customer_id)
            return ordering.context(conn, customer_id)

    ctx = await asyncio.to_thread(prepare)
    engine = "rules"
    actions = None
    if use_ai:
        try:
            actions = await asyncio.to_thread(ordering.parse_ai, body.text, ctx, s)
            engine = "ai"
        except ask_mod.AskError:
            actions = None  # The rules parser still has a go.
    if actions is None:
        actions = ordering.parse_rules(body.text, ctx)

    def save() -> dict:
        with db.tx() as conn:
            oid = ordering.create(conn, customer_id, actions, engine, body.text, user.actor)
            return _view(conn, oid)

    return await asyncio.to_thread(save)


@router.post("/customers/{customer_id}/orders", status_code=201)
def create_order(customer_id: str, body: FormIn, user: UserDep) -> dict:
    """A draft from a form, such as the directory's Connect button."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(404, "Customer not found.")
        oid = ordering.create(conn, customer_id, body.actions, "form", "", user.actor)
        return _view(conn, oid)


@router.get("/customers/{customer_id}/orders")
def list_orders(customer_id: str, user: UserDep) -> list[dict]:
    check_customer(user, customer_id)
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE customer_id = %s ORDER BY id DESC LIMIT 50", (customer_id,)
        ).fetchall()
        return [ordering.view(conn, r) for r in rows]


@router.get("/customers/{customer_id}/orders/{order_id}")
def get_order(customer_id: str, order_id: int, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return ordering.view(conn, _order(conn, customer_id, order_id))


@router.post("/customers/{customer_id}/orders/{order_id}/confirm")
def confirm(customer_id: str, order_id: int, body: ConfirmIn, user: UserDep) -> dict:
    """Apply every change in the order, or none."""
    check_customer(user, customer_id)
    try:
        with db.tx() as conn:
            ordering.confirm(conn, _order(conn, customer_id, order_id, lock=True), body.inputs, user.actor)
    except ordering.OrderError as e:
        raise _bad(e) from None
    with db.tx() as conn:
        return _view(conn, order_id)


@router.post("/customers/{customer_id}/orders/{order_id}/cancel")
def cancel(customer_id: str, order_id: int, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        try:
            ordering.cancel(conn, _order(conn, customer_id, order_id, lock=True), user.actor)
        except ordering.OrderError as e:
            raise _bad(e) from None
        return _view(conn, order_id)


@router.get("/admin/orders")
def admin_orders(
    user: AdminDep, status: Literal["draft", "done", "pending_partner", "cancelled", "failed"] | None = None
) -> list[dict]:
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT o.*, c.name AS customer FROM orders o JOIN customers c ON c.id = o.customer_id
               WHERE (%s::text IS NULL OR o.status = %s) ORDER BY o.id DESC LIMIT 200""",
            (status, status),
        ).fetchall()
        return [{**ordering.view(conn, r), "customer_id": r["customer_id"], "customer": r["customer"]} for r in rows]


@router.post("/admin/orders/{order_id}/complete")
def complete(order_id: int, body: CompleteIn, user: AdminDep) -> dict:
    """Enter a service partner's gateway details: its connections become circuits."""
    try:
        with db.tx() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = %s FOR UPDATE", (order_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Order not found.")
            ordering.complete(conn, row, body.model_dump(), user.actor)
    except ordering.OrderError as e:
        raise _bad(e) from None
    with db.tx() as conn:
        return _view(conn, order_id)
