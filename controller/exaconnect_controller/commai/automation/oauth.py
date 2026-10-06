"""OAuth 2.0 authorisation-code sign-in for connectors, and the access token
a connector uses for each call (ADR 0020).

Client ids and secrets come from the environment. Until Dudley registers
the apps they are empty and the sign-in button explains what is missing:

  Google Calendar  EXA_GOOGLE_CLIENT_ID, EXA_GOOGLE_CLIENT_SECRET
  HubSpot          EXA_HUBSPOT_CLIENT_ID, EXA_HUBSPOT_CLIENT_SECRET
                   (or a private-app token entered on the Integrations screen)
  Both             EXA_PUBLIC_URL, the controller's public https address, used
                   for the redirect URI {EXA_PUBLIC_URL}/api/v1/commai/oauth/{app}/callback

Tokens are stored encrypted (vault.py); the state value is single-use,
expires after 10 minutes and only its hash is stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any

import psycopg

from ..connectors import ConnectorError
from . import http, vault

STATE_TTL = dt.timedelta(minutes=10)


@dataclass(frozen=True)
class Provider:
    app: str
    label: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    env: str  # prefix of the client id/secret variables
    extra: tuple[tuple[str, str], ...] = ()


PROVIDERS = {
    "google_calendar": Provider(
        "google_calendar",
        "Google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        (
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.freebusy",
        ),
        "EXA_GOOGLE",
        (("access_type", "offline"), ("prompt", "consent"), ("include_granted_scopes", "true")),
    ),
    "hubspot": Provider(
        "hubspot",
        "HubSpot",
        "https://app.hubspot.com/oauth/authorize",
        "https://api.hubapi.com/oauth/v1/token",
        (
            "crm.objects.contacts.read",
            "crm.objects.contacts.write",
            "crm.objects.deals.read",
            "crm.objects.deals.write",
            "tickets",
        ),
        "EXA_HUBSPOT",
    ),
}


class OAuthError(Exception):
    """Sign-in can't go ahead; the message is safe to show."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _client(p: Provider) -> tuple[str, str]:
    return os.environ.get(f"{p.env}_CLIENT_ID", ""), os.environ.get(f"{p.env}_CLIENT_SECRET", "")


def redirect_uri(app: str) -> str:
    base = os.environ.get("EXA_PUBLIC_URL", "").rstrip("/")
    return f"{base}/api/v1/commai/oauth/{app}/callback"


