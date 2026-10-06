"""Enterprise single sign-on and directory set-up for a business (ADR 0017).

The business admin adds its identity provider (SAML 2.0 metadata or an OpenID
Connect discovery URL), tests it with a sign-in that creates no session, then
switches it on. Email-domain claims route nobody until an ExaCarib admin
approves them. SCIM tokens and directory-group mappings live here too."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit, db
from ..commai import access
from ..identity import keycloak, oidc, scim, sso
from ..security import new_token, token_hash
from .auth import begin
from .deps import AdminDep, User, UserDep

router = APIRouter(tags=["sign-in"])


def _biz_admin(user: User, customer_id: str) -> None:
    access.check(user, customer_id, "commai:admin")


def _gateway(request: Request) -> keycloak.KeycloakAdmin:
    return keycloak.admin_for(request.app.state.settings)


def _gateway_call(fn, *args) -> None:
    try:
        fn(*args)
    except keycloak.GatewayError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e


# ---- email-first discovery (public) -------------------------------------------


class DiscoverIn(BaseModel):
    email: str = Field(max_length=255)


@router.post("/auth/sso/discover")
def discover(body: DiscoverIn, request: Request) -> dict:
    """How this email signs in: 'password', or 'sso' with the URL that starts it."""
    with db.tx() as conn:
        c = sso.routed_connection(conn, body.email)
    if c is None or not oidc.configured(request.app.state.settings):
        return {"method": "password"}
    return {
        "method": "sso",
        "name": c["display_name"],
        "password_allowed": not c["require_sso"],
        "start_url": f"/api/v1/auth/oidc/start?idp={c['alias']}&next=/",
    }


# ---- connections (business admin) -----------------------------------------------


def _connection_out(conn, c: dict, issuer: str = "") -> dict:
    domains = conn.execute(
        "SELECT id, domain, status, decided_at FROM sso_domains WHERE connection_id = %s ORDER BY domain", (c["id"],)
    ).fetchall()
    return {
        "id": str(c["id"]),
        "alias": c["alias"],
        "protocol": c["protocol"],
        "display_name": c["display_name"],
        "metadata_url": c["metadata_url"],
        "has_metadata_xml": bool(c["metadata_xml"]),
        "client_id": c["client_id"],
        "status": c["status"],
        "require_sso": c["require_sso"],
        "last_test": c["last_test"],
        "tested_at": c["tested_at"],
        "updated_at": c["updated_at"],
        "domains": domains,
        # What the business enters in its own identity provider (Keycloak's broker endpoints).
        "provider_setup": {
            "redirect_uri": f"{issuer}/broker/{c['alias']}/endpoint" if issuer else "",
            "sp_entity_id": issuer,
            "sp_metadata_url": f"{issuer}/broker/{c['alias']}/endpoint/descriptor" if issuer else "",
        },
    }


def _get_connection(conn, customer_id: str, connection_id: str) -> dict:
    c = conn.execute(
        "SELECT * FROM sso_connections WHERE id::text = %s AND customer_id = %s", (connection_id, customer_id)
    ).fetchone()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found.")
    return c


def _idp(c: dict, client_secret: str = "", enabled: bool = True) -> keycloak.IdpConfig:
    return keycloak.IdpConfig(
        alias=c["alias"],
        display_name=c["display_name"],
        protocol=c["protocol"],
        metadata_xml=c["metadata_xml"],
        metadata_url=c["metadata_url"],
        client_id=c["client_id"],
        client_secret=client_secret,
        enabled=enabled,
    )


class ConnectionIn(BaseModel):
    protocol: Literal["saml", "oidc"]
    display_name: str = Field(min_length=1, max_length=80)
    domains: list[str] = Field(default_factory=list, max_length=20)
    metadata_xml: str = Field(default="", max_length=512_000)
    metadata_url: str = Field(default="", max_length=500)
    client_id: str = Field(default="", max_length=200)
    # Passed to the gateway and never stored by the controller.
    client_secret: str = Field(default="", max_length=500, repr=False)


class ConnectionPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    metadata_xml: str | None = Field(default=None, max_length=512_000)
    metadata_url: str | None = Field(default=None, max_length=500)
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=500, repr=False)
    require_sso: bool | None = None
    domains: list[str] | None = Field(default=None, max_length=20)


def _check_config(protocol: str, xml: str, url: str, client_id: str) -> tuple[str, str]:
    try:
        if protocol == "saml":
            if xml.strip():
                sso.check_saml_metadata(xml)
                return xml, ""
            if url.strip():
                return "", sso.check_url(url)
            raise sso.SsoError("Upload the identity provider's SAML metadata, or give its metadata URL.")
        if not url.strip() or not client_id.strip():
            raise sso.SsoError("OpenID Connect needs the discovery URL and a client ID.")
        return "", sso.check_url(url)
    except sso.SsoError as e:
        raise HTTPException(422, str(e)) from e


def _set_domains(conn, c: dict, domains: list[str], actor: str) -> None:
    try:
        wanted = sorted({sso.normalise_domain(d) for d in domains})
    except sso.SsoError as e:
        raise HTTPException(422, str(e)) from e
    conn.execute("DELETE FROM sso_domains WHERE connection_id = %s AND NOT (domain = ANY(%s))", (c["id"], wanted))
    for d in wanted:
        taken = conn.execute(
            "SELECT customer_id FROM sso_domains WHERE domain = %s AND status = 'approved' AND customer_id <> %s",
            (d, c["customer_id"]),
        ).fetchone()
        if taken:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{d} is already routed to another organisation.")
        conn.execute(
            """INSERT INTO sso_domains (connection_id, customer_id, domain, requested_by) VALUES (%s, %s, %s, %s)
               ON CONFLICT (connection_id, domain) DO NOTHING""",
            (c["id"], c["customer_id"], d, actor),
        )


@router.get("/customers/{customer_id}/sso-connections")
def list_connections(customer_id: str, user: UserDep, request: Request) -> dict:
    _biz_admin(user, customer_id)
    s = request.app.state.settings
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM sso_connections WHERE customer_id = %s ORDER BY created_at", (customer_id,)
        ).fetchall()
        items = [_connection_out(conn, c, request.app.state.settings.oidc_issuer) for c in rows]
    return {
        "gateway": {"configured": oidc.configured(s), "simulated": _gateway(request).simulated},
        "callback_url": oidc.redirect_uri(s) if s.public_url else "",
        "items": items,
    }


@router.post("/customers/{customer_id}/sso-connections", status_code=201)
def create_connection(customer_id: str, body: ConnectionIn, user: UserDep, request: Request) -> dict:
    _biz_admin(user, customer_id)
    xml, url = _check_config(body.protocol, body.metadata_xml, body.metadata_url, body.client_id)
    with db.tx() as conn:
        c = conn.execute(
            """INSERT INTO sso_connections (customer_id, alias, protocol, display_name, metadata_xml, metadata_url,
                                            client_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                customer_id,
                sso.new_alias(body.display_name),
                body.protocol,
                body.display_name.strip(),
                xml,
                url,
                body.client_id.strip(),
            ),
        ).fetchone()
        _set_domains(conn, c, body.domains, user.actor)
        _gateway_call(_gateway(request).upsert_idp, _idp(c, body.client_secret))
        audit.record(
            conn,
            user.actor,
            "sso.create",
            c["alias"],
            customer_id,
            {"protocol": body.protocol, "domains": body.domains},
        )
        return _connection_out(conn, c, request.app.state.settings.oidc_issuer)


