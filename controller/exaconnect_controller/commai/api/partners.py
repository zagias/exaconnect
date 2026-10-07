"""Partners and white-label API (ADR 0031).

ExaCarib admins create partners and add their people. A partner asks to manage
a business; the business accepts, chooses the scopes, and can revoke. Partner
people switch into a linked business through a delegate account. Brands and
custom domains belong to a partner or (optionally) one business.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import User, UserDep
from ...identity import sessions
from .. import access, branding, partners

router = APIRouter(tags=["commai: partners"])
public = APIRouter(tags=["commai: partners"])

NOT_AVAILABLE = "Not available for this account."


@contextmanager
def _errors():
    try:
        yield
    except partners.PartnerError as e:
        raise HTTPException(e.code, str(e)) from e
    except branding.BrandError as e:
        raise HTTPException(e.code, str(e)) from e


def _partner(conn, user: User, partner_id: str, admin: bool = False) -> dict:
    """The partner, if this account may act for it (ExaCarib admin, or one of its people)."""
    if user.role == "admin":
        row = conn.execute(
            "SELECT *, 'admin' AS member_role FROM commai_partners WHERE id = %s", (partner_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Partner not found.")
        return row
    m = partners.membership(conn, user.id) if user.role == "customer" else None
    if m is None or str(m["id"]) != str(partner_id):
        raise HTTPException(403, NOT_AVAILABLE)
    if user.scopes is not None and "commai:admin" not in user.scopes:
        raise HTTPException(403, "Partner work needs the commai:admin scope.")
    if admin and m["member_role"] != "admin":
        raise HTTPException(403, "Only the partner's admins can do this.")
    return m


def _no_delegate(conn, user: User) -> None:
    if partners.is_delegate(conn, user.id):
        raise HTTPException(403, "Only the business's own people can do this, not a partner acting for it.")


def _person(user: User) -> None:
    if user.via == "key":
        raise HTTPException(403, "A person must do this in the portal, not an API key.")


# ---- partners (ExaCarib admins) -----------------------------------------------------------------


class PartnerIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    kind: str = Field(pattern="^(reseller|msp)$")
    markup_pct: float = Field(default=0, ge=0, le=500)
    contact_email: str = Field(default="", max_length=255)


class PartnerPatch(BaseModel):
    markup_pct: float | None = Field(default=None, ge=0, le=500)
    contact_email: str | None = Field(default=None, max_length=255)
    active: bool | None = None


@router.post("/partners", status_code=201)
def create_partner(body: PartnerIn, user: UserDep) -> dict:
    if user.role != "admin":
        raise HTTPException(403, "Admins only.")
    with db.tx() as conn, _errors():
        row = partners.create(
            conn, body.name.strip(), body.kind, body.markup_pct, body.contact_email.strip(), user.actor
        )
        audit.record(conn, user.actor, "commai.partner.create", str(row["id"]), None, body.model_dump())
    return row


@router.get("/partners")
def list_partners(user: UserDep) -> list[dict]:
    with db.tx() as conn:
        if user.role == "admin":
            return conn.execute(
                """SELECT p.*, (SELECT count(*) FROM commai_partner_links l WHERE l.partner_id = p.id
                                AND l.status = 'active') AS customers
                   FROM commai_partners p ORDER BY p.name"""
            ).fetchall()
        m = partners.membership(conn, user.id) if user.role == "customer" else None
        return [m] if m else []


@router.patch("/partners/{partner_id}")
def update_partner(partner_id: str, body: PartnerPatch, user: UserDep) -> dict:
    if user.role != "admin":
        raise HTTPException(403, "Admins only.")
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE commai_partners SET markup_pct = coalesce(%s, markup_pct),
                 contact_email = coalesce(%s, contact_email), active = coalesce(%s, active)
               WHERE id = %s RETURNING *""",
            (body.markup_pct, body.contact_email, body.active, partner_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Partner not found.")
        audit.record(conn, user.actor, "commai.partner.update", partner_id, None, body.model_dump(exclude_none=True))
    return row


class MemberIn(BaseModel):
    email: str = Field(max_length=255)
    role: str = Field(default="member", pattern="^(admin|member)$")


@router.get("/partners/{partner_id}/members")
def list_members(partner_id: str, user: UserDep) -> list[dict]:
    with db.tx() as conn:
        _partner(conn, user, partner_id)
        return conn.execute(
            """SELECT u.id AS user_id, u.email, u.display_name, m.role, m.created_at FROM commai_partner_members m
               JOIN users u ON u.id = m.user_id WHERE m.partner_id = %s ORDER BY u.email""",
            (partner_id,),
        ).fetchall()


@router.post("/partners/{partner_id}/members", status_code=201)
def add_member(partner_id: str, body: MemberIn, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _partner(conn, user, partner_id, admin=True)
        row = partners.add_member(conn, partner_id, body.email, body.role)
        audit.record(conn, user.actor, "commai.partner.member.add", body.email, None,
                     {"partner_id": partner_id, "role": body.role})  # fmt: skip
    return row


@router.delete("/partners/{partner_id}/members/{user_id}", status_code=204)
def remove_member(partner_id: str, user_id: str, user: UserDep) -> None:
    with db.tx() as conn:
        _partner(conn, user, partner_id, admin=True)
        row = conn.execute(
            "DELETE FROM commai_partner_members WHERE partner_id = %s AND user_id = %s RETURNING user_id",
            (partner_id, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Not a member.")
        # Their delegate accounts stop working too.
        for d in conn.execute(
            "SELECT delegate_user_id FROM commai_partner_delegates WHERE partner_user_id = %s", (user_id,)
        ).fetchall():
            conn.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (d["delegate_user_id"],))
            sessions.end_all(conn, d["delegate_user_id"], revoke_keys=True)
        audit.record(conn, user.actor, "commai.partner.member.remove", user_id, None, {"partner_id": partner_id})


# ---- who am I --------------------------------------------------------------------------------------


@router.get("/partners/me")
def me(user: UserDep) -> dict:
    """The partner this account belongs to, or the partner it is acting for (a delegate)."""
    with db.tx() as conn:
        out: dict[str, Any] = {"partner": None, "role": None, "acting_for": None}
        if user.role != "customer":
            return out
        m = partners.membership(conn, user.id)
        if m:
            out["partner"] = {"id": str(m["id"]), "name": m["name"], "kind": m["kind"]}
            out["role"] = m["member_role"]
        d = partners.delegation(conn, user.id)
        if d:
            cust = conn.execute("SELECT name FROM customers WHERE id = %s", (d["customer_id"],)).fetchone()
            out["acting_for"] = {
                "customer_id": str(d["customer_id"]),
                "customer_name": cust["name"] if cust else "",
                "partner_id": str(d["partner_id"]),
                "partner_name": d["partner_name"],
                "scopes": list(user.scopes or []),
            }
        return out


# ---- links: the partner's side --------------------------------------------------------------------


class LinkIn(BaseModel):
    customer_id: str = Field(max_length=64)
    scopes: list[str] = Field(min_length=1, max_length=10)
    note: str = Field(default="", max_length=500)


class LinkPatch(BaseModel):
    markup_pct: float | None = Field(default=None, ge=0, le=500)
    white_label: bool | None = None


@router.get("/partners/{partner_id}/customers")
def partner_customers(partner_id: str, user: UserDep, month: str | None = Query(None, max_length=7)) -> list[dict]:
    """The consolidated view: each linked business with its health and usage."""
    with db.tx() as conn, _errors():
        p = _partner(conn, user, partner_id)
        return partners.customers_view(conn, p, month)


@router.post("/partners/{partner_id}/links", status_code=201)
def request_link(partner_id: str, body: LinkIn, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _partner(conn, user, partner_id, admin=True)
        try:
            import uuid

            uuid.UUID(body.customer_id)
        except ValueError as e:
            raise HTTPException(404, "No business with that id.") from e
        row = partners.request_link(conn, partner_id, body.customer_id, body.scopes, body.note, user.actor)
        audit.record(conn, user.actor, "commai.partner.link.request", str(row["id"]), body.customer_id,
                     {"partner_id": partner_id, "scopes": row["requested_scopes"]})  # fmt: skip
    return row


@router.patch("/partners/{partner_id}/links/{link_id}")
def update_link(partner_id: str, link_id: str, body: LinkPatch, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _partner(conn, user, partner_id, admin=True)
        link = partners.get_link(conn, link_id, partner_id=partner_id)
        conn.execute(
            """UPDATE commai_partner_links SET markup_pct = CASE WHEN %s THEN %s ELSE markup_pct END,
                 white_label = coalesce(%s, white_label) WHERE id = %s""",
            ("markup_pct" in body.model_fields_set, body.markup_pct, body.white_label, link_id),
        )
        audit.record(conn, user.actor, "commai.partner.link.update", link_id, link["customer_id"],
                     body.model_dump(exclude_unset=True))  # fmt: skip
        return partners.get_link(conn, link_id)


@router.delete("/partners/{partner_id}/links/{link_id}")
def end_link(partner_id: str, link_id: str, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _partner(conn, user, partner_id, admin=True)
        row = partners.revoke(conn, link_id, user.actor, partner_id=partner_id)
        audit.record(conn, user.actor, "commai.partner.link.end", link_id, row["customer_id"], {"by": "partner"})
    return row


# ---- switching -------------------------------------------------------------------------------------


class SwitchIn(BaseModel):
    customer_id: str = Field(max_length=64)


def _session_out(request: Request, response: Response, token: str, expires, extra: dict) -> dict:
    sessions.set_cookie(response, request, token, expires)
    return {"token": token, "expires_at": expires, **extra}


@router.post("/partners/switch")
def switch(body: SwitchIn, request: Request, response: Response, user: UserDep) -> dict:
    """Act for a linked business, with only the scopes it granted. Returns a new
    session (and sets the portal cookie)."""
    _person(user)
    with db.tx() as conn, _errors():
        m = partners.membership(conn, user.id) if user.role == "customer" else None
        if m is None:
            raise HTTPException(403, "Only a partner's people can switch between businesses.")
        delegate = partners.delegate_for(conn, m, user, body.customer_id)
        hours = min(8, request.app.state.settings.session_hours)
        token, expires = partners.start_session(conn, delegate["id"], hours, "partner")
        name = conn.execute("SELECT name FROM customers WHERE id = %s", (body.customer_id,)).fetchone()["name"]
        detail = {"partner_id": str(m["id"]), "delegate": delegate["email"], "scopes": delegate["access_scopes"]}
        audit.record(conn, user.actor, "commai.partner.switch", body.customer_id, body.customer_id, detail)
    return _session_out(
        request, response, token, expires,
        {"customer_id": body.customer_id, "customer_name": name, "scopes": list(delegate["access_scopes"])},
    )  # fmt: skip


@router.post("/partners/switch-back")
def switch_back(request: Request, response: Response, user: UserDep) -> dict:
    """From a business back to the partner account."""
    _person(user)
    with db.tx() as conn:
        d = partners.delegation(conn, user.id)
        if d is None:
            raise HTTPException(409, "You are not acting for a business.")
        real = conn.execute(
            """SELECT u.* FROM users u JOIN commai_partner_members m ON m.user_id = u.id
               WHERE u.id = %s AND u.disabled_at IS NULL""",
            (d["partner_user_id"],),
        ).fetchone()
        if real is None:
            raise HTTPException(403, "Your partner account is no longer active.")
        tok, _ = sessions.request_token(request)
        if tok:
            from ...security import token_hash

            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(tok),))
        token, expires = partners.start_session(conn, real["id"], request.app.state.settings.session_hours, "partner")
        audit.record(conn, f"user:{real['email']}", "commai.partner.switch_back", str(d["customer_id"]),
                     d["customer_id"])  # fmt: skip
    return _session_out(request, response, token, expires, {"email": real["email"]})


# ---- statements (recorded for billing; no payments) -----------------------------------------------


class StatementIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


@router.get("/partners/{partner_id}/statements")
def list_statements(partner_id: str, user: UserDep, month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$")):
    with db.tx() as conn:
        _partner(conn, user, partner_id)
        return conn.execute(
            """SELECT s.*, c.name AS customer_name FROM commai_partner_statements s
               JOIN customers c ON c.id = s.customer_id WHERE s.partner_id = %s
                 AND (%s::text IS NULL OR to_char(s.period, 'YYYY-MM') = %s) ORDER BY s.period DESC, c.name""",
            (partner_id, month, month),
        ).fetchall()


@router.post("/partners/{partner_id}/statements", status_code=201)
def record_statements(partner_id: str, body: StatementIn, user: UserDep) -> list[dict]:
    with db.tx() as conn, _errors():
        p = _partner(conn, user, partner_id, admin=True)
        rows = partners.record_statements(conn, p, body.month, user.actor)
        audit.record(conn, user.actor, "commai.partner.statements", partner_id, None,
                     {"month": body.month, "customers": len(rows)})  # fmt: skip
    return rows


# ---- links: the business's side ----------------------------------------------------------------------


class DecideIn(BaseModel):
    scopes: list[str] | None = Field(default=None, max_length=10)


class ScopesIn(BaseModel):
    scopes: list[str] = Field(min_length=1, max_length=10)


@router.get("/customers/{customer_id}/partner-links")
def customer_links(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return conn.execute(
            f"""SELECT {partners.LINK_COLUMNS}, p.name AS partner_name, p.kind AS partner_kind
                FROM commai_partner_links l JOIN commai_partners p ON p.id = l.partner_id
                WHERE l.customer_id = %s ORDER BY l.requested_at DESC""",
            (customer_id,),
        ).fetchall()


def _business_admin(conn, user: User, customer_id: str) -> None:
    access.check(user, customer_id, "commai:admin")
    _no_delegate(conn, user)


@router.post("/customers/{customer_id}/partner-links/{link_id}/accept")
def accept_link(customer_id: str, link_id: str, body: DecideIn, user: UserDep) -> dict:
    _person(user)
    with db.tx() as conn, _errors():
        _business_admin(conn, user, customer_id)
        row = partners.decide(conn, customer_id, link_id, True, body.scopes, user.actor)
        audit.record(conn, user.actor, "commai.partner.link.accept", link_id, customer_id, {"scopes": row["scopes"]})
    return row


@router.post("/customers/{customer_id}/partner-links/{link_id}/decline")
def decline_link(customer_id: str, link_id: str, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _business_admin(conn, user, customer_id)
        row = partners.decide(conn, customer_id, link_id, False, None, user.actor)
        audit.record(conn, user.actor, "commai.partner.link.decline", link_id, customer_id)
    return row


@router.put("/customers/{customer_id}/partner-links/{link_id}/scopes")
def change_link_scopes(customer_id: str, link_id: str, body: ScopesIn, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _business_admin(conn, user, customer_id)
        row = partners.change_scopes(conn, customer_id, link_id, body.scopes)
        audit.record(conn, user.actor, "commai.partner.link.scopes", link_id, customer_id, {"scopes": row["scopes"]})
    return row


@router.delete("/customers/{customer_id}/partner-links/{link_id}")
def revoke_link(customer_id: str, link_id: str, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _business_admin(conn, user, customer_id)
        row = partners.revoke(conn, link_id, user.actor, customer_id=customer_id)
        audit.record(conn, user.actor, "commai.partner.link.revoke", link_id, customer_id, {"by": "business"})
    return row


# =====================================================================================================
# White-label
# =====================================================================================================


class BrandIn(BaseModel):
    product_name: str = Field(max_length=60)
    colour: str = Field(max_length=7)
    ink: str | None = Field(default=None, max_length=7)
    support_email: str = Field(default="", max_length=255)


def _customer_brand_access(conn, user: User, customer_id: str) -> None:
    """ExaCarib admins; or the business (or its partner acting for it) once a partner manages it."""
    if user.role == "admin":
        return
    access.check(user, customer_id, "commai:admin")
    if not conn.execute(
        "SELECT 1 FROM commai_partner_links WHERE customer_id = %s AND status = 'active'", (customer_id,)
    ).fetchone():
        raise HTTPException(403, "Own branding comes with a partner. Ask your partner or ExaCarib.")


def _brand_access(conn, user: User, brand_id: str) -> dict:
    b = conn.execute(f"SELECT {branding.BRAND_COLUMNS} FROM commai_whitelabel WHERE id = %s", (brand_id,)).fetchone()
    if b is None:
        raise HTTPException(404, "Brand not found.")
    if b["partner_id"]:
        _partner(conn, user, str(b["partner_id"]), admin=True)
    else:
        _customer_brand_access(conn, user, str(b["customer_id"]))
    return b


@router.get("/branding/current")
def current_brand(user: UserDep) -> dict | None:
    """The look the portal applies for this account; null means ExaCarib's own."""
    with db.tx() as conn:
        return branding.public_view(branding.for_user(conn, user))


@public.get("/branding/host")
def host_brand(request: Request) -> JSONResponse:
    """The brand of a verified custom portal domain (for the sign-in page)."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    with db.tx() as conn:
        b = branding.public_view(branding.for_host(conn, host))
    return JSONResponse(b, headers={"Cache-Control": "public, max-age=300"})


@public.get("/branding/logo/{brand_id}", include_in_schema=False)
def brand_logo(brand_id: str) -> Response:
    try:
        import uuid

        uuid.UUID(brand_id)
    except ValueError:
        return Response(status_code=404)
    with db.tx() as conn:
        row = conn.execute("SELECT logo, logo_type FROM commai_whitelabel WHERE id = %s", (brand_id,)).fetchone()
    if row is None or not row["logo"]:
        return Response(status_code=404)
    return Response(
        bytes(row["logo"]),
        media_type=row["logo_type"],
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/partners/{partner_id}/branding")
def get_partner_brand(partner_id: str, user: UserDep) -> dict | None:
    with db.tx() as conn:
        _partner(conn, user, partner_id)
        b = branding.get(conn, partner_id=partner_id)
        return {**b, "logo_url": branding.public_view(b)["logo_url"]} if b else None


@router.put("/partners/{partner_id}/branding")
def put_partner_brand(partner_id: str, body: BrandIn, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _partner(conn, user, partner_id, admin=True)
        row = branding.save(conn, partner_id=partner_id, **body.model_dump(), actor=user.actor)
        audit.record(conn, user.actor, "commai.brand.save", str(row["id"]), None,
                     {"partner_id": partner_id, **body.model_dump()})  # fmt: skip
    return row


@router.get("/customers/{customer_id}/branding")
def get_customer_brand(customer_id: str, user: UserDep) -> dict:
    """The business's own brand (if any) and the brand its people and widget actually get."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        own = branding.get(conn, customer_id=customer_id)
        return {
            "own": {**own, "logo_url": branding.public_view(own)["logo_url"]} if own else None,
            "effective": branding.public_view(branding.for_customer(conn, customer_id)),
            "partner_managed": conn.execute(
                "SELECT 1 FROM commai_partner_links WHERE customer_id = %s AND status = 'active'", (customer_id,)
            ).fetchone()
            is not None,
        }


@router.put("/customers/{customer_id}/branding")
def put_customer_brand(customer_id: str, body: BrandIn, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        _customer_brand_access(conn, user, customer_id)
        row = branding.save(conn, customer_id=customer_id, **body.model_dump(), actor=user.actor)
        audit.record(conn, user.actor, "commai.brand.save", str(row["id"]), customer_id, body.model_dump())
    return row


@router.delete("/customers/{customer_id}/branding", status_code=204)
def delete_customer_brand(customer_id: str, user: UserDep) -> None:
    with db.tx() as conn:
        _customer_brand_access(conn, user, customer_id)
        conn.execute("DELETE FROM commai_whitelabel WHERE customer_id = %s", (customer_id,))
        audit.record(conn, user.actor, "commai.brand.delete", customer_id, customer_id)


@router.put("/brands/{brand_id}/logo")
async def put_logo(brand_id: str, request: Request, user: UserDep) -> dict:
    """Upload the logo as the raw request body with its Content-Type (PNG, JPEG or WebP, 256 KB at most)."""
    data = await request.body()
    if len(data) > branding.MAX_LOGO_BYTES * 2:
        raise HTTPException(413, "The logo is too large.")
    from starlette.concurrency import run_in_threadpool

    def run() -> dict:
        with db.tx() as conn, _errors():
            _brand_access(conn, user, brand_id)
            row = branding.set_logo(conn, brand_id, data, request.headers.get("content-type", ""), user.actor)
            audit.record(conn, user.actor, "commai.brand.logo", brand_id, row["customer_id"],
                         {"type": row["logo_type"], "bytes": len(data)})  # fmt: skip
            return {**row, "logo_url": branding.public_view(row)["logo_url"]}

    return await run_in_threadpool(run)


# ---- custom domains -------------------------------------------------------------------------------------


class DomainIn(BaseModel):
    purpose: str = Field(pattern="^(portal|widget)$")
    domain: str = Field(max_length=253)


@router.get("/brands/{brand_id}/domains")
def list_domains(brand_id: str, user: UserDep) -> list[dict]:
    with db.tx() as conn:
        _brand_access(conn, user, brand_id)
        rows = conn.execute(
            "SELECT * FROM commai_brand_domains WHERE brand_id = %s ORDER BY created_at", (brand_id,)
        ).fetchall()
    return [branding.domain_view(r) for r in rows]


@router.post("/brands/{brand_id}/domains", status_code=201)
def add_domain(brand_id: str, body: DomainIn, user: UserDep) -> dict:
    """Register a domain. Publish the TXT record shown, then verify."""
    with db.tx() as conn, _errors():
        b = _brand_access(conn, user, brand_id)
        row = branding.add_domain(conn, brand_id, body.purpose, body.domain, user.actor)
        audit.record(conn, user.actor, "commai.brand.domain.add", row["domain"], b["customer_id"],
                     {"purpose": body.purpose})  # fmt: skip
    return branding.domain_view(row)


@router.post("/brands/{brand_id}/domains/{domain_id}/verify")
def verify_domain(brand_id: str, domain_id: str, user: UserDep) -> dict:
    with db.tx() as conn, _errors():
        b = _brand_access(conn, user, brand_id)
        if not conn.execute(
            "SELECT 1 FROM commai_brand_domains WHERE id = %s AND brand_id = %s", (domain_id, brand_id)
        ).fetchone():
            raise HTTPException(404, "Domain not found.")
        row = branding.verify(conn, domain_id)
        audit.record(conn, user.actor, "commai.brand.domain.verify", row["domain"], b["customer_id"],
                     {"status": row["status"], "resolver": branding.resolver().name})  # fmt: skip
    return branding.domain_view(row)


@router.delete("/brands/{brand_id}/domains/{domain_id}", status_code=204)
def delete_domain(brand_id: str, domain_id: str, user: UserDep) -> None:
    with db.tx() as conn:
        b = _brand_access(conn, user, brand_id)
        row = conn.execute(
            "DELETE FROM commai_brand_domains WHERE id = %s AND brand_id = %s RETURNING domain", (domain_id, brand_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Domain not found.")
        audit.record(conn, user.actor, "commai.brand.domain.delete", row["domain"], b["customer_id"])


@public.get("/whitelabel/tls-ask", include_in_schema=False)
def tls_ask(domain: str = "") -> Response:
    """Caddy's on-demand TLS check: 200 only for a verified custom domain."""
    with db.tx() as conn:
        ok = branding.tls_allowed(conn, domain)
    return Response(status_code=200 if ok else 404)


class SimDnsIn(BaseModel):
    name: str = Field(max_length=300)
    value: str = Field(max_length=500)


@router.put("/whitelabel/simulated-dns", status_code=204)
def publish_sim_dns(body: SimDnsIn, user: UserDep) -> None:
    """Publish a TXT record in the simulated resolver (demo and tests). ExaCarib admins only."""
    if user.role != "admin":
        raise HTTPException(403, "Admins only.")
    if branding.resolver().name != "simulated":
        raise HTTPException(409, "Real DNS is in use; publish the record with the domain's DNS host.")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_sim_dns (name, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (body.name.strip().lower().rstrip("."), body.value.strip()),
        )
        audit.record(conn, user.actor, "commai.sim_dns.publish", body.name, None, {"value": body.value[:100]})
