"""Portal sessions: an opaque token kept hashed in `sessions`, carried in a
secure HttpOnly cookie for the portal and as a Bearer token for scripts."""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from fastapi import Request, Response

from ..security import new_token, token_hash

COOKIE = "exa_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "exa-portal"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def start(conn: psycopg.Connection, user_id: Any, hours: int, via: str = "password") -> tuple[str, dt.datetime]:
    token = new_token()
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours)
    conn.execute("DELETE FROM sessions WHERE user_id = %s AND expires_at < now()", (user_id,))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, via) VALUES (%s, %s, %s, %s)",
        (token_hash(token), user_id, expires, via),
    )
    return token, expires


def set_cookie(response: Response, request: Request, token: str, expires: dt.datetime) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        COOKIE,
        token,
        max_age=max(0, int((expires - dt.datetime.now(dt.UTC)).total_seconds())),
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE, path="/", httponly=True, samesite="lax", secure=request.app.state.settings.cookie_secure
    )


def request_token(request: Request) -> tuple[str | None, bool]:
    """(token, from_cookie). The Authorization header wins over the cookie."""
    auth = request.headers.get("authorization")
    if auth:
        return (auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None), False
    return request.cookies.get(COOKIE), True


def end_all(conn: psycopg.Connection, user_id: Any, revoke_keys: bool = False) -> None:
    """Sign a person out everywhere at once (and, if asked, revoke their API keys)."""
    conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM mfa_challenges WHERE user_id = %s", (user_id,))
    if revoke_keys:
        conn.execute("UPDATE api_keys SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
