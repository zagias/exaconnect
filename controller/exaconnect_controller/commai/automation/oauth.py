"""OAuth 2.0 authorisation-code sign-in for connectors, and the access token
a connector uses for each call (ADR 0020, ADR 0028).

Client ids and secrets come from the environment. Until Dudley registers
the apps they are empty and the sign-in button explains what is missing:

  Google Calendar, Gmail  EXA_GOOGLE_CLIENT_ID, EXA_GOOGLE_CLIENT_SECRET
  HubSpot          EXA_HUBSPOT_CLIENT_ID, EXA_HUBSPOT_CLIENT_SECRET
                   (or a private-app token entered on the Integrations screen)
  Microsoft 365    EXA_MS365_CLIENT_ID, EXA_MS365_CLIENT_SECRET (Entra ID app, multi-tenant)
  Dynamics 365     EXA_DYNAMICS_CLIENT_ID, EXA_DYNAMICS_CLIENT_SECRET (Entra ID app)
  Salesforce       EXA_SALESFORCE_CLIENT_ID, EXA_SALESFORCE_CLIENT_SECRET (connected app)
  Zoho CRM         EXA_ZOHO_CLIENT_ID, EXA_ZOHO_CLIENT_SECRET (Zoho API console, server-based app)
  Pipedrive        EXA_PIPEDRIVE_CLIENT_ID, EXA_PIPEDRIVE_CLIENT_SECRET (Pipedrive Marketplace app)
  Calendly         EXA_CALENDLY_CLIENT_ID, EXA_CALENDLY_CLIENT_SECRET (Calendly developer app)
  Custom REST      the business's own client id and secret, stored encrypted (ADR 0028)
  All              EXA_PUBLIC_URL, the controller's public https address, used
                   for the redirect URI {EXA_PUBLIC_URL}/api/v1/commai/oauth/{app}/callback

Other modules may add a provider to PROVIDERS (one entry) or a resolver for
providers made at run time. Provider URLs and scopes may name a connection
setting in braces ({tenant}, {subdomain}, {shop}, {org_host}...), filled from
the connection's settings when sign-in starts.

Tokens are stored encrypted (vault.py); the state value is single-use,
expires after 10 minutes and only its hash is stored.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import urllib.parse
from collections.abc import Callable
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
    scope_sep: str = " "  # some providers want comma-separated scopes
    expiring: bool = True  # False: tokens without expires_in never expire
    token_auth: str = "form"  # "form": client id/secret in the body; "basic": in an Authorization header
    keep: tuple[str, ...] = ()  # token-response fields saved to settings (instance_url, api_domain...)
    defaults: tuple[tuple[str, str], ...] = ()  # default values for {settings} in URLs and scopes
    client_id: str = ""  # a business's own client (custom REST) instead of the environment
    client_secret: str = ""


_GOOGLE_EXTRA = (("access_type", "offline"), ("prompt", "consent"), ("include_granted_scopes", "true"))
_MS = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"

PROVIDERS: dict[str, Provider] = {
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
        _GOOGLE_EXTRA,
    ),
    "gmail": Provider(
        "gmail",
        "Google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        ("https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"),
        "EXA_GOOGLE",
        _GOOGLE_EXTRA,
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
    "microsoft365": Provider(
        "microsoft365",
        "Microsoft",
        _MS + "/authorize",
        _MS + "/token",
        ("offline_access", "User.Read", "Calendars.ReadWrite", "Mail.Send", "Mail.Read"),
        "EXA_MS365",
        (("response_mode", "query"),),
        defaults=(("tenant", "common"),),
    ),
    "dynamics365": Provider(
        "dynamics365",
        "Microsoft Dynamics 365",
        _MS + "/authorize",
        _MS + "/token",
        ("https://{org_host}/user_impersonation", "offline_access"),
        "EXA_DYNAMICS",
        (("response_mode", "query"),),
        defaults=(("tenant", "common"),),
    ),
    "salesforce": Provider(
        "salesforce",
        "Salesforce",
        "https://{login_host}/services/oauth2/authorize",
        "https://{login_host}/services/oauth2/token",
        ("api", "refresh_token"),
        "EXA_SALESFORCE",
        keep=("instance_url",),
        defaults=(("login_host", "login.salesforce.com"),),
    ),
    "zoho_crm": Provider(
        "zoho_crm",
        "Zoho",
        "https://accounts.zoho.{dc}/oauth/v2/auth",
        "https://accounts.zoho.{dc}/oauth/v2/token",
        (
            "ZohoCRM.modules.contacts.ALL",
            "ZohoCRM.modules.leads.ALL",
            "ZohoCRM.modules.deals.ALL",
            "ZohoCRM.coql.READ",
        ),
        "EXA_ZOHO",
        (("access_type", "offline"), ("prompt", "consent")),
        scope_sep=",",
        keep=("api_domain",),
        defaults=(("dc", "com"),),
    ),
    "pipedrive": Provider(
        "pipedrive",
        "Pipedrive",
        "https://oauth.pipedrive.com/oauth/authorize",
        "https://oauth.pipedrive.com/oauth/token",
        (),
        "EXA_PIPEDRIVE",
        token_auth="basic",
        keep=("api_domain",),
    ),
    "calendly": Provider(
        "calendly",
        "Calendly",
        "https://auth.calendly.com/oauth/authorize",
        "https://auth.calendly.com/oauth/token",
        (),
        "EXA_CALENDLY",
        keep=("owner", "organization"),
    ),
}

# Providers made at run time (a business's custom REST app, ADR 0028): each
# resolver takes an app name and returns a Provider or None.
resolvers: list[Callable[[str], Provider | None]] = []


def provider(app: str) -> Provider | None:
    p = PROVIDERS.get(app)
    if p is not None:
        return p
    for fn in resolvers:
        p = fn(app)
        if p is not None:
            return p
    return None


class OAuthError(Exception):
    """Sign-in can't go ahead; the message is safe to show."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


