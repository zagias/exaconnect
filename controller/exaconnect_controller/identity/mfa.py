"""Two-step sign-in for local accounts: TOTP authenticator apps and one-time
recovery codes (stored hashed), plus the short-lived sign-in challenge."""

from __future__ import annotations

import secrets
from typing import Any

import psycopg

from ..security import hash_password, new_token, token_hash, verify_password
from . import totp

RECOVERY_CODES = 10
CHALLENGE_MINUTES = 5
CHALLENGE_ATTEMPTS = 5
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i


def new_recovery_codes(conn: psycopg.Connection, user_id: Any) -> list[str]:
    """Replace any earlier codes with ten new ones. Shown once."""
    conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id = %s", (user_id,))
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        code = f"{raw[:5]}-{raw[5:]}"
        conn.execute(
            "INSERT INTO mfa_recovery_codes (user_id, code_hash) VALUES (%s, %s)", (user_id, hash_password(code))
        )
        codes.append(code)
    return codes


def recovery_left(conn: psycopg.Connection, user_id: Any) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM mfa_recovery_codes WHERE user_id = %s AND used_at IS NULL", (user_id,)
    ).fetchone()["n"]


def check_code(conn: psycopg.Connection, user: dict, code: str) -> str | None:
    """'totp' or 'recovery' when the code is right (and uses it up), else None."""
    code = (code or "").strip()
    compact = code.replace(" ", "")
    if compact.isdigit():
        if not user.get("totp_secret"):
            return None
        step = totp.verify(user["totp_secret"], compact, after_step=user.get("totp_last_step"))
        if step is None:
            return None
        conn.execute("UPDATE users SET totp_last_step = %s WHERE id = %s", (step, user["id"]))
        return "totp"
    norm = code.lower().replace(" ", "")
    if len(norm) == 10:
        norm = f"{norm[:5]}-{norm[5:]}"
    rows = conn.execute(
        "SELECT id, code_hash FROM mfa_recovery_codes WHERE user_id = %s AND used_at IS NULL FOR UPDATE",
        (user["id"],),
    ).fetchall()
    for r in rows:
        if verify_password(norm, r["code_hash"]):
            conn.execute("UPDATE mfa_recovery_codes SET used_at = now() WHERE id = %s", (r["id"],))
            return "recovery"
    return None


def new_challenge(conn: psycopg.Connection, user_id: Any) -> str:
    token = new_token()
    conn.execute("DELETE FROM mfa_challenges WHERE user_id = %s AND expires_at < now()", (user_id,))
    conn.execute(
        "INSERT INTO mfa_challenges (token_hash, user_id, expires_at)"
        f" VALUES (%s, %s, now() + interval '{CHALLENGE_MINUTES} minutes')",
        (token_hash(token), user_id),
    )
    return token


def open_challenge(conn: psycopg.Connection, token: str) -> dict | None:
    """The challenge's user, counting this attempt; None once it is used, expired or worn out."""
    row = conn.execute(
        """UPDATE mfa_challenges SET attempts = attempts + 1
           WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() AND attempts < %s
           RETURNING user_id""",
        (token_hash(token or ""), CHALLENGE_ATTEMPTS),
    ).fetchone()
    return row


def close_challenge(conn: psycopg.Connection, token: str) -> None:
    conn.execute("UPDATE mfa_challenges SET used_at = now() WHERE token_hash = %s", (token_hash(token),))


def peek_challenge(conn: psycopg.Connection, token: str) -> dict | None:
    """The challenge's user without counting an attempt (to fetch passkey options)."""
    return conn.execute(
        """SELECT user_id FROM mfa_challenges
           WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() AND attempts < %s""",
        (token_hash(token or ""), CHALLENGE_ATTEMPTS),
    ).fetchone()


def two_step_on(conn: psycopg.Connection, user: dict) -> bool:
    if user.get("totp_enabled_at") is not None:
        return True
    return conn.execute("SELECT 1 FROM passkeys WHERE user_id = %s LIMIT 1", (user["id"],)).fetchone() is not None


def methods(conn: psycopg.Connection, user: dict) -> list[str]:
    out = ["totp"] if user.get("totp_enabled_at") is not None else []
    if conn.execute("SELECT 1 FROM passkeys WHERE user_id = %s LIMIT 1", (user["id"],)).fetchone():
        out.append("passkey")
    if recovery_left(conn, user["id"]):
        out.append("recovery")
    return out
