"""Request authentication: portal users (bearer session tokens) and agents
(client certificate verified by the TLS proxy)."""

from __future__ import annotations

import hmac
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
    customer_id: Any
    carrier_id: Any

    @property
    def actor(self) -> str:
        return f"user:{self.email}"


def current_user(authorization: Annotated[str | None, Header()] = None) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
    with db.tx() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = %s AND s.expires_at > now()",
            (token_hash(authorization.removeprefix("Bearer ")),),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has ended. Sign in again.")
    return User(row["id"], row["email"], row["role"], row["customer_id"], row["carrier_id"])


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only.")
    return user


def require_viewer(user: Annotated[User, Depends(current_user)]) -> User:
    """Admins and customer users. Carrier users get their own view (M6)."""
    if user.role not in ("admin", "customer"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for this account.")
    return user


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
