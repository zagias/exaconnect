"""Passkeys (WebAuthn) as the second sign-in step, using the Duo Labs `webauthn`
library. If the library is missing, passkeys report themselves unavailable."""

from __future__ import annotations

import json
import secrets
import urllib.parse
from typing import Any

import psycopg

from ..security import new_token, token_hash

try:  # optional: the controller still runs without it
    import webauthn
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

CHALLENGE_MINUTES = 5
MAX_PER_USER = 10


class PasskeyError(ValueError):
    """Safe to show."""


def rp(settings, request) -> tuple[str, str]:
    """(rp_id, origin): the portal's public address, or the address this request came to."""
    if settings.public_url:
        origin = settings.public_url
    else:
        origin = f"{request.headers.get('x-forwarded-proto') or request.url.scheme}://{request.headers.get('host')}"
    return urllib.parse.urlparse(origin).hostname or "localhost", origin.rstrip("/")


def _store_challenge(conn: psycopg.Connection, user_id: Any, purpose: str, challenge: bytes) -> str:
    ticket = new_token()
    conn.execute("DELETE FROM passkey_challenges WHERE user_id = %s AND expires_at < now()", (user_id,))
    conn.execute(
        "INSERT INTO passkey_challenges (token_hash, user_id, purpose, challenge, expires_at)"
        f" VALUES (%s, %s, %s, %s, now() + interval '{CHALLENGE_MINUTES} minutes')",
        (token_hash(ticket), user_id, purpose, challenge),
    )
    return ticket


def _take_challenge(conn: psycopg.Connection, ticket: str, purpose: str) -> dict:
    row = conn.execute(
        """UPDATE passkey_challenges SET used_at = now()
           WHERE token_hash = %s AND purpose = %s AND used_at IS NULL AND expires_at > now()
           RETURNING user_id, challenge""",
        (token_hash(ticket or ""), purpose),
    ).fetchone()
    if row is None:
        raise PasskeyError("That passkey request has expired. Try again.")
    return row


def _descriptors(conn: psycopg.Connection, user_id: Any) -> list:
    rows = conn.execute("SELECT credential_id FROM passkeys WHERE user_id = %s", (user_id,)).fetchall()
    return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"])) for r in rows]


def count(conn: psycopg.Connection, user_id: Any) -> int:
    return conn.execute("SELECT count(*) AS n FROM passkeys WHERE user_id = %s", (user_id,)).fetchone()["n"]


def registration_options(conn: psycopg.Connection, user: dict, rp_id: str) -> tuple[str, dict]:
    if count(conn, user["id"]) >= MAX_PER_USER:
        raise PasskeyError(f"You have {MAX_PER_USER} passkeys. Remove one first.")
    opts = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name="ExaCarib",
        user_name=user["email"],
        user_id=user["id"].bytes,
        user_display_name=user.get("display_name") or user["email"],
        challenge=secrets.token_bytes(32),
        exclude_credentials=_descriptors(conn, user["id"]),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.PREFERRED
        ),
    )
    ticket = _store_challenge(conn, user["id"], "register", opts.challenge)
    return ticket, json.loads(webauthn.options_to_json(opts))


def register(
    conn: psycopg.Connection, user_id: Any, ticket: str, credential: dict, name: str, rp_id: str, origin: str
) -> dict:
    ch = _take_challenge(conn, ticket, "register")
    if str(ch["user_id"]) != str(user_id):
        raise PasskeyError("That passkey request belongs to someone else.")
    try:
        v = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=bytes(ch["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
        )
    except Exception as e:  # the library raises several types; none should become a 500
        raise PasskeyError("The passkey could not be verified.") from e
    cred_id = bytes_to_base64url(v.credential_id)
    if conn.execute("SELECT 1 FROM passkeys WHERE credential_id = %s", (cred_id,)).fetchone():
        raise PasskeyError("That passkey is already registered.")
    return conn.execute(
        """INSERT INTO passkeys (user_id, credential_id, public_key, sign_count, name) VALUES (%s, %s, %s, %s, %s)
           RETURNING id, name, created_at, last_used_at""",
        (user_id, cred_id, v.credential_public_key, v.sign_count, (name or "Passkey").strip()[:60]),
    ).fetchone()


def signin_options(conn: psycopg.Connection, user_id: Any, rp_id: str) -> tuple[str, dict]:
    creds = _descriptors(conn, user_id)
    if not creds:
        raise PasskeyError("This account has no passkeys.")
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id,
        challenge=secrets.token_bytes(32),
        allow_credentials=creds,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    ticket = _store_challenge(conn, user_id, "signin", opts.challenge)
    return ticket, json.loads(webauthn.options_to_json(opts))


def verify_signin(
    conn: psycopg.Connection, user_id: Any, ticket: str, credential: dict, rp_id: str, origin: str
) -> None:
    ch = _take_challenge(conn, ticket, "signin")
    if str(ch["user_id"]) != str(user_id):
        raise PasskeyError("That passkey request belongs to someone else.")
    cred_id = str(credential.get("rawId") or credential.get("id") or "")
    row = conn.execute(
        "SELECT * FROM passkeys WHERE credential_id = %s AND user_id = %s FOR UPDATE", (cred_id, user_id)
    ).fetchone()
    if row is None:
        raise PasskeyError("That passkey is not registered to this account.")
    try:
        v = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=bytes(ch["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"],
        )
    except Exception as e:
        raise PasskeyError("The passkey could not be verified.") from e
    conn.execute(
        "UPDATE passkeys SET sign_count = %s, last_used_at = now() WHERE id = %s", (v.new_sign_count, row["id"])
    )