def ready(app: str) -> tuple[bool, str]:
    """Whether sign-in can start, and if not, what is missing (for the screen)."""
    p = PROVIDERS.get(app)
    if p is None:
        return False, "This app does not use a sign-in."
    cid, secret = _client(p)
    missing = [n for n, v in ((f"{p.env}_CLIENT_ID", cid), (f"{p.env}_CLIENT_SECRET", secret)) if not v]
    if not os.environ.get("EXA_PUBLIC_URL"):
        missing.append("EXA_PUBLIC_URL")
    if not vault.configured():
        missing.append("EXA_SECRETS_KEY")
    if missing:
        return False, f"Not live until ExaCarib registers the {p.label} app: the controller needs {', '.join(missing)}."
    return True, ""


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def start(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> str:
    """Record a single-use state and return the provider's sign-in URL."""
    ok, why = ready(app)
    if not ok:
        raise OAuthError(why, 409)
    p = PROVIDERS[app]
    state = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO commai_oauth_states (state_hash, customer_id, app, actor, expires_at)
           VALUES (%s, %s, %s, %s, now() + %s)""",
        (_hash(state), customer_id, app, actor, STATE_TTL),
    )
    params = {
        "client_id": _client(p)[0],
        "redirect_uri": redirect_uri(app),
        "response_type": "code",
        "scope": " ".join(p.scopes),
        "state": state,
        **dict(p.extra),
    }
    return f"{p.authorize_url}?{urllib.parse.urlencode(params)}"


def _store(conn, connection: dict, tokens: dict, previous: dict | None = None) -> None:
    now = dt.datetime.now(dt.UTC)
    secret = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or (previous or {}).get("refresh_token", ""),
    }
    expires = now + dt.timedelta(seconds=int(tokens.get("expires_in") or 3600))
    ref = vault.put(conn, connection["customer_id"], f"oauth:{connection['app']}", secret, connection["secret_ref"])
    scopes = tokens.get("scope")
    granted = scopes.split() if isinstance(scopes, str) else list(connection.get("granted_scopes") or [])
    conn.execute(
        """UPDATE integration_connections SET secret_ref = %s, auth_status = 'signed_in', auth_method = 'oauth',
                  token_expires_at = %s, granted_scopes = %s, updated_at = now()
           WHERE id = %s""",
        (ref, expires, granted, connection["id"]),
    )


def callback(conn: psycopg.Connection, app: str, code: str, state: str) -> dict:
    """Finish sign-in: check the state, swap the code for tokens, store them encrypted.
    Returns the connection row."""
    row = conn.execute(
        "SELECT * FROM commai_oauth_states WHERE state_hash = %s FOR UPDATE", (_hash(state or ""),)
    ).fetchone()
    if row is None or row["app"] != app:
        raise OAuthError("This sign-in link is not valid. Start again from the Integrations screen.", 400)
    if row["used_at"] is not None or row["expires_at"] < dt.datetime.now(dt.UTC):
        raise OAuthError("This sign-in link has expired or was already used. Start again.", 400)
    conn.execute("UPDATE commai_oauth_states SET used_at = now() WHERE state_hash = %s", (row["state_hash"],))
    p = PROVIDERS[app]
    cid, secret = _client(p)
    try:
        r = http.request(
            "POST",
            p.token_url,
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(app),
                "client_id": cid,
                "client_secret": secret,
            },
        )
    except http.NetworkError as e:
        raise OAuthError(f"{p.label} could not be reached to finish sign-in.", 502) from e
    if r.status != 200 or not isinstance(r.body, dict) or "access_token" not in r.body:
        err = r.body.get("error", "") if isinstance(r.body, dict) else ""
        raise OAuthError(f"{p.label} refused the sign-in ({r.status} {err}).".replace(" )", ")"), 502)
    connection = conn.execute(
        "SELECT * FROM integration_connections WHERE customer_id = %s AND app = %s FOR UPDATE",
        (row["customer_id"], app),
    ).fetchone()
    if connection is None:
        connection = conn.execute(
            "INSERT INTO integration_connections (customer_id, app) VALUES (%s, %s) RETURNING *",
            (row["customer_id"], app),
        ).fetchone()
    _store(conn, connection, r.body)
    return conn.execute(
        """UPDATE integration_connections SET
                  status = CASE WHEN status IN ('draft', 'broken') THEN 'authorised' ELSE status END,
                  last_cause = CASE WHEN last_cause = 'expired_signin' THEN '' ELSE last_cause END
           WHERE id = %s RETURNING *""",
        (connection["id"],),
    ).fetchone()


def access_token(conn: psycopg.Connection, connection: dict) -> str:
    """A current access token for the connection, refreshing it when it is
    about to expire. Raises ConnectorError('expired_signin') when sign-in is needed."""
    try:
        secret = vault.get(conn, connection["customer_id"], connection.get("secret_ref") or "")
    except vault.VaultError as e:
        raise ConnectorError(str(e), "expired_signin") from None
    if not secret:
        raise ConnectorError("Not signed in.", "expired_signin")
    if connection.get("auth_method") == "token":
        return secret["token"]
    exp = connection.get("token_expires_at")
    if exp and exp > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60):
        return secret["access_token"]
    p = PROVIDERS[connection["app"]]
    if not secret.get("refresh_token"):
        raise ConnectorError(f"The {p.label} sign-in has expired.", "expired_signin")
    cid, csecret = _client(p)
    try:
        r = http.request(
            "POST",
            p.token_url,
            form={
                "grant_type": "refresh_token",
                "refresh_token": secret["refresh_token"],
                "client_id": cid,
                "client_secret": csecret,
            },
        )
    except http.NetworkError as e:
        raise ConnectorError(str(e), "provider") from None
    if (
        r.status in (400, 401)
        and isinstance(r.body, dict)
        and r.body.get("error")
        in (
            "invalid_grant",
            "invalid_client",
            "unauthorized_client",
            "BAD_REFRESH_TOKEN",
        )
    ):
        conn.execute("UPDATE integration_connections SET auth_status = 'expired' WHERE id = %s", (connection["id"],))
        raise ConnectorError(f"The {p.label} sign-in has expired or was revoked.", "expired_signin")
    if r.status != 200 or not isinstance(r.body, dict) or "access_token" not in r.body:
        raise ConnectorError(f"{p.label} answered {r.status} when renewing the sign-in.", "provider")
    _store(conn, connection, r.body, secret)
    return r.body["access_token"]


def set_token(conn: psycopg.Connection, connection: dict, token: str) -> None:
    """A private-app token (HubSpot), entered through secure entry."""
    ref = vault.put(
        conn,
        connection["customer_id"],
        f"token:{connection['app']}",
        {"token": token},
        connection.get("secret_ref") or "",
    )
    conn.execute(
        """UPDATE integration_connections SET secret_ref = %s, auth_status = 'signed_in', auth_method = 'token',
                  token_expires_at = NULL, updated_at = now(),
                  status = CASE WHEN status IN ('draft', 'broken') THEN 'authorised' ELSE status END,
                  last_cause = CASE WHEN last_cause = 'expired_signin' THEN '' ELSE last_cause END
           WHERE id = %s""",
        (ref, connection["id"]),
    )


def sign_out(conn: psycopg.Connection, connection: dict) -> None:
    vault.delete(conn, connection["customer_id"], connection.get("secret_ref") or "")
    conn.execute(
        """UPDATE integration_connections SET secret_ref = '', auth_status = 'none', auth_method = '',
                  token_expires_at = NULL, status = 'draft', updated_at = now() WHERE id = %s""",
        (connection["id"],),
    )
