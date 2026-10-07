"""Developer platform API (ADR 0025): OAuth 2.0 for partner apps, sandboxes and
sandbox keys, the API policy, changelog and event catalogue."""

from __future__ import annotations

import base64
import json
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ... import audit, db
from ...api.deps import UserDep
from .. import access, apipolicy, events, oauth, partners, sandbox
from .partners import _partner, _person

router = APIRouter(tags=["commai: developer"])
public = APIRouter(tags=["commai: developer"])


def _oauth_http(e: oauth.OAuthError) -> HTTPException:
    return HTTPException(e.code, f"{e.description} ({e.error})")


# ---- OAuth clients (a partner's apps) ------------------------------------------------------------


class ClientIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    scopes: list[str] = Field(min_length=1, max_length=10)
    confidential: bool = False


@router.get("/partners/{partner_id}/oauth-clients")
def list_clients(partner_id: str, user: UserDep) -> list[dict]:
    with db.tx() as conn:
        _partner(conn, user, partner_id)
        return conn.execute(
            """SELECT client_id, name, redirect_uris, scopes, confidential, created_by, created_at,
                      (SELECT count(*) FROM commai_oauth_grants g WHERE g.client_id = c.client_id
                       AND g.revoked_at IS NULL) AS active_grants
               FROM commai_oauth_clients c WHERE partner_id = %s AND revoked_at IS NULL ORDER BY created_at""",
            (partner_id,),
        ).fetchall()


@router.post("/partners/{partner_id}/oauth-clients", status_code=201)
def register_client(partner_id: str, body: ClientIn, user: UserDep) -> dict:
    """Register an app. A confidential app's secret is shown once, here."""
    try:
        with db.tx() as conn:
            _partner(conn, user, partner_id, admin=True)
            row = oauth.register(
                conn, partner_id, body.name, body.redirect_uris, body.scopes, body.confidential, user.actor
            )
            detail = {"partner_id": partner_id, "scopes": row["scopes"], "redirect_uris": row["redirect_uris"]}
            audit.record(conn, user.actor, "commai.oauth.client.create", row["client_id"], None, detail)
            return row
    except oauth.OAuthError as e:
        raise _oauth_http(e) from e


@router.delete("/partners/{partner_id}/oauth-clients/{client_id}", status_code=204)
def withdraw_client(partner_id: str, client_id: str, user: UserDep) -> None:
    with db.tx() as conn:
        _partner(conn, user, partner_id, admin=True)
        row = conn.execute(
            """UPDATE commai_oauth_clients SET revoked_at = now() WHERE client_id = %s AND partner_id = %s
               AND revoked_at IS NULL RETURNING client_id""",
            (client_id, partner_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "App not found.")
        for g in conn.execute(
            "SELECT id FROM commai_oauth_grants WHERE client_id = %s AND revoked_at IS NULL", (client_id,)
        ).fetchall():
            oauth.revoke_grant(conn, g["id"], user.actor)
        audit.record(conn, user.actor, "commai.oauth.client.withdraw", client_id, None, {"partner_id": partner_id})


# ---- consent (the portal's consent page) -------------------------------------------------------------


class AuthorizeIn(BaseModel):
    client_id: str = Field(max_length=100)
    redirect_uri: str = Field(max_length=500)
    scope: str = Field(default="", max_length=300)
    state: str = Field(default="", max_length=500)
    code_challenge: str = Field(default="", max_length=128)
    code_challenge_method: str = Field(default="", max_length=10)
    response_type: str = Field(default="code", max_length=20)
    approve: bool = False


def _consenter(conn, user) -> None:
    if user.role != "customer" or user.customer_id is None:
        raise HTTPException(403, "Sign in with an account of the business that will use this app.")
    if partners.is_delegate(conn, user.id):
        raise HTTPException(403, "Only the business's own people can approve an app, not a partner acting for it.")


@router.get("/oauth/authorize")
def consent_info(
    user: UserDep,
    client_id: str = "",
    redirect_uri: str = "",
    scope: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    response_type: str = "code",
) -> dict:
    """What the consent page shows: the app, its partner, and the scopes it would get."""
    _person(user)
    with db.tx() as conn:
        _consenter(conn, user)
        try:
            c, requested = oauth.check_request(
                conn, client_id, redirect_uri, scope, code_challenge, code_challenge_method, response_type
            )
        except oauth.OAuthError as e:
            raise _oauth_http(e) from e
        own = set(access.SCOPES) if user.scopes is None else set(user.scopes)
        cust = conn.execute("SELECT name FROM customers WHERE id = %s", (user.customer_id,)).fetchone()
    return {
        "client": {"client_id": c["client_id"], "name": c["name"], "partner_name": c["partner_name"]},
        "redirect_uri": redirect_uri,
        "requested": requested,
        "granted": sorted(set(requested) & own),
        "not_granted": sorted(set(requested) - own),
        "customer_name": cust["name"] if cust else "",
    }


@router.post("/oauth/authorize")
def consent(body: AuthorizeIn, user: UserDep) -> dict:
    """Approve or refuse. Returns where to send the browser next (the app's redirect URI)."""
    _person(user)
    with db.tx() as conn:
        _consenter(conn, user)
        try:
            c, requested = oauth.check_request(
                conn, body.client_id, body.redirect_uri, body.scope, body.code_challenge,
                body.code_challenge_method, body.response_type,
            )  # fmt: skip
        except oauth.OAuthError as e:
            raise _oauth_http(e) from e
        q: dict[str, str] = {}
        if not body.approve:
            q["error"] = "access_denied"
            audit.record(conn, user.actor, "commai.oauth.deny", c["client_id"], user.customer_id)
        else:
            try:
                code, granted = oauth.issue_code(conn, c, user, requested, body.redirect_uri, body.code_challenge)
            except oauth.OAuthError as e:
                raise _oauth_http(e) from e
            q["code"] = code
            audit.record(conn, user.actor, "commai.oauth.consent", c["client_id"], user.customer_id,
                         {"scopes": granted})  # fmt: skip
        if body.state:
            q["state"] = body.state
    sep = "&" if urllib.parse.urlparse(body.redirect_uri).query else "?"
    return {"redirect": body.redirect_uri + sep + urllib.parse.urlencode(q)}


# ---- token and revoke (public, RFC 6749 / 7009) --------------------------------------------------------


async def _params(request: Request) -> dict[str, str]:
    raw = await request.body()
    if len(raw) > 20_000:
        return {}
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        try:
            data = json.loads(raw or b"{}")
            return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}