_SETTING = re.compile(r"\{([a-z_]+)\}")


def fill(p: Provider, template: str, settings: dict | None) -> str:
    """Fill {tenant}, {subdomain}, {org_host}... from the connection's settings."""
    names = _SETTING.findall(template)
    if not names:
        return template
    s = {**dict(p.defaults), **{k: str(v) for k, v in (settings or {}).items() if isinstance(v, str | int)}}
    missing = [n for n in names if not s.get(n)]
    if missing:
        raise OAuthError(f"Set {', '.join(missing)} in the {p.label} settings before signing in.", 409)
    for n in names:
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,120}", s[n]):
            raise OAuthError(f"The {n} setting is not valid.", 422)
    return _SETTING.sub(lambda m: s[m.group(1)], template)


def _client(p: Provider) -> tuple[str, str]:
    if p.client_id:
        return p.client_id, p.client_secret
    return os.environ.get(f"{p.env}_CLIENT_ID", ""), os.environ.get(f"{p.env}_CLIENT_SECRET", "")


def env_names(app: str) -> list[str]:
    """The environment settings Dudley creates for this app's sign-in."""
    p = provider(app)
    if p is None or p.client_id:
        return []
    return [f"{p.env}_CLIENT_ID", f"{p.env}_CLIENT_SECRET"]


def redirect_uri(app: str) -> str:
    base = os.environ.get("EXA_PUBLIC_URL", "").rstrip("/")
    return f"{base}/api/v1/commai/oauth/{app}/callback"


def ready(app: str) -> tuple[bool, str]:
    """Whether sign-in can start, and if not, what is missing (for the screen)."""
    p = provider(app)
    if p is None:
        return False, "This app does not use a sign-in."
    cid, secret = _client(p)
    missing = [n for n, v in zip(env_names(app), (cid, secret), strict=False) if not v]
    if not os.environ.get("EXA_PUBLIC_URL"):
        missing.append("EXA_PUBLIC_URL")
    if not vault.configured():
        missing.append("EXA_SECRETS_KEY")
    if missing:
        return False, f"Not live until ExaCarib registers the {p.label} app: the controller needs {', '.join(missing)}."
    return True, ""


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _settings_of(conn, customer_id: Any, app: str) -> dict:
    row = conn.execute(
        "SELECT settings FROM integration_connections WHERE customer_id = %s AND app = %s", (customer_id, app)
    ).fetchone()
    return (row or {}).get("settings") or {}


def start(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> str:
    """Record a single-use state and return the provider's sign-in URL."""
    ok, why = ready(app)
    if not ok:
        raise OAuthError(why, 409)
    p = provider(app)
    settings = _settings_of(conn, customer_id, app)
    authorize = fill(p, p.authorize_url, settings)
    scopes = [fill(p, s, settings) for s in p.scopes]
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
        "state": state,
        **dict(p.extra),
    }
    if scopes:
        params["scope"] = p.scope_sep.join(scopes)
    return f"{authorize}?{urllib.parse.urlencode(params)}"


