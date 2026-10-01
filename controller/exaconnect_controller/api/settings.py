"""Customer settings: shadow mode (and, from M5, Storm Mode)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import audit, db
from ..routing import maps
from .deps import UserDep

router = APIRouter(tags=["settings"])


def _check(user, customer_id: str) -> None:
    if user.role == "admin":
        return
    if user.role == "customer" and str(user.customer_id) == customer_id:
        return
    raise HTTPException(403, "Not available for this account.")


@router.get("/customers/{customer_id}/settings")
def get_settings(customer_id: str, user: UserDep) -> dict:
    _check(user, customer_id)
    with db.tx() as conn:
        row = conn.execute(
            """SELECT id, name, shadow_mode, storm_mode, storm_since, storm_by, storm_allow_bulk_sat
               FROM customers WHERE id = %s""",
            (customer_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Customer not found.")
    return row


class SettingsIn(BaseModel):
    shadow_mode: bool | None = None


@router.patch("/customers/{customer_id}/settings")
def update_settings(customer_id: str, body: SettingsIn, user: UserDep) -> dict:
    """Shadow mode logs routing decisions without acting on them. Switching
    it either way starts every class from its default path."""
    _check(user, customer_id)
    with db.tx() as conn:
        row = conn.execute("SELECT shadow_mode FROM customers WHERE id = %s FOR UPDATE", (customer_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Customer not found.")
        if body.shadow_mode is not None and body.shadow_mode != row["shadow_mode"]:
            conn.execute("UPDATE customers SET shadow_mode = %s WHERE id = %s", (body.shadow_mode, customer_id))
            conn.execute("DELETE FROM steering WHERE customer_id = %s", (customer_id,))
            audit.record(conn, user.actor, "settings.shadow_mode", str(body.shadow_mode), customer_id)
            maps.refresh(conn, customer_id)
    return get_settings(customer_id, user)
