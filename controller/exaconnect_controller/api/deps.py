"""Request authentication: portal users (bearer session tokens) and agents
(client certificate verified by the TLS proxy)."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from .. import db, pki
from ..security import token_hash


@dataclass
class User:
    id: Any
    email: str
    role: str
    customer_id: Any
    carrier_id: Any
    # API keys may carry scopes (ADR 0016); None means everything the owner may do.
    scopes: tuple[str, ...] | None = None
    # How the request signed in: 'session' (Bearer), 'cookie' (portal) or 'key'.
    via: str = "session"
    # Roles (ADR 0024): None means the person has no roles and keeps their
    # account's rights; otherwise the business-wide permissions, plus
    # permissions held for single teams only.
    permissions: frozenset[str] | None = None
    team_permissions: dict = field(default_factory=dict)
    path: str = ""
    query_team: str | None = None

    @property
    def actor(self) -> str:
        return f"user:{self.email}"


API_KEY_PREFIX = "exa_"
SESSION_COOKIE = "exa_session"
# Cookie-authenticated writes must carry this header (a cross-site form can't set it).
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "exa-portal"


def current_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> User:
    if authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
        token = authorization.removeprefix("Bearer ")
        via_cookie = False
    else:
        # The portal's session cookie (ADR 0017). Websocket callers pass a header instead.
        cookies = getattr(request, "cookies", None) or {}
        token = cookies.get(SESSION_COOKIE) or ""
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
        via_cookie = True
        method = getattr(request, "method", "GET")
        if method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This request did not come from the portal.")
    is_key = token.startswith(API_KEY_PREFIX)
    with db.tx() as conn:
        if is_key:
            # An API key (ADR 0013): it acts as the person who made it.
            row = conn.execute(
                """WITH k AS (
                     UPDATE api_keys SET last_used_at = now()
                     WHERE token_hash = %(h)s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
                       AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
                     RETURNING user_id)
                   SELECT u.*, kk.scopes AS key_scopes, kk.id AS key_id, kk.locked_until AS key_locked_until
                   FROM users u, (SELECT scopes, id, locked_until FROM api_keys WHERE token_hash = %(h)s) kk
                   WHERE u.id = (SELECT user_id FROM k UNION ALL
                                 SELECT user_id FROM api_keys WHERE token_hash = %(h)s AND revoked_at IS NULL
                                   AND (expires_at IS NULL OR expires_at > now()) LIMIT 1)
                     AND u.disabled_at IS NULL""",
                {"h": token_hash(token)},
            ).fetchone()
        else:
            row = None
        if row is None:
            row = conn.execute(
                "SELECT u.*, s.via AS session_via, s.created_at AS session_created_at, s.token_hash AS session_hash"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = %s AND s.expires_at > now() AND u.disabled_at IS NULL",
                (token_hash(token),),
            ).fetchone()
            is_key = False if row is not None else is_key
    if row is None and is_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This API key has been revoked or has expired.")
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has ended. Sign in again.")
    scopes = _scopes(row.get("key_scopes"), row.get("access_scopes"))
    user = User(
        row["id"],
        row["email"],
        row["role"],
        row["customer_id"],
        row["carrier_id"],
        scopes,
        "key" if is_key else ("cookie" if via_cookie else "session"),
    )
    # Business security settings and roles (ADR 0024).
    from ..commai.enterprise import security as enterprise_security

    enterprise_security.guard(request, user, row)
    if user.scopes is not None and "connect" not in user.scopes:
        # A key limited to CommAI scopes never reaches the network API. A
        # limited account (directory-provisioned) may still manage its own sign-in.
        path = str(request.url.path)
        allowed = path.startswith("/api/v1/commai/") or path == "/api/v1/auth/me"
        if not allowed and user.via != "key":
            allowed = path.startswith("/api/v1/auth/")
        if not allowed:
            what = "API key" if user.via == "key" else "account"
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"This {what} is not allowed to use this part of the API.")
    return user


def _scopes(key_scopes: Any, account_scopes: Any) -> tuple[str, ...] | None:
    """What a request may do: the key's scopes within the account's (None = no limit)."""
    if key_scopes is None and account_scopes is None:
        return None
    if key_scopes is None:
        return tuple(account_scopes)
    if account_scopes is None:
        return tuple(key_scopes)
    return tuple(s for s in key_scopes if s in account_scopes)


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only.")
    return user


def require_viewer(user: Annotated[User, Depends(current_user)]) -> User:
    """Admins and customer users. Carrier users get their own view (M6)."""
    if user.role not in ("admin", "customer"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for this account.")
    return user


def check_customer(user: User, customer_id: Any) -> None:
    """Admins act for any customer; customer users only for their own."""
    if user.role == "admin":
        return
    if user.role == "customer" and str(user.customer_id) == str(customer_id):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for this account.")


def customer_scope(user: User) -> Any | None:
    """None means all customers (admin)."""
    return None if user.role == "admin" else user.customer_id


@dataclass
class Node:
    id: Any
    name: str
    customer_id: Any
    site_id: Any


def current_node(
    request: Request,
    x_exa_proxy: Annotated[str | None, Header()] = None,
    x_ssl_client_verify: Annotated[str | None, Header()] = None,
    x_ssl_client_serial: Annotated[str | None, Header()] = None,
) -> Node:
    secret = request.app.state.settings.proxy_secret
    if not secret or not x_exa_proxy or not hmac.compare_digest(x_exa_proxy, secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent endpoints are only served through the TLS proxy")
    if x_ssl_client_verify != "SUCCESS" or not x_ssl_client_serial:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "client certificate required")
    with db.tx() as conn:
        row = conn.execute(
            "UPDATE nodes SET last_seen = now() WHERE cert_serial = %s RETURNING id, name, customer_id, site_id",
            (pki.normalise_serial(x_ssl_client_serial),),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or replaced client certificate")
    return Node(row["id"], row["name"], row["customer_id"], row["site_id"])


UserDep = Annotated[User, Depends(current_user)]
AdminDep = Annotated[User, Depends(require_admin)]
ViewerDep = Annotated[User, Depends(require_viewer)]
NodeDep = Annotated[Node, Depends(current_node)]