@router.patch("/customers/{customer_id}/sso-connections/{connection_id}")
def update_connection(
    customer_id: str, connection_id: str, body: ConnectionPatch, user: UserDep, request: Request
) -> dict:
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        c = _get_connection(conn, customer_id, connection_id)
        changed = body.model_dump(exclude_unset=True, exclude={"client_secret"})
        config_changed = any(k in changed for k in ("metadata_xml", "metadata_url", "client_id")) or bool(
            body.client_secret
        )
        xml, url, client_id = c["metadata_xml"], c["metadata_url"], c["client_id"]
        if config_changed:
            xml, url = _check_config(
                c["protocol"],
                body.metadata_xml if body.metadata_xml is not None else ("" if body.metadata_url else xml),
                body.metadata_url if body.metadata_url is not None else url,
                body.client_id if body.client_id is not None else client_id,
            )
            client_id = (body.client_id if body.client_id is not None else client_id).strip()
        c = conn.execute(
            """UPDATE sso_connections SET display_name = %s, metadata_xml = %s, metadata_url = %s, client_id = %s,
                 require_sso = %s, updated_at = now(),
                 status = CASE WHEN %s THEN 'draft' ELSE status END
               WHERE id = %s RETURNING *""",
            (
                (body.display_name or c["display_name"]).strip(),
                xml,
                url,
                client_id,
                c["require_sso"] if body.require_sso is None else body.require_sso,
                config_changed,
                c["id"],
            ),
        ).fetchone()
        if body.domains is not None:
            _set_domains(conn, c, body.domains, user.actor)
        if config_changed or body.display_name:
            _gateway_call(_gateway(request).upsert_idp, _idp(c, body.client_secret or ""))
        audit.record(
            conn,
            user.actor,
            "sso.update",
            c["alias"],
            customer_id,
            {
                "changed": sorted(changed) + (["client_secret"] if body.client_secret else []),
                "needs_test": config_changed,
            },
        )
        return _connection_out(conn, c, request.app.state.settings.oidc_issuer)


