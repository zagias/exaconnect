"""OAuth 2.0 for partner apps (ADR 0025): authorisation code with PKCE.

- A partner registers an app (client) with its redirect URIs and the scopes it
  may ask for (the existing API scopes, access.SCOPES).
- Someone at a business approves the app on the portal's consent page. Only
  S256 PKCE is accepted, and the code is single use, ten minutes, bound to the
  client, the redirect URI and the challenge.
- The access token is an API key (api_keys row) that expires after an hour,
  with the granted scopes: every existing scope and tenant check applies to it.
  Granted scopes are what was asked, within the client's and the person's own.
- The refresh token rotates on every use; replaying an old one revokes the
  grant. Tokens can be revoked by the app (RFC 7009) or by the business.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import secrets
import urllib.parse
from typing import Any

import psycopg

from ..api.deps import API_KEY_PREFIX
from ..security import hash_password, token_hash, verify_password
from .access import SCOPES

CODE_TTL = dt.timedelta(minutes=10)
ACCESS_TTL_S = 3600
REFRESH_TTL = dt.timedelta(days=30)
REFRESH_PREFIX = "exr_"
_PKCE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class OAuthError(Exception):
    """error is the RFC 6749 code (invalid_request, invalid_grant, ...)."""

    def __init__(self, error: str, description: str, code: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.code = code


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def check_redirect(uri: str) -> str:
    u = urllib.parse.urlparse(uri)
    local = u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")
    if not (u.scheme == "https" or local) or not u.hostname or u.fragment or len(uri) > 500:
        raise OAuthError("invalid_request", "Redirect URIs use https (or http://localhost) and have no #fragment.", 422)
    return uri


def parse_scopes(raw: str | list[str] | None) -> list[str]:
    items = raw.split() if isinstance(raw, str) else list(raw or [])
    bad = sorted(set(items) - set(SCOPES))
    if bad:
        raise OAuthError("invalid_scope", f"Unknown scopes: {', '.join(bad)}.")
    return sorted(set(items))


# ---- clients ---------------------------------------------------------------------------------


def register(
    conn: psycopg.Connection,
    partner_id: Any,
    name: str,
    redirect_uris: list[str],
    scopes: list[str],
    confidential: bool,
    actor: str,
) -> dict:
    uris = [check_redirect(u) for u in redirect_uris]
    if not uris:
        raise OAuthError("invalid_request", "Give at least one redirect URI.", 422)
    scopes = parse_scopes(scopes)
    if not scopes:
        raise OAuthError("invalid_scope", "Give at least one scope.", 422)
    client_id = "oc_" + secrets.token_urlsafe(16)
    secret = "ocs_" + secrets.token_urlsafe(32) if confidential else ""
    row = conn.execute(
        """INSERT INTO commai_oauth_clients (client_id, partner_id, name, redirect_uris, scopes, confidential,
             secret_hash, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING client_id, partner_id, name, redirect_uris, scopes, confidential, created_at""",
        (
            client_id,
            partner_id,
            name.strip(),
            uris,
            scopes,
            confidential,
            hash_password(secret) if secret else "",
            actor,
        ),
    ).fetchone()
    return {**row, "client_secret": secret or None}


def client(conn: psycopg.Connection, client_id: str) -> dict:
    row = conn.execute(
        """SELECT c.*, p.name AS partner_name FROM commai_oauth_clients c JOIN commai_partners p ON p.id = c.partner_id
           WHERE c.client_id = %s AND c.revoked_at IS NULL AND p.active""",
        (client_id or "",),
    ).fetchone()
    if row is None:
        raise OAuthError("invalid_client", "Unknown or withdrawn app.", 401)
    return row


def authenticate(conn: psycopg.Connection, client_id: str, client_secret: str | None) -> dict:
    c = client(conn, client_id)
    if c["confidential"] and not (client_secret and verify_password(client_secret, c["secret_hash"])):
        raise OAuthError("invalid_client", "The app's secret is wrong.", 401)
    return c


# ---- authorisation ----------------------------------------------------------------------------


def check_request(
    conn: psycopg.Connection,
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    code_challenge_method: str,
    response_type: str = "code",
) -> tuple[dict, list[str]]:
    c = client(conn, client_id)
    if redirect_uri not in c["redirect_uris"]:
        # Never redirect to an unregistered address.
        raise OAuthError("invalid_request", "That redirect URI is not registered for this app.")
    if response_type != "code":
        raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
    if not code_challenge:
        raise OAuthError("invalid_request", "PKCE is required: send code_challenge with method S256.")
    if code_challenge_method != "S256":
        raise OAuthError("invalid_request", "Only the S256 code challenge method is accepted.")
    if not _CHALLENGE.match(code_challenge):
        raise OAuthError("invalid_request", "The code challenge is not a base64url SHA-256.")
    requested = parse_scopes(scope)
    if not requested:
        raise OAuthError("invalid_scope", "Ask for at least one scope.")
    over = sorted(set(requested) - set(c["scopes"]))
    if over:
        raise OAuthError("invalid_scope", f"This app may not ask for {', '.join(over)}.")
    return c, requested


def issue_code(
    conn: psycopg.Connection,
    c: dict,
    user: Any,
    requested: list[str],
    redirect_uri: str,
    code_challenge: str,
) -> tuple[str, list[str]]:
    own = set(SCOPES) if user.scopes is None else set(user.scopes)
    granted = sorted(set(requested) & own)
    if not granted:
        raise OAuthError("access_denied", "Your account has none of the scopes this app asks for.", 403)
    code = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO commai_oauth_codes (code_hash, client_id, user_id, customer_id, scopes, redirect_uri,
             code_challenge, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s)""",
        (token_hash(code), c["client_id"], user.id, user.customer_id, granted, redirect_uri, code_challenge, CODE_TTL),
    )
    return code, granted


