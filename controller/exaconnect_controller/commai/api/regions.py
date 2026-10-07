"""Regional hosting API (ADR 0025). Regions switch on through the go-live API
(/golive/region/{key}/status) once every dependency has a provider recorded here."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, golive, regions

router = APIRouter(tags=["commai: regions"])


def _admin(user) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Admins only.")


@router.get("/regions")
def list_regions(user: UserDep) -> list[dict]:
    """ExaCarib admins see every dependency; others see which regions they may use."""
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")
    with db.tx() as conn:
        rows = [regions.describe(conn, k) for k in regions.REGIONS]
        if user.role == "admin":
            return rows
        return [
            {"key": r["key"], "name": r["name"],
             "available": r["key"] == regions.DEFAULT_REGION
             or golive.enabled(conn, "region", r["key"], user.customer_id)}
            for r in rows
        ]  # fmt: skip


@router.get("/regions/{key}")
def get_region(key: str, user: UserDep) -> dict:
    _admin(user)
    with db.tx() as conn:
        try:
            return regions.describe(conn, key)
        except regions.RegionError as e:
            raise HTTPException(e.code, str(e)) from e


class ProviderIn(BaseModel):
    provider: str = Field(min_length=1, max_length=120)
    provider_region: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=500)


@router.put("/regions/{key}/dependencies/{dependency}")
def record_provider(key: str, dependency: str, body: ProviderIn, user: UserDep) -> dict:
    _admin(user)
    try:
        with db.tx() as conn:
            out = regions.record(conn, key, dependency, body.provider, body.provider_region, body.notes, user.actor)
            audit.record(conn, user.actor, "commai.region.provider", f"{key}/{dependency}", None, body.model_dump())
            return out
    except (regions.RegionError, golive.GoLiveError) as e:
        raise HTTPException(e.code, str(e)) from e


@router.delete("/regions/{key}/dependencies/{dependency}")
def remove_provider(key: str, dependency: str, user: UserDep) -> dict:
    _admin(user)
    try:
        with db.tx() as conn:
            out = regions.remove(conn, key, dependency, user.actor)
            audit.record(conn, user.actor, "commai.region.provider.remove", f"{key}/{dependency}")
            return out
    except (regions.RegionError, golive.GoLiveError) as e:
        raise HTTPException(e.code, str(e)) from e


class HomeIn(BaseModel):
    region: str = Field(max_length=40)


@router.get("/customers/{customer_id}/data-location")
def data_location(customer_id: str, user: UserDep) -> dict:
    """Where this business's data is kept: its home region and each dependency's provider."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return regions.data_location(conn, customer_id)


@router.put("/customers/{customer_id}/home-region")
def set_home_region(customer_id: str, body: HomeIn, user: UserDep) -> dict:
    """ExaCarib admins only: a region switched on for this business (moving data is a planned migration)."""
    _admin(user)
    try:
        with db.tx() as conn:
            before = conn.execute("SELECT home_region FROM customers WHERE id = %s", (customer_id,)).fetchone()
            if before is None:
                raise HTTPException(404, "No such business.")
            regions.set_home(conn, customer_id, body.region)
            audit.record(conn, user.actor, "commai.region.home", body.region, customer_id,
                         {"from": before["home_region"]})  # fmt: skip
            return regions.data_location(conn, customer_id)
    except regions.RegionError as e:
        raise HTTPException(e.code, str(e)) from e