class TestIn(BaseModel):
    next: str = Field(default="/", max_length=500)


@router.post("/customers/{customer_id}/sso-connections/{connection_id}/test")
def test_connection(
    customer_id: str, connection_id: str, user: UserDep, request: Request, body: Annotated[TestIn | None, Body()] = None
) -> dict:
    """Start a test sign-in. It records the result and never creates a session."""
    _biz_admin(user, customer_id)
    if not oidc.configured(request.app.state.settings):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The sign-in gateway is not set up yet, so a test sign-in can't run."
        )
    with db.tx() as conn:
        c = _get_connection(conn, customer_id, connection_id)
        _gateway_call(_gateway(request).set_enabled, c["alias"], True)
        audit.record(conn, user.actor, "sso.test_start", c["alias"], customer_id)
    url = begin(request, c["alias"], "test", (body or TestIn()).next, c["id"], user.actor)
    return {"start_url": url}


@router.post("/customers/{customer_id}/sso-connections/{connection_id}/enable")
def enable_connection(customer_id: str, connection_id: str, user: UserDep, request: Request) -> dict:
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        c = _get_connection(conn, customer_id, connection_id)
        if c["status"] not in ("tested", "enabled", "disabled") or not (c["last_test"] or {}).get("ok"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Run a successful test sign-in before switching it on.")
        _gateway_call(_gateway(request).set_enabled, c["alias"], True)
        c = conn.execute(
            "UPDATE sso_connections SET status = 'enabled', updated_at = now() WHERE id = %s RETURNING *", (c["id"],)
        ).fetchone()
        audit.record(conn, user.actor, "sso.enable", c["alias"], customer_id)
        return _connection_out(conn, c, request.app.state.settings.oidc_issuer)


@router.post("/customers/{customer_id}/sso-connections/{connection_id}/disable")
def disable_connection(customer_id: str, connection_id: str, user: UserDep, request: Request) -> dict:
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        c = _get_connection(conn, customer_id, connection_id)
        _gateway_call(_gateway(request).set_enabled, c["alias"], False)
        c = conn.execute(
            """UPDATE sso_connections SET status = CASE WHEN status = 'draft' THEN 'draft' ELSE 'disabled' END,
                 updated_at = now() WHERE id = %s RETURNING *""",
            (c["id"],),
        ).fetchone()
        audit.record(conn, user.actor, "sso.disable", c["alias"], customer_id)
        return _connection_out(conn, c, request.app.state.settings.oidc_issuer)


@router.delete("/customers/{customer_id}/sso-connections/{connection_id}", status_code=204)
def delete_connection(customer_id: str, connection_id: str, user: UserDep, request: Request) -> None:
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        c = _get_connection(conn, customer_id, connection_id)
        _gateway_call(_gateway(request).delete_idp, c["alias"])
        conn.execute("DELETE FROM sso_connections WHERE id = %s", (c["id"],))
        audit.record(conn, user.actor, "sso.delete", c["alias"], customer_id)


# ---- domain approvals (ExaCarib admins) -------------------------------------------


@router.get("/sso-domains")
def list_domains(user: AdminDep, state: Literal["pending", "approved", "rejected", "all"] = "pending") -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            """SELECT d.id, d.domain, d.status, d.requested_by, d.decided_by, d.decided_at, d.created_at,
                      c.display_name AS connection, cu.name AS customer, d.customer_id
               FROM sso_domains d JOIN sso_connections c ON c.id = d.connection_id
               JOIN customers cu ON cu.id = d.customer_id
               WHERE %s = 'all' OR d.status = %s ORDER BY d.created_at""",
            (state, state),
        ).fetchall()


