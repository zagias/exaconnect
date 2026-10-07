"""Module entitlements API (ADR 0033). A business reads its plan; only an
ExaCarib admin changes it."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, entitlements

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: plan"])


@router.get("/entitlements")
def get_entitlements(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        have = entitlements.all_for(conn, customer_id)
    return {"modules": [{"module": m, "label": label, "enabled": have[m]} for m, label in entitlements.MODULES.items()]}


class EntitlementsIn(BaseModel):
    modules: dict[str, bool] = Field(min_length=1, max_length=10)
    note: str = Field(default="", max_length=300)


@router.put("/entitlements")
def set_entitlements(customer_id: str, body: EntitlementsIn, user: UserDep) -> dict:
    """ExaCarib admins only: switch modules on or off for a business."""
    if user.role != "admin":
        raise HTTPException(403, "Only ExaCarib can change which modules a business has.")
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id::text = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(404, "Business not found.")
        try:
            have = entitlements.set_modules(conn, customer_id, body.modules, user.actor, body.note)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        audit.record(conn, user.actor, "commai.entitlements.set", customer_id, customer_id, {"modules": body.modules})
    return {"modules": [{"module": m, "enabled": have[m]} for m in entitlements.MODULES]}
