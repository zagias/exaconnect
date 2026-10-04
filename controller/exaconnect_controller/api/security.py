"""The encryption report (ADR 0012). Customers see their own; admins any;
carrier users none."""

from __future__ import annotations

from fastapi import APIRouter

from .. import db
from .. import encryption as report
from .deps import UserDep, check_customer

router = APIRouter(tags=["security"])


@router.get("/customers/{customer_id}/encryption")
def get_encryption(customer_id: str, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return report.report(conn, customer_id)
