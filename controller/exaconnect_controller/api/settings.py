"""Customer settings: shadow mode and Storm Mode."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import audit, db
from ..routing import maps
from ..storm.service import set_storm
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


class StormIn(BaseModel):
    on: bool
    allow_bulk_satellite: bool | None = None


@router.post("/customers/{customer_id}/storm")
def storm(customer_id: str, body: StormIn, user: UserDep) -> dict:
    """Switch Storm Mode on or off. Who switched it and when is recorded."""
    _check(user, customer_id)
    if body.allow_bulk_satellite is not None and user.role != "admin":
        raise HTTPException(403, "Only an admin can allow bulk traffic on satellite.")
    with db.tx() as conn:
        try:
            set_storm(conn, customer_id, body.on, user.actor, body.allow_bulk_satellite)
        except LookupError:
            raise HTTPException(404, "Customer not found.") from None
    return get_settings(customer_id, user)


@router.get("/customers/mine")
def my_customers(user: UserDep) -> list[dict]:
    """The customers this account can act for: all for admins, its own otherwise."""
    with db.tx() as conn:
        return conn.execute(
            """SELECT id, name, shadow_mode, storm_mode, storm_since, storm_by, storm_allow_bulk_sat
               FROM customers WHERE %(c)s::uuid IS NULL OR id = %(c)s ORDER BY name""",
            {"c": None if user.role == "admin" else user.customer_id},
        ).fetchall()
