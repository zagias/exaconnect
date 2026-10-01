"""Traffic rules and application detection (ADR 0007). Customers manage
their own; admins act for any customer. Carrier users have no access."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, traffic
from ..ai import apps, detect
from .deps import UserDep, check_customer

router = APIRouter(tags=["traffic"])


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    class_name: str = Field(max_length=20)
    site_ids: list[str] = Field(default=[], max_length=100)
    apps: list[str] = Field(default=[], max_length=20)
    ports: str = Field(default="", max_length=500)
    dst_subnets: list[str] = Field(default=[], max_length=50)
    src_subnets: list[str] = Field(default=[], max_length=50)
    vlans: list[int] = Field(default=[], max_length=50)
    domains: list[str] = Field(default=[], max_length=50)
    dscp: list[int] = Field(default=[], max_length=64)
    enabled: bool = True
    ordinal: int = Field(default=100, ge=1, le=1000)


@router.get("/applications/catalogue")
def catalogue(user: UserDep) -> list[dict]:
    """The applications a rule can name and detection recognises."""
    return apps.catalogue_view()


@router.get("/customers/{customer_id}/paths")
def list_paths(customer_id: str, user: UserDep) -> list[dict]:
    """The paths a class can prefer: one row per path name, with its carriers."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        return conn.execute(
            """SELECT p.name, p.label, p.ordinal, array_agg(DISTINCT c.name) AS carriers
               FROM links l JOIN paths p ON p.name = l.path JOIN carriers c ON c.id = l.carrier_id
               WHERE l.customer_id = %s GROUP BY p.name, p.label, p.ordinal ORDER BY p.ordinal""",
            (customer_id,),
        ).fetchall()


@router.get("/customers/{customer_id}/rules")
def list_rules(customer_id: str, user: UserDep) -> list[dict]:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return traffic.load_rules(conn, customer_id)


def _save(customer_id: str, body: RuleIn, user, rule_id: int | None = None) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(404, "Customer not found.")
        try:
            rid = traffic.save_rule(conn, customer_id, body.model_dump(), user.actor, rule_id)
        except traffic.RuleError as e:
            raise HTTPException(400, str(e)) from None
        except LookupError:
            raise HTTPException(404, "Rule not found.") from None
        return next(r for r in traffic.load_rules(conn, customer_id) if r["id"] == rid)


@router.post("/customers/{customer_id}/rules", status_code=201)
def create_rule(customer_id: str, body: RuleIn, user: UserDep) -> dict:
    """Put matching traffic in a class. Agents apply it on their next poll."""
    return _save(customer_id, body, user)


@router.put("/customers/{customer_id}/rules/{rule_id}")
def update_rule(customer_id: str, rule_id: int, body: RuleIn, user: UserDep) -> dict:
    return _save(customer_id, body, user, rule_id)


@router.delete("/customers/{customer_id}/rules/{rule_id}", status_code=204)
def delete_rule(customer_id: str, rule_id: int, user: UserDep) -> None:
    check_customer(user, customer_id)
    with db.tx() as conn:
        try:
            traffic.delete_rule(conn, customer_id, rule_id, user.actor)
        except LookupError:
            raise HTTPException(404, "Rule not found.") from None


@router.get("/customers/{customer_id}/applications")
def list_detections(customer_id: str, user: UserDep, include_closed: bool = False) -> list[dict]:
    """Applications seen at the customer's sites, with the suggested class."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        return conn.execute(
            """SELECT d.*, s.name AS site FROM app_detections d JOIN sites s ON s.id = d.site_id
               WHERE d.customer_id = %s AND (%s OR d.status = 'suggested')
               ORDER BY d.status = 'suggested' DESC, d.confidence DESC, d.last_seen DESC""",
            (customer_id, include_closed),
        ).fetchall()


@router.post("/customers/{customer_id}/applications/detect")
def detect_now(customer_id: str, user: UserDep) -> dict:
    """Run detection now rather than waiting for the next five-minute pass."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        customer = conn.execute("SELECT id, auto_prioritise FROM customers WHERE id = %s", (customer_id,)).fetchone()
        if customer is None:
            raise HTTPException(404, "Customer not found.")
        return {"detections": len(detect.detect(conn, customer, dt.datetime.now(dt.UTC)))}


def _detection(conn, detection_id: int, user) -> dict[str, Any]:
    det = conn.execute("SELECT * FROM app_detections WHERE id = %s FOR UPDATE", (detection_id,)).fetchone()
    if det is None:
        raise HTTPException(404, "Detection not found.")
    check_customer(user, det["customer_id"])
    return det


class ApplyIn(BaseModel):
    class_name: str | None = Field(default=None, max_length=20)


@router.post("/applications/{detection_id}/apply")
def apply_detection(detection_id: int, user: UserDep, body: ApplyIn | None = None) -> dict:
    """Create a traffic rule from a detection (optionally into another class)."""
    with db.tx() as conn:
        det = _detection(conn, detection_id, user)
        if det["status"] == "applied":
            raise HTTPException(400, "This one is already applied.")
        try:
            rule_id = detect.apply(conn, det, user.actor, body.class_name if body else None)
        except traffic.RuleError as e:
            raise HTTPException(400, str(e)) from None
    return {"rule_id": rule_id}


@router.post("/applications/{detection_id}/dismiss", status_code=204)
def dismiss_detection(detection_id: int, user: UserDep) -> None:
    with db.tx() as conn:
        detect.dismiss(conn, _detection(conn, detection_id, user), user.actor)
