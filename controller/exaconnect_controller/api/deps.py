"""Request authentication: portal users (bearer session tokens) and agents
(client certificate verified by the TLS proxy)."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from .. import db, pki
from ..security import token_hash


@dataclass
class User:
    id: Any
    email: str
    role: str
    # For customer accounts, the organisation this request acts for (ADR 0023):
    # the session's current organisation, or the one an API key was made in.
    # None when the account belongs to no organisation.
    customer_id: Any
    carrier_id: Any
    # API keys may carry scopes (ADR 0016); None means everything the owner may do.
    scopes: tuple[str, ...] | None = None
    # How the request signed in: 'session' (Bearer), 'cookie' (portal) or 'key'.
    via: str = "session"
    # The person's role in that organisation: owner, admin, member or viewer.
    # None for ExaCarib admins and carrier accounts.
    org_role: str | None = None
    # The person's primary organisation (users.customer_id).
    home_customer_id: Any = None
    # The plans that organisation holds ('connect', 'commai'); None for ExaCarib
    # admins and carrier accounts, who are not limited by plan.
    products: tuple[str, ...] | None = None

    @property
    def actor(self) -> str:
        return f"user:{self.email}"


API_KEY_PREFIX = "exa_"
SESSION_COOKIE = "exa_session"
# Cookie-authenticated writes must carry this header (a cross-site form can't set it).
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "exa-portal"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

ORG_ROLES = ("owner", "admin", "member", "viewer")
VIEWER_ONLY = "You have view-only access to this organisation. Ask an owner or admin to change your role."
NO_ORG = "You are not a member of an organisation. Ask an owner to invite you."
# What anyone signed in may do whatever their role in the organisation: their
# own sign-in and keys, switching organisation, accepting an invitation,
# leaving an organisation, and asking a question (any plan an answer suggests
# still needs a write to apply).
_ALWAYS = ("/api/v1/auth/", "/api/v1/invites/")
_ALWAYS_EXACT = ("/api/v1/ai/ask",)
_LEAVE = re.compile(r"^/api/v1/orgs/[^/]+/leave$")


def _always_allowed(path: str) -> bool:
    return path.startswith(_ALWAYS) or path in _ALWAYS_EXACT or bool(_LEAVE.match(path))


def current_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> User:
    method = getattr(request, "method", "GET")
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
        if method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This request did not come from the portal.")
    is_key = token.startswith(API_KEY_PREFIX)
    with db.tx() as conn:
        if is_key:
            # An API key (ADR 0013): it acts as the person who made it, in the
            # organisation it was made in (ADR 0023).
            row = conn.execute(
                """WITH k AS (
                     UPDATE api_keys SET last_used_at = now()
                     WHERE token_hash = %(h)s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
                       AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
                     RETURNING user_id)
                   SELECT u.*, (SELECT scopes FROM api_keys WHERE token_hash = %(h)s) AS key_scopes,
                          (SELECT customer_id FROM api_keys WHERE token_hash = %(h)s) AS acting_org FROM users u
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
                "SELECT u.*, s.customer_id AS acting_org FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = %s AND s.expires_at > now() AND u.disabled_at IS NULL",
                (token_hash(token),),
            ).fetchone()
            is_key = False if row is not None else is_key
        org = membership_for(conn, row, is_key) if row is not None and row["role"] == "customer" else None
    if row is None and is_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This API key has been revoked or has expired.")
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has ended. Sign in again.")
    scopes = _scopes(row.get("key_scopes"), row.get("access_scopes"))
    customer = row["role"] == "customer"
    user = User(
        row["id"],
        row["email"],
        row["role"],
        # A customer account acts for its current organisation only.
        (org["customer_id"] if org else None) if customer else row["customer_id"],
        row["carrier_id"],
        scopes,
        "key" if is_key else ("cookie" if via_cookie else "session"),
        org["role"] if org else None,
        row["customer_id"],
        products_of(org["products"]) if org else None,
    )
    path = str(request.url.path)
    if customer and user.customer_id is None and not _always_allowed(path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, NO_ORG)
    # Viewers are read-only everywhere, enforced here once for every write.
    if user.org_role == "viewer" and method not in SAFE_METHODS and not _always_allowed(path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, VIEWER_ONLY)
    if user.scopes is not None and "connect" not in user.scopes:
        # A key limited to CommAI scopes never reaches the network API. A
        # limited account (directory-provisioned) may still manage its own sign-in.
        allowed = path.startswith("/api/v1/commai/") or path == "/api/v1/auth/me"
        if not allowed and user.via != "key":
            allowed = path.startswith(("/api/v1/auth/", "/api/v1/invites/", "/api/v1/orgs/"))
        if not allowed:
            what = "API key" if user.via == "key" else "account"
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"This {what} is not allowed to use this part of the API.")
    return user