def _access_key(conn: psycopg.Connection, grant: dict, c: dict) -> tuple[str, int]:
    token = API_KEY_PREFIX + secrets.token_urlsafe(32)
    row = conn.execute(
        """INSERT INTO api_keys (user_id, name, prefix, token_hash, scopes, expires_at)
           VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s)) RETURNING id""",
        (grant["user_id"], f"OAuth: {c['name']}"[:100], token[:12], token_hash(token), list(grant["scopes"]),
         ACCESS_TTL_S),
    ).fetchone()  # fmt: skip
    return token, row["id"]


def _tokens(access: str, refresh: str, scopes: list[str]) -> dict:
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_S,
        "refresh_token": refresh,
        "scope": " ".join(scopes),
    }


def exchange_code(conn: psycopg.Connection, c: dict, code: str, redirect_uri: str, code_verifier: str) -> dict:
    row = conn.execute(
        "SELECT * FROM commai_oauth_codes WHERE code_hash = %s AND client_id = %s FOR UPDATE",
        (token_hash(code or ""), c["client_id"]),
    ).fetchone()
    if row is None:
        raise OAuthError("invalid_grant", "Unknown code.")
    if row["used_at"] is not None:
        # A code used twice: revoke whatever it produced (RFC 6749 section 4.1.2).
        if row["grant_id"]:
            revoke_grant(conn, row["grant_id"], "code replay")
        raise OAuthError("invalid_grant", "This code has already been used.")
    if row["expires_at"] < dt.datetime.now(dt.UTC):
        raise OAuthError("invalid_grant", "This code has expired.")
    if row["redirect_uri"] != redirect_uri:
        raise OAuthError("invalid_grant", "The redirect URI does not match the one used to get the code.")
    if not code_verifier or not _PKCE.match(code_verifier):
        raise OAuthError("invalid_request", "PKCE is required: send the code_verifier (43 to 128 characters).")
    if not hmac.compare_digest(s256(code_verifier), row["code_challenge"]):
        raise OAuthError("invalid_grant", "The code verifier does not match the challenge.")
    user = conn.execute("SELECT disabled_at FROM users WHERE id = %s", (row["user_id"],)).fetchone()
    if user is None or user["disabled_at"] is not None:
        raise OAuthError("invalid_grant", "The account that approved this app is switched off.")
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    grant = conn.execute(
        """INSERT INTO commai_oauth_grants (client_id, user_id, customer_id, scopes, refresh_hash, expires_at)
           VALUES (%s, %s, %s, %s, %s, now() + %s) RETURNING *""",
        (c["client_id"], row["user_id"], row["customer_id"], list(row["scopes"]), token_hash(refresh), REFRESH_TTL),
    ).fetchone()
    access, key_id = _access_key(conn, grant, c)
    conn.execute("UPDATE commai_oauth_grants SET access_key_id = %s WHERE id = %s", (key_id, grant["id"]))
    conn.execute(
        "UPDATE commai_oauth_codes SET used_at = now(), grant_id = %s WHERE code_hash = %s",
        (grant["id"], row["code_hash"]),
    )
    return _tokens(access, refresh, list(grant["scopes"])) | {"_grant": grant}


