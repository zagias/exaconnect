"""Organisation set-up (ADR 0043): company details, the shared list of
locations, connecting a location to the network, the set-up checklist, and
ExaCarib's view of every organisation with a one-step "new organisation".

Any member reads; owners and admins (and ExaCarib admins) change. Every write
is audited."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit, db, inventory, organisation
from ..identity import orgs
from .deps import PRODUCTS, AdminDep, UserDep, check_customer, require_org_manager

router = APIRouter(tags=["organisations"])


def _uuid(v: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(v)))
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from None


def _run(fn, *args):
    try:
        return fn(*args)
    except organisation.OrgSetupError as e:
        raise HTTPException(e.status, str(e)) from e


# ---- company details -----------------------------------------------------------------------


class CompanyIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=2)
    timezone: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=40)
    website: str | None = Field(default=None, max_length=200)


@router.get("/orgs/{customer_id}/company")
def get_company(customer_id: str, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return _run(organisation.company, conn, _uuid(customer_id))


@router.patch("/orgs/{customer_id}/company")
def update_company(customer_id: str, body: CompanyIn, user: UserDep) -> dict:
    require_org_manager(user, customer_id)
    if body.timezone:
        _run(organisation._clean, {"timezone": body.timezone})
    with db.tx() as conn:
        return _run(organisation.save_company, conn, _uuid(customer_id), user.actor, body.model_dump(exclude_none=True))


# ---- locations -----------------------------------------------------------------------------


class LocationIn(BaseModel):
    name: str = Field(default="", max_length=120)
    address_line1: str = Field(default="", max_length=200)
    address_line2: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=120)
    island: str = Field(default="", max_length=120)
    country: str = Field(default="", max_length=2)
    postcode: str = Field(default="", max_length=20)
    timezone: str = Field(default="America/Port_of_Spain", max_length=64)
    latitude: float | None = None
    longitude: float | None = None


@router.get("/orgs/{customer_id}/locations")
def list_locations(customer_id: str, user: UserDep) -> dict:
    check_customer(user, customer_id)
    cid = _uuid(customer_id)
    with db.tx() as conn:
        products = _run(organisation._products, conn, cid)
        return {
            "locations": organisation.locations(conn, cid),
            "products": products,
            "hub_ready": organisation.has_hub(conn, cid),
        }


@router.post("/orgs/{customer_id}/locations", status_code=201)
def create_location(customer_id: str, body: LocationIn, user: UserDep) -> dict:
    require_org_manager(user, customer_id)
    with db.tx() as conn:
        return _run(organisation.save_location, conn, _uuid(customer_id), user.actor, body.model_dump())


@router.put("/orgs/{customer_id}/locations/{location_id}")
def update_location(customer_id: str, location_id: str, body: LocationIn, user: UserDep) -> dict:
    require_org_manager(user, customer_id)
    with db.tx() as conn:
        return _run(
            organisation.save_location, conn, _uuid(customer_id), user.actor, body.model_dump(), _uuid(location_id)
        )


@router.delete("/orgs/{customer_id}/locations/{location_id}", status_code=204)
def delete_location(customer_id: str, location_id: str, user: UserDep) -> None:
    require_org_manager(user, customer_id)
    with db.tx() as conn:
        _run(organisation.delete_location, conn, _uuid(customer_id), user.actor, _uuid(location_id))


class LinkIn(BaseModel):
    carrier: str = Field(min_length=1, max_length=120)
    underlay_type: Literal["fibre", "broadband", "lte", "leo", "geo"] = "fibre"
    commit_mbps: float = Field(default=0, ge=0, le=100_000)


class ConnectIn(BaseModel):
    links: list[LinkIn] = Field(min_length=1, max_length=3)
    lan_prefixes: list[str] = Field(default_factory=list, max_length=16)


def _install(request: Request, site: str, token: str, expires) -> dict:
    s = request.app.state.settings
    return {
        "site": site,
        "token": token,
        "expires_at": expires,
        "ca_fingerprint": request.app.state.ca.fingerprint,
        "agent_url": s.agent_url,
        "public_agent_url": s.agent_public_url,
    }


@router.post("/orgs/{customer_id}/locations/{location_id}/connect", status_code=201)
def connect_location(customer_id: str, location_id: str, body: ConnectIn, user: UserDep, request: Request) -> dict:
    """Make the Connect site for a location and return a one-time install code for its box."""
    require_org_manager(user, customer_id)
    cid = _uuid(customer_id)
    with db.tx() as conn:
        site = _run(
            organisation.connect_location,
            conn,
            cid,
            user.actor,
            _uuid(location_id),
            [lk.model_dump() for lk in body.links],
            body.lan_prefixes,
        )
        token, expires = inventory.issue_token(conn, site["site_id"], user.actor, ttl_hours=72)
        hub = organisation.has_hub(conn, cid)
    return {**site, "hub_ready": hub, "install": _install(request, site["name"], token, expires)}


@router.post("/orgs/{customer_id}/sites/{site_id}/install-code", status_code=201)
def install_code(customer_id: str, site_id: str, user: UserDep, request: Request) -> dict:
    """A new one-time install code for a location's box (the old one stops working when this is used)."""
    require_org_manager(user, customer_id)
    cid = _uuid(customer_id)
    with db.tx() as conn:
        site = conn.execute(
            "SELECT id, name FROM sites WHERE id = %s AND customer_id = %s AND kind = 'site'", (_uuid(site_id), cid)
        ).fetchone()
        if site is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found.")
        token, expires = inventory.issue_token(conn, site["id"], user.actor, ttl_hours=72)
    return _install(request, site["name"], token, expires)


