"""Portal sign-in with local accounts (CLAUDE.md §4.2). SSO can replace this later."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from .. import audit, db
from ..security import new_token, token_hash, verify_password
from .deps import UserDep

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


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
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (body.email,)).fetchone()
        if row is None or not verify_password(body.password, row["password_hash"]):
            audit.record(conn, f"user:{body.email}", "login_failed")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That email and password don't match.")
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
