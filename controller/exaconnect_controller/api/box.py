"""Cloud management of ExaCarib boxes (ADR 0049): where each box is managed from."""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import box as boxes
from .. import db
from .deps import UserDep, check_customer, require_org_manager

router = APIRouter(tags=["box"])
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ManagedIn(BaseModel):
    mode: Literal["site", "cloud", "both"]


class PortIn(BaseModel):
    iface: str = Field(max_length=15)
    path: str = Field(default="", max_length=16)
    mode: Literal["dhcp", "static", "pppoe", "keep"]
    address: str = Field(default="", max_length=64)
    gateway: str = Field(default="", max_length=64)
    dns: list[str] = Field(default=[], max_length=4)
    pppoe_user: str = Field(default="", max_length=128)
    pppoe_password: str = Field(default="", max_length=128)


class NetworkIn(BaseModel):
    wan: list[PortIn] = Field(max_length=3)
    lan: PortIn | None = None
    lan_dhcp: bool = False


def _node(conn, site_id: str) -> dict:
    if not _UUID.match(site_id):
        raise HTTPException(404, "No agent at this site.")
    row = conn.execute(
        "SELECT id, name, customer_id, box, box_managed, box_network FROM nodes WHERE site_id = %s FOR UPDATE",
        (site_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "No agent at this site.")
    return row


@router.get("/sites/{site_id}/box/managed")
def get_managed(site_id: str, user: UserDep) -> dict:
    with db.tx() as conn:
        n = _node(conn, site_id)
    check_customer(user, n["customer_id"])
    return {"console": bool((n["box"] or {}).get("console")), "managed": boxes.managed(n)}


@router.post("/sites/{site_id}/box/managed")
def set_managed(site_id: str, body: ManagedIn, user: UserDep) -> dict:
    """Switch where a box is managed from: on site (its console only), the cloud
    (the portal only; the console reads) or both (latest change wins). The box
    keeps its settings as they are. For the organisation's owners and admins."""
    with db.tx() as conn:
        n = _node(conn, site_id)
        require_org_manager(user, n["customer_id"])
        if not (n["box"] or {}).get("console"):
            raise HTTPException(409, "This site's agent does not run the box console.")
        m = boxes.set_from_portal(conn, n, body.mode, user.actor)
    return {"console": True, "managed": {"mode": m["mode"], "at": m["at"], "by": m["by"]}}


@router.get("/sites/{site_id}/box/network")
def get_network(site_id: str, user: UserDep) -> dict:
    with db.tx() as conn:
        n = _node(conn, site_id)
    check_customer(user, n["customer_id"])
    box = n["box"] or {}
    return {
        "console": bool(box.get("console")),
        "managed": boxes.managed(n),
        "box": box,
        "portal": boxes.public_network(n["box_network"]),
    }


@router.post("/sites/{site_id}/box/network")
def set_network(site_id: str, body: NetworkIn, user: UserDep) -> dict:
    """Set a box's ports from the portal (ADR 0051). The box takes them while it
    is managed from the cloud or both, and keeps them only once it can still
    reach ExaCarib with them; otherwise it puts its earlier ports back after two
    minutes. For the organisation's owners and admins."""
    with db.tx() as conn:
        n = _node(conn, site_id)
        require_org_manager(user, n["customer_id"])
        if not (n["box"] or {}).get("console"):
            raise HTTPException(409, "This site's agent does not run the box console.")
        if boxes.managed(n)["mode"] == "site":
            raise HTTPException(409, "This box is managed on site. Switch Managed from to Cloud or both first.")
        try:
            bn = boxes.set_network(conn, n, body.model_dump(), user.actor)
        except boxes.NetworkError as e:
            raise HTTPException(400, str(e)) from None
    return {"portal": boxes.public_network(bn)}
