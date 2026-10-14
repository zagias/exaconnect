"""Jibsy voice: Teams, Zoom Phone, Webex Calling, Google Voice and contact-centre
platforms joined to Jibsy (ADR 0056).

Links (one per platform per organisation), the people or queues on each with their
Jibsy number and extension, a set-up guide for the platform's own admin, and test
calls. Reading needs Jibsy read access; every change needs a voice admin and is
audited. ExaCarib staff see the Microsoft 365 DNS records to publish for Teams.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ... import db
from ...api.deps import UserDep
from .. import access
from ..voice import perms, platforms
from .common import errors

router = APIRouter(tags=["commai: voice calling platforms"])
C = "/customers/{customer_id}/voice"


def _admin(conn, user, cid) -> None:
    access.check(user, cid, "commai:admin")
    perms.require_voice_admin(conn, user, cid)


class LinkIn(BaseModel):
    platform: str | None = Field(default=None, pattern="^(teams|zoom|webex|google|genesys|nice|talkdesk)$")
    mode: str | None = Field(default=None, pattern="^(live|simulated)$")
    enabled: bool | None = None
    hosts: list[str] | None = Field(default=None, max_length=16)
    allow_ips: list[str] | None = Field(default=None, max_length=16)
    max_channels: int | None = Field(default=None, ge=1, le=10000)
    sim_fail: str | None = Field(default=None, pattern="^(|refuse|down)$")
    dns_txt: str | None = Field(default=None, max_length=200)


class UserIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    account: str | None = Field(default=None, max_length=200)
    number_id: str | None = None
    extension: str | None = Field(default=None, max_length=6)
    enabled: bool | None = None


class TestIn(BaseModel):
    user_id: str
    direction: str = Field(pattern="^(to|from)$")
    to: str | None = Field(default=None, max_length=40)


class DnsIn(BaseModel):
    done: bool


@router.get(C + "/platforms")
def overview(customer_id: str, user: UserDep) -> dict:
    """Everything the calling platforms screen shows."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return {
            "platforms": platforms.catalogue(),
            "links": platforms.list_links(conn, customer_id),
            "numbers": platforms.free_numbers(conn, customer_id),
            "sip_domain": platforms.sip_domain(),
        }


@router.post(C + "/platforms", status_code=201)
def add_link(customer_id: str, body: LinkIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return platforms.add_link(conn, customer_id, body.model_dump(exclude_none=True), user.actor)


@router.patch(C + "/platforms/{link_id}")
def change_link(customer_id: str, link_id: str, body: LinkIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return platforms.change_link(conn, customer_id, link_id, body.model_dump(exclude_none=True), user.actor)


@router.delete(C + "/platforms/{link_id}", status_code=204)
def delete_link(customer_id: str, link_id: str, user: UserDep) -> None:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        platforms.delete_link(conn, customer_id, link_id, user.actor)


@router.post(C + "/platforms/{link_id}/users", status_code=201)
def add_user(customer_id: str, link_id: str, body: UserIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return platforms.save_user(conn, customer_id, {**body.model_dump(), "link_id": link_id}, user.actor)


@router.patch(C + "/platform-users/{user_id}")
def change_user(customer_id: str, user_id: str, body: UserIn, user: UserDep) -> dict:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return platforms.save_user(conn, customer_id, body.model_dump(exclude_unset=True), user.actor, user_id)


@router.delete(C + "/platform-users/{user_id}", status_code=204)
def delete_user(customer_id: str, user_id: str, user: UserDep) -> None:
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        platforms.delete_user(conn, customer_id, user_id, user.actor)


@router.post(C + "/platforms/{link_id}/test")
def test_call(customer_id: str, link_id: str, body: TestIn, user: UserDep) -> dict:
    """A test call between Jibsy and someone on the platform, step by step (no phone needed)."""
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return platforms.test_call(conn, customer_id, link_id, body.model_dump())


@router.get("/voice-admin/platform-dns")
def dns_requests(user: UserDep) -> list[dict]:
    """Microsoft 365 TXT records ExaCarib publishes for organisations joining Teams."""
    perms.require_exacarib(user)
    with db.tx() as conn:
        return platforms.dns_requests(conn)


@router.put("/voice-admin/platform-dns/{link_id}")
def mark_dns(link_id: str, body: DnsIn, user: UserDep) -> dict:
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        return platforms.mark_dns(conn, link_id, body.done, user.actor)
