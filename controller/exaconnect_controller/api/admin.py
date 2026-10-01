"""Admin inventory API: customers, sites, links, enrolment tokens, nodes, audit."""

from __future__ import annotations

import datetime as dt
import ipaddress
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .. import db, inventory
from .deps import AdminDep, ViewerDep, customer_scope

router = APIRouter(tags=["admin"])


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/customers")
def list_customers(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        return conn.execute("SELECT id, name, created_at FROM customers ORDER BY name").fetchall()


@router.post("/customers", status_code=201)
def create_customer(body: CustomerIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        return {"id": inventory.ensure_customer(conn, body.name, user.actor)}


class SiteIn(BaseModel):
    customer_id: str
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    kind: Literal["site", "pop"] = "site"
    location: str = ""
    timezone: str = "UTC"
    asn: int = Field(ge=1, le=4294967294)
    lan_prefixes: list[str] = []
    overlay_host: int = Field(ge=1, le=254)

    @field_validator("lan_prefixes")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        return [str(ipaddress.ip_network(p)) for p in v]


@router.post("/sites", status_code=201)
def upsert_site(body: SiteIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        sid = inventory.upsert_site(conn, body.customer_id, user.actor, **body.model_dump(exclude={"customer_id"}))
    return {"id": sid}


class LinkIn(BaseModel):
    carrier: str = Field(min_length=1, max_length=120)
    path: Literal["carrier-a", "carrier-b", "sat"]
    underlay_type: Literal["fibre", "broadband", "lte", "leo", "geo"]
    underlay_interface: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,15}$")
    underlay_ip: str | None = None
    commit_mbps: float = 0
    cost_per_mbps: float = 0
    burst_price: float = 0


@router.post("/sites/{site_id}/links", status_code=201)
def upsert_link(site_id: str, body: LinkIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        site = conn.execute("SELECT customer_id FROM sites WHERE id = %s", (site_id,)).fetchone()
        if site is None:
            raise HTTPException(404, "Site not found.")
        carrier_id = inventory.ensure_carrier(conn, body.carrier, user.actor)
        lid = inventory.upsert_link(
            conn,
            site["customer_id"],
            site_id,
            user.actor,
            carrier_id=carrier_id,
            **body.model_dump(exclude={"carrier"}),
        )
    return {"id": lid}


class TokenIn(BaseModel):
    site_id: str
    ttl_hours: int = Field(default=24, ge=1, le=24 * 30)


class TokenOut(BaseModel):
    token: str
    expires_at: dt.datetime
    ca_fingerprint: str
    agent_url: str


@router.post("/enrolment-tokens", status_code=201)
def issue_token(body: TokenIn, user: AdminDep, request: Request) -> TokenOut:
    """The token is shown once. Give it to the installer with the CA fingerprint."""
    with db.tx() as conn:
        try:
            token, expires = inventory.issue_token(conn, body.site_id, user.actor, body.ttl_hours)
        except LookupError:
            raise HTTPException(404, "Site not found.") from None
    return TokenOut(
        token=token,
        expires_at=expires,
        ca_fingerprint=request.app.state.ca.fingerprint,
        agent_url=request.app.state.settings.agent_url,
    )


@router.get("/nodes")
def list_nodes(user: ViewerDep) -> list[dict]:
    scope = customer_scope(user)
    with db.tx() as conn:
        return conn.execute(
            """SELECT n.id, n.name, s.kind AS role, n.customer_id, n.enrolled_at, n.last_seen,
                      n.applied_version, n.apply_ok, n.apply_error, n.agent_version,
                      (SELECT max(version) FROM desired_states d WHERE d.node_id = n.id) AS desired_version
               FROM nodes n JOIN sites s ON s.id = n.site_id
               WHERE %(c)s::uuid IS NULL OR n.customer_id = %(c)s
               ORDER BY n.name""",
            {"c": scope},
        ).fetchall()


@router.get("/audit")
def list_audit(user: AdminDep, limit: int = 100) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            "SELECT at, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT %s",
            (min(limit, 1000),),
        ).fetchall()
