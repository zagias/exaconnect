"""Internet breakout, NAT gateway and firewall (ADR 0010). Customers manage
their own; admins act for any customer. Carrier users have no access."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, internet
from .deps import UserDep, check_customer

router = APIRouter(tags=["internet"])


class ModeIn(BaseModel):
    mode: Literal["pop", "local", "off"]


class RulePatch(BaseModel):
    site_id: str | None = None
    action: Literal["allow", "deny"] | None = None
    src: list[str] | None = Field(default=None, max_length=50)
    dst: list[str] | None = Field(default=None, max_length=50)
    protocol: Literal["any", "tcp", "udp", "icmp"] | None = None
    ports: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None


class RuleIn(RulePatch):
    action: Literal["allow", "deny"]
    position: int | None = Field(default=None, ge=1)


class OrderIn(BaseModel):
    ids: list[int] = Field(max_length=internet.MAX_RULES)


class ForwardPatch(BaseModel):
    description: str | None = Field(default=None, max_length=120)
    protocol: Literal["tcp", "udp"] | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    to_site_id: str | None = None
    to_address: str | None = Field(default=None, max_length=45)
    to_port: int | None = Field(default=None, ge=1, le=65535)
    allow_from: list[str] | None = Field(default=None, max_length=50)
    enabled: bool | None = None


class ForwardIn(ForwardPatch):
    protocol: Literal["tcp", "udp"]
    port: int = Field(ge=1, le=65535)
    to_site_id: str
    to_address: str = Field(max_length=45)


def _bad(e: internet.InternetError) -> HTTPException:
    return HTTPException(400, str(e))


@router.get("/customers/{customer_id}/internet")
def get_internet(customer_id: str, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return internet.view(conn, customer_id)


@router.patch("/customers/{customer_id}/internet/sites/{site_id}")
def set_mode(customer_id: str, site_id: str, body: ModeIn, user: UserDep) -> dict:
    """Send a site's internet traffic through the PoP, straight out of its own links, or nowhere."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        try:
            internet.set_mode(conn, customer_id, site_id, body.mode, user.actor)
        except internet.InternetError as e:
            raise _bad(e) from None
        return internet.view(conn, customer_id)


def _rule(conn, customer_id: str, rule_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM firewall_rules WHERE customer_id = %s AND id = %s FOR UPDATE", (customer_id, rule_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Rule not found.")
    return row


def _rule_view(conn, customer_id: str, rule_id: int) -> dict:
    return next(r for r in internet.view(conn, customer_id)["rules"] if r["id"] == rule_id)


@router.post("/customers/{customer_id}/firewall/rules", status_code=201)
def create_rule(customer_id: str, body: RuleIn, user: UserDep) -> dict:
    """A firewall rule for internet traffic. Rules apply in order, first match wins, then allow."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(404, "Customer not found.")
        try:
            rid = internet.create_rule(conn, customer_id, body.model_dump(), user.actor)
        except internet.InternetError as e:
            raise _bad(e) from None
        return _rule_view(conn, customer_id, rid)


@router.patch("/customers/{customer_id}/firewall/rules/{rule_id}")
def update_rule(customer_id: str, rule_id: int, body: RulePatch, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        rule = _rule(conn, customer_id, rule_id)
        try:
            internet.update_rule(conn, customer_id, rule, body.model_dump(exclude_unset=True), user.actor)
        except internet.InternetError as e:
            raise _bad(e) from None
        return _rule_view(conn, customer_id, rule_id)


@router.delete("/customers/{customer_id}/firewall/rules/{rule_id}", status_code=204)
def delete_rule(customer_id: str, rule_id: int, user: UserDep) -> None:
    check_customer(user, customer_id)
    with db.tx() as conn:
        internet.delete_rule(conn, customer_id, _rule(conn, customer_id, rule_id), user.actor)


@router.post("/customers/{customer_id}/firewall/order")
def order_rules(customer_id: str, body: OrderIn, user: UserDep) -> list[dict]:
    """Set the order of every rule (all of the organisation's rule ids, first match first)."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        try:
            internet.reorder(conn, customer_id, body.ids, user.actor)
        except internet.InternetError as e:
            raise _bad(e) from None
        return internet.view(conn, customer_id)["rules"]


def _forward(conn, customer_id: str, forward_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM port_forwards WHERE customer_id = %s AND id = %s FOR UPDATE", (customer_id, forward_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Port forward not found.")
    return row


def _forward_view(conn, customer_id: str, forward_id: int) -> dict:
    return next(f for f in internet.view(conn, customer_id)["forwards"] if f["id"] == forward_id)


@router.post("/customers/{customer_id}/port-forwards", status_code=201)
def create_forward(customer_id: str, body: ForwardIn, user: UserDep) -> dict:
    """Forward a public port on the PoP to an address at a site. Works while that site goes through the PoP."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(404, "Customer not found.")
        try:
            fid = internet.create_forward(conn, customer_id, body.model_dump(), user.actor)
        except internet.InternetError as e:
            raise _bad(e) from None
        return _forward_view(conn, customer_id, fid)


@router.patch("/customers/{customer_id}/port-forwards/{forward_id}")
def update_forward(customer_id: str, forward_id: int, body: ForwardPatch, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        fwd = _forward(conn, customer_id, forward_id)
        try:
            internet.update_forward(conn, customer_id, fwd, body.model_dump(exclude_unset=True), user.actor)
        except internet.InternetError as e:
            raise _bad(e) from None
        return _forward_view(conn, customer_id, forward_id)


@router.delete("/customers/{customer_id}/port-forwards/{forward_id}", status_code=204)
def delete_forward(customer_id: str, forward_id: int, user: UserDep) -> None:
    check_customer(user, customer_id)
    with db.tx() as conn:
        internet.delete_forward(conn, customer_id, _forward(conn, customer_id, forward_id), user.actor)