def refresh(conn: psycopg.Connection, c: dict, refresh_token: str, scope: str | None) -> dict:
    h = token_hash(refresh_token or "")
    grant = conn.execute(
        """SELECT * FROM commai_oauth_grants WHERE client_id = %s
           AND (refresh_hash = %s OR previous_refresh_hash = %s) FOR UPDATE""",
        (c["client_id"], h, h),
    ).fetchone()
    if grant is None or grant["revoked_at"] is not None:
        raise OAuthError("invalid_grant", "Unknown or revoked refresh token.")
    if grant["refresh_hash"] != h:
        # An old refresh token came back: someone may have copied it. End the grant.
        revoke_grant(conn, grant["id"], "refresh token replay")
        raise OAuthError("invalid_grant", "This refresh token has already been used; the app must ask again.")
    if grant["expires_at"] < dt.datetime.now(dt.UTC):
        raise OAuthError("invalid_grant", "This refresh token has expired.")
    scopes = list(grant["scopes"])
    if scope:
        narrower = parse_scopes(scope)
        if set(narrower) - set(scopes):
            raise OAuthError("invalid_scope", "A refresh can't add scopes.")
        scopes = narrower
    user = conn.execute("SELECT disabled_at, access_scopes FROM users WHERE id = %s", (grant["user_id"],)).fetchone()
    if user is None or user["disabled_at"] is not None:
        revoke_grant(conn, grant["id"], "account switched off")
        raise OAuthError("invalid_grant", "The account that approved this app is switched off.")
    if grant["access_key_id"]:
        conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (grant["access_key_id"],)
        )
    new_refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    grant = conn.execute(
        """UPDATE commai_oauth_grants SET previous_refresh_hash = refresh_hash, refresh_hash = %s, scopes = %s,
             refreshed_at = now() WHERE id = %s RETURNING *""",
        (token_hash(new_refresh), scopes, grant["id"]),
    ).fetchone()
    access, key_id = _access_key(conn, grant, c)
    conn.execute("UPDATE commai_oauth_grants SET access_key_id = %s WHERE id = %s", (key_id, grant["id"]))
    return _tokens(access, new_refresh, scopes)


def revoke_grant(conn: psycopg.Connection, grant_id: Any, by: str) -> None:
    g = conn.execute(
        """UPDATE commai_oauth_grants SET revoked_at = coalesce(revoked_at, now()), revoked_by = %s
           WHERE id = %s RETURNING access_key_id""",
        (by, grant_id),
    ).fetchone()
    if g and g["access_key_id"]:
        conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (g["access_key_id"],)
        )


def revoke_token(conn: psycopg.Connection, c: dict, token: str) -> None:
    """RFC 7009: a refresh or access token of this app. Unknown tokens are not an error."""
    h = token_hash(token or "")
    g = conn.execute(
        """SELECT g.id FROM commai_oauth_grants g LEFT JOIN api_keys k ON k.id = g.access_key_id
           WHERE g.client_id = %s AND (g.refresh_hash = %s OR k.token_hash = %s)""",
        (c["client_id"], h, h),
    ).fetchone()
    if g:
        revoke_grant(conn, g["id"], f"app:{c['client_id']}")