def _client_auth(request: Request, p: dict[str, str]) -> tuple[str, str | None]:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            return urllib.parse.unquote(cid), urllib.parse.unquote(secret)
        except ValueError:
            return "", None
    return p.get("client_id", ""), p.get("client_secret")


def _oauth_error(e: oauth.OAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": e.error, "error_description": e.description},
        status_code=401 if e.error == "invalid_client" else 400,
        headers={"Cache-Control": "no-store"},
    )


@public.post("/oauth/token")
async def token(request: Request):
    """Exchange a code (with its PKCE verifier) or a refresh token for tokens."""
    p = await _params(request)
    cid, secret = _client_auth(request, p)

    def run():
        err = None
        out = None
        with db.tx() as conn:  # commits revocations even when the request is refused
            try:
                c = oauth.authenticate(conn, cid, secret)
                grant = p.get("grant_type", "")
                if grant == "authorization_code":
                    out = oauth.exchange_code(
                        conn, c, p.get("code", ""), p.get("redirect_uri", ""), p.get("code_verifier", "")
                    )
                    g = out.pop("_grant")
                    audit.record(conn, f"app:{c['client_id']}", "commai.oauth.token", str(g["id"]), g["customer_id"],
                                 {"scopes": list(g["scopes"])})  # fmt: skip
                elif grant == "refresh_token":
                    out = oauth.refresh(conn, c, p.get("refresh_token", ""), p.get("scope"))
                else:
                    raise oauth.OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
            except oauth.OAuthError as e:
                err = e
        if err:
            return _oauth_error(err)
        return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    return await run_in_threadpool(run)


@public.post("/oauth/revoke")
async def revoke(request: Request):
    """RFC 7009: revoke an access or refresh token (and its grant). Unknown tokens are fine."""
    p = await _params(request)
    cid, secret = _client_auth(request, p)

    def run():
        with db.tx() as conn:
            try:
                c = oauth.authenticate(conn, cid, secret)
            except oauth.OAuthError as e:
                return _oauth_error(e)
            oauth.revoke_token(conn, c, p.get("token", ""))
            audit.record(conn, f"app:{c['client_id']}", "commai.oauth.revoke", c["client_id"])
        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    return await run_in_threadpool(run)


# ---- the business's view of apps ---------------------------------------------------------------------------


