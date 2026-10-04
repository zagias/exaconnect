"""Portal sign-in with local accounts (CLAUDE.md §4.2). SSO can replace this later."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit, db
from ..security import hash_password, new_token, token_hash, verify_password
from .deps import API_KEY_PREFIX, UserDep

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=1024)


# Throttling: after this many failed sign-ins for one email (or from one
# address) in the window, further attempts are refused until it passes.
MAX_FAILURES = 10
FAILURE_WINDOW = "15 minutes"


def _client_ip(request: Request) -> str:
    # Behind the public proxy (deploy/caddy) the real address is in X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "")


class UserOut(BaseModel):
    email: str
    role: str
    customer_id: str | None = None


class LoginOut(BaseModel):
    token: str
    expires_at: dt.datetime
    user: UserOut


@router.post("/login")
def login(body: LoginIn, request: Request) -> LoginOut:
    hours = request.app.state.settings.session_hours
    ip = _client_ip(request)
    with db.tx() as conn:
        recent = conn.execute(
            f"""SELECT count(*) FILTER (WHERE lower(target) = lower(%s)) AS by_email,
                       count(*) FILTER (WHERE detail->>'ip' = %s) AS by_ip
                FROM audit_log WHERE action = 'login_failed' AND at > now() - interval '{FAILURE_WINDOW}'""",
            (body.email, ip),
        ).fetchone()
        if recent["by_email"] >= MAX_FAILURES or recent["by_ip"] >= MAX_FAILURES * 3:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-ins. Try again in 15 minutes.")
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (body.email,)).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        # Its own transaction, so the record survives the error response.
        with db.tx() as conn:
            audit.record(conn, f"user:{body.email}", "login_failed", body.email, detail={"ip": ip})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That email and password don't match.")
    with db.tx() as conn:
        token = new_token()
        expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours)
        conn.execute("DELETE FROM sessions WHERE user_id = %s AND expires_at < now()", (row["id"],))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (token_hash(token), row["id"], expires),
        )
        audit.record(conn, f"user:{row['email']}", "login", customer_id=row["customer_id"])
    return LoginOut(
        token=token,
        expires_at=expires,
        user=UserOut(
            email=row["email"],
            role=row["role"],
            customer_id=str(row["customer_id"]) if row["customer_id"] else None,
        ),
    )


@router.get("/me")
def me(user: UserDep) -> UserOut:
    return UserOut(email=user.email, role=user.role, customer_id=str(user.customer_id) if user.customer_id else None)


@router.post("/logout", status_code=204)
def logout(user: UserDep, authorization: str = Header()) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(authorization.removeprefix("Bearer ")),))
        audit.record(conn, user.actor, "logout", customer_id=user.customer_id)


class PasswordIn(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(min_length=12, max_length=1024)


@router.post("/password", status_code=204)
def change_password(body: PasswordIn, user: UserDep, authorization: str = Header()) -> None:
    """Change your own password. Other sessions for the account are signed out."""
    with db.tx() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user.id,)).fetchone()
        if not verify_password(body.current, row["password_hash"]):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is not right.")
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(body.new), user.id))
        conn.execute(
            "DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s",
            (user.id, token_hash(authorization.removeprefix("Bearer "))),
        )
        audit.record(conn, user.actor, "user.change_password", user.email, user.customer_id)


# ---- API keys (ADR 0013) ----------------------------------------------------

MAX_KEYS = 20
KEY_COLUMNS = "id, name, prefix, created_at, last_used_at, expires_at"


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    days: int | None = Field(default=None, ge=1, le=730)


@router.get("/api-keys")
def list_keys(user: UserDep) -> list[dict]:
    """Your own active keys. The keys themselves are never shown again."""
    with db.tx() as conn:
        return conn.execute(
            f"""SELECT {KEY_COLUMNS} FROM api_keys WHERE user_id = %s AND revoked_at IS NULL
                AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC""",
            (user.id,),
        ).fetchall()


@router.post("/api-keys", status_code=201)
def create_key(body: KeyIn, user: UserDep) -> dict:
    """A key that acts as you, for the SDK, Terraform or your own code. Shown once."""
    token = API_KEY_PREFIX + new_token()
    with db.tx() as conn:
        active = conn.execute(
            """SELECT count(*) AS n FROM api_keys WHERE user_id = %s AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > now())""",
            (user.id,),
        ).fetchone()["n"]
        if active >= MAX_KEYS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"You have {MAX_KEYS} keys. Revoke one first.")
        row = conn.execute(
            f"""INSERT INTO api_keys (user_id, name, prefix, token_hash, expires_at)
                VALUES (%s, %s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s) END)
                RETURNING {KEY_COLUMNS}""",
            (user.id, body.name.strip(), token[:12], token_hash(token), body.days, body.days),
        ).fetchone()
        audit.record(conn, user.actor, "api_key.create", row["prefix"], user.customer_id, {"name": row["name"]})
    return {**row, "token": token}


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: int, user: UserDep) -> None:
    with db.tx() as conn:
        row = conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL"
            " RETURNING prefix, name",
            (key_id, user.id),
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found.")
        audit.record(conn, user.actor, "api_key.revoke", row["prefix"], user.customer_id, {"name": row["name"]})
