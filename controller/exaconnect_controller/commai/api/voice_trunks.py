"""Jibsy voice: the organisation's own SIP trunks (ADR 0044).

Trunks, the organisation default, a primary and secondary trunk per location,
numbers that arrive on a trunk, and a route preview ("which trunks would a call
from this extension to this number try?"). Reading needs Jibsy read access;
every change needs a voice admin and is audited.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import db
from ...api.deps import UserDep
from .. import access
from ..voice import config, perms, trunks
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}/voice", tags=["commai: voice trunks"])


def _admin(conn, user, cid) -> None:
    access.check(user, cid, "commai:admin")
    perms.require_voice_admin(conn, user, cid)


class TrunkIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    adapter: str | None = Field(default=None, pattern="^(sip|simulated)$")
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    transport: str | None = Field(default=None, pattern="^(udp|tcp|tls)$")
    prefix: str | None = Field(default=None, max_length=10)
    max_channels: int | None = Field(default=None, ge=1, le=10000)
    allow_ips: list[str] | None = Field(default=None, max_length=32)
    enabled: bool | None = None
    sim_fail: str | None = Field(default=None, pattern="^(|refuse|down)$")


class SettingsIn(BaseModel):
    default_trunk_id: str | None = None
    exacarib_fallback: bool | None = None


class LocationIn(BaseModel):
    primary_trunk_id: str | None = None
    secondary_trunk_id: str | None = None


class NumberIn(BaseModel):
    e164: str = Field(max_length=40)
    location_id: str
    target_type: str = Field(default="none", pattern="^(none|user|ring_group|queue|menu|ai)$")
    target: str | None = Field(default=None, max_length=120)


class NumberLocationIn(BaseModel):
    location_id: str


@router.get("/trunks")
def overview(customer_id: str, user: UserDep) -> dict:
    """Everything the SIP trunks screen shows."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return {
            "trunks": trunks.list_trunks(conn, customer_id),
            "settings": trunks.settings(conn, customer_id),
            "locations": trunks.locations(conn, customer_id),
            "numbers": trunks.numbers(conn, customer_id),
        }


@router.post("/trunks", status_code=201)
def add_trunk(customer_id: str, body: TrunkIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return trunks.save_trunk(conn, customer_id, body.model_dump(exclude_none=True), user.actor)


@router.patch("/trunks/{trunk_id}")
def change_trunk(customer_id: str, trunk_id: str, body: TrunkIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return trunks.save_trunk(conn, customer_id, body.model_dump(exclude_none=True), user.actor, trunk_id)


@router.delete("/trunks/{trunk_id}", status_code=204)
def delete_trunk(customer_id: str, trunk_id: str, user: UserDep) -> None:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        trunks.delete_trunk(conn, customer_id, trunk_id, user.actor)


@router.put("/trunk-settings")
def put_settings(customer_id: str, body: SettingsIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return trunks.save_settings(conn, customer_id, body.model_dump(exclude_unset=True), user.actor)


@router.put("/locations/{location_id}/trunks")
def put_location(customer_id: str, location_id: str, body: LocationIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return trunks.set_location(conn, customer_id, location_id, body.model_dump(), user.actor)


@router.post("/trunks/{trunk_id}/numbers", status_code=201)
def add_number(customer_id: str, trunk_id: str, body: NumberIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return trunks.add_number(conn, customer_id, trunk_id, body.model_dump(), user.actor)


@router.put("/numbers/{number_id}/location")
def put_number_location(customer_id: str, number_id: str, body: NumberLocationIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return trunks.set_number_location(conn, customer_id, number_id, body.model_dump(), user.actor)


@router.get("/route")
def route_preview(customer_id: str, extension: str, to: str, user: UserDep) -> dict:
    """The trunks and carriers a call from this extension to this number would try, in order."""
    access.check(user, customer_id, "commai:read")
    if not re.fullmatch(r"\d{2,6}", extension):
        raise HTTPException(422, "Give an extension, for example 201.")
    with db.tx() as conn, errors():
        vu = conn.execute(
            "SELECT id, name FROM voice_users WHERE customer_id = %s AND extension = %s AND status = 'active'",
            (customer_id, extension),
        ).fetchone()
        if vu is None:
            raise HTTPException(422, f"There is no extension {extension}.")
        try:
            number = config.norm_e164(to)
        except config.OpError as e:
            raise HTTPException(422, str(e)) from e
        out = trunks.route(conn, customer_id, number, vu["id"])
        if out is None:
            note = "This organisation has no trunks of its own; ExaCarib's carriers take its calls."
            return {
                "own_trunks": False,
                "to": number,
                "caller": vu["name"],
                "location": None,
                "steps": [],
                "route": [],
                "note": note,
            }
        return {"own_trunks": True, "to": number, "caller": vu["name"], **out}