@router.get("/customers/{customer_id}/oauth-grants")
def list_grants(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return conn.execute(
            """SELECT g.id, g.client_id, c.name AS app, p.name AS partner_name, g.scopes, u.email AS approved_by,
                      g.created_at, g.refreshed_at, g.expires_at
               FROM commai_oauth_grants g JOIN commai_oauth_clients c USING (client_id)
               JOIN commai_partners p ON p.id = c.partner_id JOIN users u ON u.id = g.user_id
               WHERE g.customer_id = %s AND g.revoked_at IS NULL ORDER BY g.created_at DESC""",
            (customer_id,),
        ).fetchall()


@router.delete("/customers/{customer_id}/oauth-grants/{grant_id}", status_code=204)
def revoke_grant(customer_id: str, grant_id: str, user: UserDep) -> None:
    """The business ends an app's access (its admins, or the person who approved it)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        g = conn.execute(
            "SELECT * FROM commai_oauth_grants WHERE id = %s AND customer_id = %s AND revoked_at IS NULL",
            (grant_id, customer_id),
        ).fetchone()
        if g is None:
            raise HTTPException(404, "Not found.")
        if str(g["user_id"]) != str(user.id):
            access.require_business_admin(user)
        oauth.revoke_grant(conn, grant_id, user.actor)
        audit.record(conn, user.actor, "commai.oauth.grant.revoke", grant_id, customer_id, {"client": g["client_id"]})


# ---- sandboxes -----------------------------------------------------------------------------------------------


class SandboxKeyIn(BaseModel):
    name: str = Field(default="Sandbox key", max_length=100)
    scopes: list[str] | None = Field(default=None, max_length=10)


def _sandbox_errors(e: sandbox.SandboxError) -> HTTPException:
    return HTTPException(e.code, str(e))


@router.get("/customers/{customer_id}/sandbox")
def get_sandbox(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        return {"sandbox": sandbox.of(conn, customer_id), "keys": sandbox.list_keys(conn, customer_id)}


@router.post("/customers/{customer_id}/sandbox", status_code=201)
def make_sandbox(customer_id: str, user: UserDep) -> dict:
    """A copy of this business where every provider is simulated and nothing real is sent."""
    access.check(user, customer_id, "commai:admin")
    try:
        with db.tx() as conn:
            sb = sandbox.create(conn, customer_id)
            audit.record(conn, user.actor, "commai.sandbox.create", str(sb["id"]), customer_id)
            return sb
    except sandbox.SandboxError as e:
        raise _sandbox_errors(e) from e


@router.post("/customers/{customer_id}/sandbox/keys", status_code=201)
def make_sandbox_key(customer_id: str, body: SandboxKeyIn, user: UserDep) -> dict:
    """A key that acts only in the sandbox. Shown once."""
    access.check(user, customer_id, "commai:admin")
    try:
        with db.tx() as conn:
            row = sandbox.create_key(conn, customer_id, body.name, body.scopes)
            audit.record(conn, user.actor, "commai.sandbox.key.create", row["prefix"], customer_id,
                         {"scopes": row["scopes"]})  # fmt: skip
            return row
    except sandbox.SandboxError as e:
        raise _sandbox_errors(e) from e


@router.delete("/customers/{customer_id}/sandbox/keys/{key_id}", status_code=204)
def revoke_sandbox_key(customer_id: str, key_id: int, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    try:
        with db.tx() as conn:
            row = sandbox.revoke_key(conn, customer_id, key_id)
            audit.record(conn, user.actor, "commai.sandbox.key.revoke", row["prefix"], customer_id)
    except sandbox.SandboxError as e:
        raise _sandbox_errors(e) from e


# ---- API policy, changelog, event catalogue ----------------------------------------------------------------------


@public.get("/changelog")
def changelog() -> dict:
    return {"version": apipolicy.API_VERSION, "entries": apipolicy.CHANGELOG}


@public.get("/api-policy")
def api_policy() -> dict:
    return {
        "version": apipolicy.API_VERSION,
        "policy": [
            "The version is in the path: /api/v1. Within a version, changes only add (new endpoints, new optional "
            "fields, new event types).",
            "A breaking change needs a new version or a new endpoint. The old one is deprecated first.",
            "A deprecated endpoint keeps working for at least six months. Its responses carry Deprecation (RFC 9745),"
            " Sunset (RFC 8594) and a Link to its successor.",
            "Every change is listed in the changelog (/api/v1/commai/changelog).",
        ],
        "deprecations": apipolicy.deprecations_view(),
    }


@router.get("/customers/{customer_id}/event-catalogue")
def event_catalogue(customer_id: str, user: UserDep) -> list[dict]:
    """Every event type, with its family. Webhooks can subscribe to a type or a family ("conversation.*")."""
    access.check(user, customer_id, "commai:read")
    return [{"type": t, "family": t.split(".")[0], "webhooks": True} for t in sorted(events.TYPES | {"webhook.test"})]