def _token_request(p: Provider, token_url: str, form: dict) -> http.Response:
    cid, secret = _client(p)
    headers = {}
    if p.token_auth == "basic":
        headers["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()
    else:
        form = {**form, "client_id": cid, "client_secret": secret}
    return http.request("POST", token_url, form=form, headers=headers)


def _store(conn, connection: dict, tokens: dict, previous: dict | None = None) -> None:
    now = dt.datetime.now(dt.UTC)
    secret = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or (previous or {}).get("refresh_token", ""),
    }
    p = provider(connection["app"])
    if p is not None and not p.expiring and not tokens.get("expires_in"):
        expires = None  # offline tokens that never expire
    else:
        expires = now + dt.timedelta(seconds=int(tokens.get("expires_in") or 3600))
    ref = vault.put(conn, connection["customer_id"], f"oauth:{connection['app']}", secret, connection["secret_ref"])
    scopes = tokens.get("scope")
    granted = (
        scopes.replace(",", " ").split() if isinstance(scopes, str) else list(connection.get("granted_scopes") or [])
    )
    kept = {k: str(tokens[k]) for k in (p.keep if p else ()) if isinstance(tokens.get(k), str) and tokens.get(k)}
    conn.execute(
        """UPDATE integration_connections SET secret_ref = %s, auth_status = 'signed_in', auth_method = 'oauth',
                  token_expires_at = %s, granted_scopes = %s, updated_at = now(),
                  settings = settings || %s::jsonb
           WHERE id = %s""",
        (ref, expires, granted, json.dumps(kept), connection["id"]),
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
    p = provider(app)
    if p is None:
        raise OAuthError("This app does not use a sign-in.", 400)
    token_url = fill(p, p.token_url, _settings_of(conn, row["customer_id"], app))
    try:
        r = _token_request(
            p,
            token_url,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(app)},
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
    was_simulated = connection.get("auth_method") == "simulated"
    _store(conn, connection, r.body)
    if was_simulated:
        _leave_simulation(conn, connection)
    return conn.execute(
        """UPDATE integration_connections SET
                  status = CASE WHEN status IN ('draft', 'broken') THEN 'authorised' ELSE status END,
                  last_cause = CASE WHEN last_cause = 'expired_signin' THEN '' ELSE last_cause END
           WHERE id = %s RETURNING *""",
        (connection["id"],),
    ).fetchone()


def _leave_simulation(conn, connection: dict) -> None:
    """Moving from the stand-in to the real app starts the checks again:
    test and approve before it is live (ADR 0028)."""
    conn.execute(
        """UPDATE integration_connections SET status = 'authorised', approved_at = NULL, approved_by = '',
                  last_test_result = '{}', last_test_at = NULL WHERE id = %s""",
        (connection["id"],),
    )


def access_token(conn: psycopg.Connection, connection: dict) -> str:
    """A current access token for the connection, refreshing it when it is
    about to expire. Raises ConnectorError('expired_signin') when sign-in is needed."""
    try:
        secret = vault.get(conn, connection["customer_id"], connection.get("secret_ref") or "")
    except vault.VaultError as e:
        raise ConnectorError(str(e), "expired_signin") from None
    if not secret:
        raise ConnectorError("Not signed in.", "expired_signin")
    if connection.get("auth_method") in ("token", "credentials"):
        return secret.get("token") or secret.get("access_token") or ""
    exp = connection.get("token_expires_at")
    if exp and exp > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60):
        return secret["access_token"]
    p = provider(connection["app"])
    if p is None:
        raise ConnectorError("Not signed in.", "expired_signin")
    if exp is None and not p.expiring and secret.get("access_token"):
        return secret["access_token"]
    if not secret.get("refresh_token"):
        raise ConnectorError(f"The {p.label} sign-in has expired.", "expired_signin")
    try:
        token_url = fill(p, p.token_url, connection.get("settings"))
    except OAuthError as e:
        raise ConnectorError(str(e), "mapping") from None
    try:
        r = _token_request(p, token_url, {"grant_type": "refresh_token", "refresh_token": secret["refresh_token"]})
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
            "invalid_code",
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


def set_credentials(conn: psycopg.Connection, connection: dict, creds: dict) -> None:
    """Credentials a business enters (API key, user name and app password,
    client id and secret), stored encrypted (ADR 0028)."""
    if connection.get("auth_method") == "simulated":
        _leave_simulation(conn, connection)
    ref = vault.put(
        conn, connection["customer_id"], f"creds:{connection['app']}", creds, connection.get("secret_ref") or ""
    )
    conn.execute(
        """UPDATE integration_connections SET secret_ref = %s, auth_status = 'signed_in',
                  auth_method = 'credentials', token_expires_at = NULL, updated_at = now(),
                  status = CASE WHEN status IN ('draft', 'broken') THEN 'authorised' ELSE status END,
                  last_cause = CASE WHEN last_cause = 'expired_signin' THEN '' ELSE last_cause END
           WHERE id = %s""",
        (ref, connection["id"]),
    )


def credentials(conn: psycopg.Connection, connection: dict) -> dict:
    """The decrypted credentials of a connection (never returned by the API)."""
    try:
        secret = vault.get(conn, connection["customer_id"], connection.get("secret_ref") or "")
    except vault.VaultError as e:
        raise ConnectorError(str(e), "expired_signin") from None
    if not secret:
        raise ConnectorError("Not signed in.", "expired_signin")
    return secret


def sign_out(conn: psycopg.Connection, connection: dict) -> None:
    vault.delete(conn, connection["customer_id"], connection.get("secret_ref") or "")
    conn.execute(
        """UPDATE integration_connections SET secret_ref = '', auth_status = 'none', auth_method = '',
                  token_expires_at = NULL, status = 'draft', updated_at = now() WHERE id = %s""",
        (connection["id"],),
    )