# ---- the set-up checklist ----------------------------------------------------------------


@router.get("/orgs/{customer_id}/setup")
def setup(customer_id: str, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return _run(organisation.checklist, conn, _uuid(customer_id))


class FinishIn(BaseModel):
    done: bool = True


@router.post("/orgs/{customer_id}/setup")
def finish_setup(customer_id: str, body: FinishIn, user: UserDep) -> dict:
    """Hide the checklist before every step is done (or bring it back)."""
    require_org_manager(user, customer_id)
    cid = _uuid(customer_id)
    with db.tx() as conn:
        conn.execute("UPDATE customers SET setup_done_at = CASE WHEN %s THEN now() END WHERE id = %s", (body.done, cid))
        audit.record(conn, user.actor, "org.setup." + ("finish" if body.done else "reopen"), "", cid)
        return _run(organisation.checklist, conn, cid)


# ---- what each person has in the apps, for the People list ----------------------------------


@router.get("/orgs/{customer_id}/people-extras")
def people_extras(customer_id: str, user: UserDep) -> dict:
    """Each member's phone extension and Jibsy seat, keyed by user id, and the phone
    extensions and Jibsy seats held by anyone who is not a member: ExaCarib staff, or an
    extension with no sign-in. Without the second list those were on no page at all."""
    check_customer(user, customer_id)
    cid = _uuid(customer_id)
    by_user: dict[str, dict] = {}
    others: dict[str, dict] = {}
    with db.tx() as conn:
        members = {
            str(r["user_id"])
            for r in conn.execute("SELECT user_id FROM org_memberships WHERE customer_id = %s", (cid,)).fetchall()
        }

        def other(r: dict) -> dict:
            key = str(r["user_id"]) if r["user_id"] else f"x:{r['name']}:{r.get('extension', '')}"
            if key not in others:
                kind = "phone_only" if r["user_id"] is None else ("staff" if r["role"] == "admin" else "not_member")
                others[key] = {"name": r["name"], "email": r["email"] or "", "kind": kind}
            return others[key]

        if organisation._has_table(conn, "voice_users"):
            for r in conn.execute(
                """SELECT v.user_id, v.extension, coalesce(nullif(u.display_name, ''), v.name) AS name,
                          coalesce(u.email, v.email) AS email, u.role
                   FROM voice_users v LEFT JOIN users u ON u.id = v.user_id
                   WHERE v.customer_id = %s AND v.status = 'active' ORDER BY v.extension""",
                (cid,),
            ).fetchall():
                if r["user_id"] and str(r["user_id"]) in members:
                    by_user.setdefault(str(r["user_id"]), {})["extension"] = r["extension"]
                else:
                    other(r)["extension"] = r["extension"]
        if organisation._has_table(conn, "commai_members"):
            for r in conn.execute(
                """SELECT m.user_id, m.seat, coalesce(nullif(u.display_name, ''), u.email) AS name, u.email, u.role
                   FROM commai_members m JOIN users u ON u.id = m.user_id WHERE m.customer_id = %s""",
                (cid,),
            ).fetchall():
                if str(r["user_id"]) in members:
                    by_user.setdefault(str(r["user_id"]), {})["seat"] = r["seat"]
                else:
                    other(r)["seat"] = r["seat"]
    return {"by_user": by_user, "others": sorted(others.values(), key=lambda o: (o.get("extension") or "~", o["name"]))}


# ---- ExaCarib: every organisation, and a new one in one step ------------------------------


@router.get("/admin/organisations")
def all_organisations(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.products, c.created_at, c.example,
                      (SELECT string_agg(u.email, ', ' ORDER BY u.email) FROM org_memberships m
                         JOIN users u ON u.id = m.user_id WHERE m.customer_id = c.id AND m.role = 'owner') AS owners,
                      (SELECT count(*) FROM org_memberships m WHERE m.customer_id = c.id) AS people,
                      (SELECT count(*) FROM org_invites i WHERE i.customer_id = c.id AND i.accepted_at IS NULL
                         AND i.revoked_at IS NULL AND i.expires_at > now()) AS invites,
                      (SELECT count(*) FROM sites s WHERE s.customer_id = c.id AND s.kind = 'site') AS sites,
                      EXISTS (SELECT 1 FROM sites s WHERE s.customer_id = c.id AND s.kind = 'pop') AS hub
               FROM customers c ORDER BY lower(c.name)"""
        ).fetchall()
        for r in rows:
            chk = organisation.checklist(conn, r["id"])
            r["setup"] = {"done": chk["done"], "total": chk["total"], "complete": chk["complete"]}
            r["locations"] = conn.execute(
                "SELECT count(*) AS n FROM org_locations WHERE customer_id = %s", (r["id"],)
            ).fetchone()["n"]
    return rows


class NewOrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    products: list[str] = Field(min_length=1, max_length=4)
    owner_email: str = Field(min_length=3, max_length=255)
    country: str = Field(default="", max_length=2)
    timezone: str = Field(default="", max_length=64)


@router.post("/admin/organisations", status_code=201)
def new_organisation(body: NewOrgIn, user: AdminDep, request: Request) -> dict:
    """Create an organisation, choose its plans and invite the person who will
    run it. They become its owner when they accept."""
    wanted = sorted(set(body.products))
    if any(p not in PRODUCTS for p in wanted):
        raise HTTPException(422, f"Plans are {', '.join(PRODUCTS)}.")
    name = body.name.strip()
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE lower(name) = lower(%s)", (name,)).fetchone():
            raise HTTPException(409, "There is already an organisation with that name.")
        cid = inventory.ensure_customer(conn, name, user.actor)
        conn.execute(
            "UPDATE customers SET products = %s, country = %s, timezone = %s WHERE id = %s",
            (wanted, body.country.upper()[:2], body.timezone.strip(), cid),
        )
        audit.record(conn, user.actor, "customer.products", ",".join(wanted), cid, {"to": wanted})
        try:
            row, token = orgs.invite(conn, cid, body.owner_email, "admin", user.actor)
        except orgs.OrgError as e:
            raise HTTPException(e.status, str(e)) from e
        audit.record(conn, user.actor, "org.invite.create", row["email"], cid, {"role": "owner"})
    path = f"/invite/{token}"
    base = request.app.state.settings.public_url
    return {
        "id": str(cid),
        "name": name,
        "products": wanted,
        "invite": {"email": row["email"], "url": f"{base}{path}" if base else path, "expires_at": row["expires_at"]},
    }