def membership_for(conn: Any, row: dict, is_key: bool) -> dict | None:
    """The organisation a customer account acts for, and its role there.

    A key acts in the organisation it was made in and in no other. A session
    acts in the one the person switched to, else their primary organisation,
    else the first one they joined."""
    rows = conn.execute(
        """SELECT m.customer_id, m.role, c.products FROM org_memberships m JOIN customers c ON c.id = m.customer_id
           WHERE m.user_id = %s ORDER BY m.created_at, m.customer_id""",
        (row["id"],),
    ).fetchall()
    by_id = {str(r["customer_id"]): r for r in rows}
    wanted = row.get("acting_org")
    if is_key:
        return by_id.get(str(wanted if wanted is not None else row["customer_id"]))
    for c in (wanted, row["customer_id"]):
        if c is not None and str(c) in by_id:
            return by_id[str(c)]
    return rows[0] if rows else None


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


PRODUCTS = ("connect", "commai")
PRODUCT_NAMES = {"connect": "Connect", "commai": "CommAI"}


def products_of(value: Any) -> tuple[str, ...]:
    """An organisation's plans. NULL (an organisation from before plans) holds both."""
    return PRODUCTS if value is None else tuple(p for p in PRODUCTS if p in value)


def check_product(user: User, product: str) -> None:
    """403 unless the organisation the request acts for holds this plan. ExaCarib
    admins are not limited by plan; carrier accounts keep their Connect carrier view."""
    if user.role != "customer" or user.products is None or product in user.products:
        return
    name = PRODUCT_NAMES[product]
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        f"Your organisation doesn't have the {name} plan. Ask your ExaCarib account manager to add it.",
    )


def require_product(product: str):
    """A router dependency: every endpoint under it needs this plan (ADR 0023)."""
    if product not in PRODUCTS:
        raise ValueError(product)

    def dependency(user: Annotated[User, Depends(current_user)]) -> User:
        check_product(user, product)
        return user

    return dependency


def require_viewer(user: Annotated[User, Depends(current_user)]) -> User:
    """Admins and customer users. Carrier users get their own view (M6)."""
    if user.role not in ("admin", "customer"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for this account.")
    return user


def check_customer(user: User, customer_id: Any) -> None:
    """Admins act for any customer; customer users only for the organisation
    they are currently acting for (ADR 0023)."""
    if user.role == "admin":
        return
    if user.role == "customer" and user.customer_id is not None and str(user.customer_id) == str(customer_id):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for this account.")


def customer_scope(user: User) -> Any | None:
    """None means all customers (admin)."""
    if user.role == "admin":
        return None
    if user.customer_id is None:
        # Never "all customers": an account without an organisation sees nothing.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for this account.")
    return user.customer_id


def can_manage_org(user: User) -> bool:
    """People and settings: ExaCarib admins, and an organisation's owners and admins."""
    if user.role == "admin":
        return True
    return user.role == "customer" and user.org_role in ("owner", "admin")


def require_org_manager(user: User, customer_id: Any) -> None:
    check_customer(user, customer_id)
    if not can_manage_org(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only this organisation's owners and admins can manage people and settings."
        )


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