@router.post("/sso-domains/{domain_id}/{decision}")
def decide_domain(domain_id: int, decision: Literal["approve", "reject"], user: AdminDep) -> dict:
    """An ExaCarib admin confirms the business really owns the domain (e.g. by a DNS TXT record)."""
    with db.tx() as conn:
        d = conn.execute("SELECT * FROM sso_domains WHERE id = %s FOR UPDATE", (domain_id,)).fetchone()
        if d is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Domain claim not found.")
        new = "approved" if decision == "approve" else "rejected"
        if (
            new == "approved"
            and conn.execute(
                "SELECT 1 FROM sso_domains WHERE domain = %s AND status = 'approved' AND id <> %s",
                (d["domain"], d["id"]),
            ).fetchone()
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, f"{d['domain']} is already routed to another connection.")
        row = conn.execute(
            """UPDATE sso_domains SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s
               RETURNING id, domain, status, decided_by, decided_at""",
            (new, user.actor, d["id"]),
        ).fetchone()
        audit.record(conn, user.actor, f"sso.domain_{decision}", d["domain"], d["customer_id"])
        return row


# ---- SCIM tokens (business admin) -----------------------------------------------

SCIM_COLUMNS = "id, name, prefix, created_by, created_at, last_used_at"


class ScimTokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.get("/customers/{customer_id}/scim-tokens")
def list_scim_tokens(customer_id: str, user: UserDep, request: Request) -> dict:
    _biz_admin(user, customer_id)
    s = request.app.state.settings
    with db.tx() as conn:
        rows = conn.execute(
            f"SELECT {SCIM_COLUMNS} FROM scim_tokens WHERE customer_id = %s AND revoked_at IS NULL"
            " ORDER BY created_at DESC",
            (customer_id,),
        ).fetchall()
    base = s.public_url or str(request.base_url).rstrip("/")
    return {"scim_url": f"{base}/api/v1/scim/v2", "items": rows}


@router.post("/customers/{customer_id}/scim-tokens", status_code=201)
def create_scim_token(customer_id: str, body: ScimTokenIn, user: UserDep) -> dict:
    """A bearer token for the business's directory (Entra ID, Okta...). Shown once."""
    _biz_admin(user, customer_id)
    token = scim.TOKEN_PREFIX + new_token()
    with db.tx() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM scim_tokens WHERE customer_id = %s AND revoked_at IS NULL", (customer_id,)
        ).fetchone()["n"]
        if n >= 5:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This organisation has 5 SCIM tokens. Revoke one first.")
        row = conn.execute(
            f"""INSERT INTO scim_tokens (customer_id, name, prefix, token_hash, created_by)
                VALUES (%s, %s, %s, %s, %s) RETURNING {SCIM_COLUMNS}""",
            (customer_id, body.name.strip(), token[:12], token_hash(token), user.actor),
        ).fetchone()
        audit.record(conn, user.actor, "scim_token.create", row["prefix"], customer_id, {"name": row["name"]})
    return {**row, "token": token}


@router.delete("/customers/{customer_id}/scim-tokens/{token_id}", status_code=204)
def revoke_scim_token(customer_id: str, token_id: int, user: UserDep) -> None:
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE scim_tokens SET revoked_at = now() WHERE id = %s AND customer_id = %s AND revoked_at IS NULL
               RETURNING prefix""",
            (token_id, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found.")
        audit.record(conn, user.actor, "scim_token.revoke", row["prefix"], customer_id)


# ---- directory groups -> teams, seats, admin rights --------------------------------


def _group_out(g: dict, members: int) -> dict:
    admin_state = "none"
    if g["admin_requested"]:
        admin_state = "approved" if g["admin_approved_at"] else "pending"
    return {
        "id": str(g["id"]),
        "display_name": g["display_name"],
        "team_id": str(g["team_id"]) if g["team_id"] else None,
        "team": g.get("team"),
        "seat": g["seat"],
        "business_admin": admin_state,
        "admin_requested_by": g["admin_requested_by"],
        "admin_approved_by": g["admin_approved_by"],
        "members": members,
    }


def _groups(conn, customer_id: str, group_id: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT g.*, t.name AS team, (SELECT count(*) FROM scim_group_members m WHERE m.group_id = g.id) AS n
           FROM scim_groups g LEFT JOIN commai_teams t ON t.id = g.team_id
           WHERE g.customer_id = %s AND (%s::text IS NULL OR g.id::text = %s) ORDER BY g.display_name""",
        (customer_id, group_id, group_id),
    ).fetchall()
    return rows


