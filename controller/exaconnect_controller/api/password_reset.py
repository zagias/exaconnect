"""Forgotten passwords: a one-time link by email.

Asking never says whether an account exists. The link works once, for an hour,
and only its hash is stored. It goes out by SMTP when EXA_SMTP_HOST is set;
until then nothing is sent and an admin resets the password instead (Admin,
Users). Accounts whose domain must use single sign-on, and disabled accounts,
get no link. Setting a new password signs the account out everywhere; two-step
sign-in, where it is on, still applies at the next sign-in."""

from __future__ import annotations

import logging
import os
from email.message import EmailMessage
from email.utils import make_msgid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit, db
from ..commai.channels.email import SmtpSender
from ..identity import sso
from ..security import hash_password, new_token, token_hash
from .auth import _client_ip

log = logging.getLogger("exaconnect.auth")
router = APIRouter(prefix="/auth/password-reset", tags=["auth"])

LIFETIME = "1 hour"
MAX_PER_HOUR = 3  # per account, so nobody can flood someone's inbox
MAX_PER_IP = 20
GONE = "This reset link has expired or has already been used. Ask for a new one."


class ResetAskIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetIn(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


def _email_live() -> bool:
    return not SmtpSender.missing()


def _send(email: str, link: str) -> None:
    msg = EmailMessage()
    msg["From"] = os.environ.get("EXA_SMTP_FROM", "no-reply@exacarib.com")
    msg["To"] = email
    msg["Subject"] = "Reset your ExaCarib password"
    msg["Message-ID"] = make_msgid(domain="accounts.exacarib.invalid")
    msg.set_content(
        "Someone asked to reset the password for this ExaCarib account.\n\n"
        f"Choose a new password here (the link works once, for one hour):\n{link}\n\n"
        "If it wasn't you, ignore this email; your password stays as it is.\n"
    )
    SmtpSender().send(None, {"customer_id": None, "id": None}, msg)


@router.post("", status_code=202)
def ask(body: ResetAskIn, request: Request) -> dict:
    """Send a reset link if the address belongs to an account. The answer is
    the same either way."""
    email = body.email.strip().lower()
    ip = _client_ip(request)
    answer = {"emailed": _email_live()}
    with db.tx() as conn:
        by_ip = conn.execute(
            """SELECT count(*) AS n FROM audit_log WHERE action = 'password_reset.ask'
               AND detail->>'ip' = %s AND at > now() - interval '1 hour'""",
            (ip,),
        ).fetchone()["n"]
        if by_ip >= MAX_PER_IP:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests. Try again later.")
        audit.record(conn, f"user:{email}", "password_reset.ask", email, detail={"ip": ip})
        row = conn.execute(
            "SELECT id, email, role FROM users WHERE lower(email) = %s AND disabled_at IS NULL", (email,)
        ).fetchone()
        if row is None or (row["role"] != "admin" and sso.requires_sso(conn, row["email"])):
            return answer
        recent = conn.execute(
            "SELECT count(*) AS n FROM password_resets WHERE user_id = %s AND created_at > now() - interval '1 hour'",
            (row["id"],),
        ).fetchone()["n"]
        if recent >= MAX_PER_HOUR or not answer["emailed"]:
            return answer
        token = new_token()
        # A new link replaces any earlier one.
        conn.execute("UPDATE password_resets SET used_at = now() WHERE user_id = %s AND used_at IS NULL", (row["id"],))
        conn.execute(
            f"""INSERT INTO password_resets (user_id, token_hash, expires_at, ip)
                VALUES (%s, %s, now() + interval '{LIFETIME}', %s)""",
            (row["id"], token_hash(token), ip),
        )
    base = request.app.state.settings.public_url or str(request.base_url).rstrip("/")
    try:
        _send(row["email"], f"{base}/reset/{token}")
    except Exception as e:  # never tell the asker; the log says why
        log.warning("password reset email failed: %s", type(e).__name__)
    return answer


def _open(conn, token: str, lock: bool = False) -> dict:
    row = conn.execute(
        """SELECT r.id, r.user_id, u.email, u.role FROM password_resets r JOIN users u ON u.id = r.user_id
           WHERE r.token_hash = %s AND r.used_at IS NULL AND r.expires_at > now() AND u.disabled_at IS NULL"""
        + (" FOR UPDATE OF r" if lock else ""),
        (token_hash(token),),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, GONE)
    return row


@router.get("/{token}")
def check(token: str) -> dict:
    with db.tx() as conn:
        row = _open(conn, token)
    return {"email": row["email"]}


@router.post("/{token}", status_code=204)
def reset(token: str, body: ResetIn) -> None:
    with db.tx() as conn:
        row = _open(conn, token, lock=True)
        if row["role"] != "admin" and sso.requires_sso(conn, row["email"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account signs in with single sign-on.")
        conn.execute("UPDATE password_resets SET used_at = now() WHERE id = %s", (row["id"],))
        conn.execute(
            "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
            (hash_password(body.password), row["user_id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (row["user_id"],))
        audit.record(conn, f"user:{row['email']}", "user.reset_password", row["email"], detail={"via": "email"})