@router.get("/customers/{customer_id}/directory-groups")
def list_groups(customer_id: str, user: UserDep) -> list[dict]:
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        return [_group_out(g, g["n"]) for g in _groups(conn, customer_id)]


class MappingIn(BaseModel):
    seat: Literal["agent", "internal"] = "agent"
    team_id: str | None = None
    business_admin: bool = False


def _refresh_members(conn, customer_id: str, group_id) -> None:
    for m in conn.execute("SELECT user_id FROM scim_group_members WHERE group_id = %s", (group_id,)).fetchall():
        scim.refresh_rights(conn, customer_id, m["user_id"])


@router.put("/customers/{customer_id}/directory-groups/{group_id}")
def set_mapping(customer_id: str, group_id: str, body: MappingIn, user: UserDep) -> dict:
    """Map a directory group to a team and seat. Asking for business-admin rights
    leaves the mapping pending until another admin approves it."""
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        found = _groups(conn, customer_id, group_id)
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found.")
        g = found[0]
        if (
            body.team_id
            and not conn.execute(
                "SELECT 1 FROM commai_teams WHERE id::text = %s AND customer_id = %s", (body.team_id, customer_id)
            ).fetchone()
        ):
            raise HTTPException(422, "That team is not in this organisation.")
        if str(g["team_id"] or "") != (body.team_id or ""):
            for m in conn.execute("SELECT user_id FROM scim_group_members WHERE group_id = %s", (g["id"],)):
                scim._leave_team(conn, g, m["user_id"])
        keep_approval = body.business_admin and g["admin_requested"]
        conn.execute(
            """UPDATE scim_groups SET seat = %s, team_id = %s, admin_requested = %s,
                 admin_requested_by = CASE WHEN %s THEN admin_requested_by WHEN %s THEN %s ELSE NULL END,
                 admin_approved_by = CASE WHEN %s THEN admin_approved_by ELSE NULL END,
                 admin_approved_at = CASE WHEN %s THEN admin_approved_at ELSE NULL END,
                 updated_at = now() WHERE id = %s""",
            (
                body.seat,
                body.team_id,
                body.business_admin,
                keep_approval,
                body.business_admin,
                user.actor,
                keep_approval,
                keep_approval,
                g["id"],
            ),
        )
        _refresh_members(conn, customer_id, g["id"])
        audit.record(
            conn,
            user.actor,
            "directory_group.map",
            g["display_name"],
            customer_id,
            {"seat": body.seat, "team_id": body.team_id, "business_admin": body.business_admin},
        )
        g = _groups(conn, customer_id, group_id)[0]
        return _group_out(g, g["n"])


@router.post("/customers/{customer_id}/directory-groups/{group_id}/approve-admin")
def approve_admin(customer_id: str, group_id: str, user: UserDep) -> dict:
    """Grant a pending business-admin mapping. The approver must be a different
    business admin than the one who asked, or an ExaCarib admin."""
    _biz_admin(user, customer_id)
    if user.scopes is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a full business admin account can approve this.")
    with db.tx() as conn:
        found = _groups(conn, customer_id, group_id)
        if not found or not found[0]["admin_requested"]:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No admin mapping is waiting for this group.")
        g = found[0]
        if g["admin_requested_by"] == user.actor and user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Someone other than the person who asked must approve it.")
        conn.execute(
            "UPDATE scim_groups SET admin_approved_by = %s, admin_approved_at = now(), updated_at = now()"
            " WHERE id = %s",
            (user.actor, g["id"]),
        )
        _refresh_members(conn, customer_id, g["id"])
        audit.record(conn, user.actor, "directory_group.approve_admin", g["display_name"], customer_id)
        g = _groups(conn, customer_id, group_id)[0]
        return _group_out(g, g["n"])
