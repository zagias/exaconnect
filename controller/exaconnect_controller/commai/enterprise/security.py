"""Security settings per business (ADR 0024).

- Require two-step sign-in for everyone: a person without it can still sign
  in with their password, but the session can only set up two-step sign-in
  until they have. Enterprise SSO sessions are exempt: the company's own
  provider does their second step (ADR 0017).
- Session lifetime: no session of the business's accounts lives longer, from
  the moment it started, whatever the controller's default.
- IP allow-list: the portal and API keys of the business's accounts work only
  from these address ranges (CIDR). Saving a list that leaves out the address
  of the admin saving it is refused, so an admin can't lock themselves out.
  ExaCarib admins are not limited by a business's list.
- SSO sign-out: signing out of a session that came through the gateway also
  ends the gateway session (OpenID Connect RP-initiated logout), and the
  gateway passes it on to the company's provider where that provider
  publishes a logout endpoint (OIDC end_session_endpoint, SAML
  SingleLogoutService).

`guard()` runs on every authenticated request (from api/deps.current_user).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import psycopg
from fastapi import HTTPException, Request, status

from ... import audit, db
from ...identity import mfa
from . import roles

log = logging.getLogger("exaconnect.commai.enterprise")

TWO_STEP_REQUIRED = "Your organisation requires two-step sign-in. Set it up to continue."
IP_BLOCKED = "Your organisation only allows access from its own network addresses."
SESSION_ENDED = "Your session has ended. Sign in again."
KEY_LOCKED = "This API key is locked after unusual use. Ask your administrator to unlock it."

# What a session waiting for two-step set-up may still reach.
ENROL_PATHS = ("/api/v1/auth/two-step", "/api/v1/auth/passkeys")
ENROL_EXACT = ("/api/v1/auth/me", "/api/v1/auth/logout")
MAX_CIDRS = 50
_MD = "urn:oasis:names:tc:SAML:2.0:metadata"


class SecurityError(ValueError):
    """A security setting we refuse. The message is safe to show."""


def client_ip(request: Request) -> str:
    # The public proxy (deploy/) sets X-Forwarded-For; the controller only listens on localhost.
    headers = getattr(request, "headers", None) or {}
    fwd = headers.get("x-forwarded-for", "")
    client = getattr(request, "client", None)
    return fwd.split(",")[0].strip() or (client.host if client else "")


def normalise_cidrs(items: list[str]) -> list[str]:
    out: list[str] = []
    for raw in items:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError as e:
            raise SecurityError(f"{raw} is not an address or range. Use a form like 203.0.113.0/24.") from e
        if net.prefixlen == 0:
            raise SecurityError("A range that allows every address is not an allow-list. Leave the list empty instead.")
        if str(net) not in out:
            out.append(str(net))
    if len(out) > MAX_CIDRS:
        raise SecurityError(f"Use at most {MAX_CIDRS} ranges.")
    return out


def ip_allowed(ip: str, cidrs: list[str]) -> bool:
    if not cidrs:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


def get(conn: psycopg.Connection, customer_id: Any) -> dict:
    row = conn.execute("SELECT * FROM commai_security_settings WHERE customer_id = %s", (customer_id,)).fetchone()
    return row or {
        "customer_id": customer_id,
        "require_two_step": False,
        "session_hours": None,
        "ip_allowlist": [],
        "sso_logout": True,
        "thresholds": {},
        "updated_by": "",
        "updated_at": None,
    }


def is_sso_session(via: str | None) -> bool:
    return (via or "").startswith("sso")


def _enrol_path(path: str) -> bool:
    return path in ENROL_EXACT or any(path == p or path.startswith(p + "/") for p in ENROL_PATHS)


def needs_two_step(conn: psycopg.Connection, settings: dict, row: dict, via: str | None) -> bool:
    """True when this session must set up two-step sign-in before anything else."""
    if not settings["require_two_step"] or is_sso_session(via):
        return False
    return not mfa.two_step_on(conn, row)


_tick_seen = False


def guard(request: Request, user: Any, row: dict) -> None:
    """Business security settings and role permissions for one request."""
    global _tick_seen
    path = str(request.url.path)
    user.path = path
    params = getattr(request, "query_params", None)
    user.query_team = params.get("team_id") if params is not None else None
    if user.role != "customer" or not user.customer_id:
        return
    from . import protect

    ip = client_ip(request)
    refuse: tuple[int, str] | None = None
    side: list[tuple] = []
    with db.tx() as conn:
        app_settings = getattr(getattr(getattr(request, "app", None), "state", None), "settings", None)
        if not _tick_seen and getattr(app_settings, "routing_interval_s", 0) > 0:
            # The background workers run (not in tests): make sure the watch job is queued.
            protect.ensure_tick(conn)
            _tick_seen = True
        s = get(conn, user.customer_id)
        if s["ip_allowlist"] and not ip_allowed(ip, s["ip_allowlist"]):
            refuse = (status.HTTP_403_FORBIDDEN, IP_BLOCKED)
            side.append(("ip_blocked", ip))
        elif user.via == "key":
            locked = row.get("key_locked_until")
            if locked is not None and locked > dt.datetime.now(dt.UTC):
                refuse = (status.HTTP_403_FORBIDDEN, KEY_LOCKED)
            elif row.get("key_id") is not None:
                side.append(("key_seen", row["key_id"], ip))
        else:
            created = row.get("session_created_at")
            hours = s["session_hours"]
            if hours and created is not None and created + dt.timedelta(hours=hours) < dt.datetime.now(dt.UTC):
                refuse = (status.HTTP_401_UNAUTHORIZED, SESSION_ENDED)
                side.append(("session_ended", row.get("session_hash")))
            elif needs_two_step(conn, s, row, row.get("session_via")) and not _enrol_path(path):
                refuse = (status.HTTP_403_FORBIDDEN, TWO_STEP_REQUIRED)
        if refuse is None:
            user.permissions, user.team_permissions = roles.load(conn, user.id, user.customer_id)
    for item in side:
        try:
            with db.tx() as conn:
                if item[0] == "ip_blocked":
                    _record_blocked(conn, user, item[1])
                elif item[0] == "key_seen":
                    protect.key_seen(conn, user.customer_id, item[1], item[2])
                elif item[0] == "session_ended" and item[1]:
                    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (item[1],))
        except Exception:  # noqa: BLE001 - bookkeeping must never decide the request
            log.exception("security bookkeeping failed")
    if refuse:
        raise HTTPException(*refuse)


def _record_blocked(conn: psycopg.Connection, user: Any, ip: str) -> None:
    """At most one audit row per account and address every ten minutes."""
    seen = conn.execute(
        """SELECT 1 FROM audit_log WHERE action = 'security.ip_blocked' AND customer_id = %s AND target = %s
           AND detail->>'ip' = %s AND at > now() - interval '10 minutes' LIMIT 1""",
        (user.customer_id, user.email, ip),
    ).fetchone()
    if not seen:
        audit.record(conn, user.actor, "security.ip_blocked", user.email, user.customer_id, {"ip": ip, "via": user.via})


# ---- sign-in -------------------------------------------------------------------------------


def signin_policy(row: dict, ip: str, via: str) -> dict:
    """What a business's settings say about a sign-in that has passed its checks:
    {"refuse": message or None, "reason": str, "session_hours": int | None, "enrol": bool}."""
    out = {"refuse": None, "reason": "", "session_hours": None, "enrol": False}
    if row.get("role") != "customer" or not row.get("customer_id"):
        return out
    with db.tx() as conn:
        s = get(conn, row["customer_id"])
        if s["ip_allowlist"] and not ip_allowed(ip, s["ip_allowlist"]):
            out.update(refuse=IP_BLOCKED, reason="ip_not_allowed")
            return out
        out["session_hours"] = s["session_hours"]
        out["enrol"] = needs_two_step(conn, s, row, via)
    return out


def remember_id_token(token_hash: str, id_token: str) -> None:
    """Keep the gateway's ID token with the session it started, for sign-out."""
    with db.tx() as conn:
        conn.execute("UPDATE sessions SET id_token = %s WHERE token_hash = %s", (id_token, token_hash))


def logout_url(conn: psycopg.Connection, settings: Any, session_hash: str) -> str | None:
    """The gateway's RP-initiated logout address for an SSO session, or None."""
    from ...identity import oidc

    row = conn.execute(
        """SELECT s.id_token, u.customer_id FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = %s""",
        (session_hash,),
    ).fetchone()
    if row is None or not row["id_token"] or not oidc.configured(settings):
        return None
    if row["customer_id"] and not get(conn, row["customer_id"])["sso_logout"]:
        return None
    try:
        endpoint = oidc.discovery(settings.oidc_issuer).get("end_session_endpoint")
    except oidc.OidcError:
        return None
    if not endpoint:
        return None
    q = {
        "id_token_hint": row["id_token"],
        "post_logout_redirect_uri": f"{settings.public_url}/",
        "client_id": settings.oidc_client_id,
    }
    return f"{endpoint}?{urllib.parse.urlencode(q)}"


def saml_logout_supported(xml: str) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    return root.find(f".//{{{_MD}}}SingleLogoutService") is not None


def sso_logout_status(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """For each SSO connection: whether sign-out reaches the company's provider."""
    out = []
    for c in conn.execute(
        """SELECT id, display_name, protocol, metadata_xml, metadata_url, status
           FROM sso_connections WHERE customer_id = %s""",
        (customer_id,),
    ).fetchall():
        if c["protocol"] == "saml" and c["metadata_xml"]:
            ok = saml_logout_supported(c["metadata_xml"])
            note = (
                "Your provider's metadata has a SingleLogoutService, so signing out also signs out there."
                if ok
                else "Your provider's metadata has no SingleLogoutService: signing out ends the ExaCarib session only."
            )
            state = "yes" if ok else "no"
        else:
            state = "if_published"
            note = (
                "The gateway reads your provider's logout address from its "
                + ("discovery document (end_session_endpoint)." if c["protocol"] == "oidc" else "metadata URL.")
                + " Signing out reaches your provider if it publishes one."
            )
        out.append(
            {"id": str(c["id"]), "name": c["display_name"], "protocol": c["protocol"], "logout": state, "note": note}
        )
    return out


# ---- the audit view ------------------------------------------------------------------------

SIGNIN_ACTIONS = ("login", "logout", "login.password_ok")
SETTING_PREFIXES = (
    "commai.security.",
    "commai.roles.",
    "commai.org.",
    "commai.data.",
    "commai.alert.",
    "sso.",
    "scim.",
    "two_step.",
    "passkey.",
    "api_key.",
    "security.",
    "user.",
)


def audit_view(conn: psycopg.Connection, customer_id: Any, kind: str, limit: int, before: int | None) -> list[dict]:
    """Sign-ins, failed sign-ins and setting changes for one business."""
    emails = [
        r["email"].lower()
        for r in conn.execute("SELECT email FROM users WHERE customer_id = %s", (customer_id,)).fetchall()
    ]
    where = {
        "signin": "a.customer_id = %(c)s AND a.action = ANY(%(signin)s)",
        "failed": "a.action = 'login_failed' AND (a.customer_id = %(c)s OR lower(a.target) = ANY(%(emails)s))",
        "settings": "a.customer_id = %(c)s AND a.action LIKE ANY(%(prefixes)s)",
    }[kind]
    return conn.execute(
        f"""SELECT a.id, a.at, a.actor, a.action, a.target, a.detail FROM audit_log a
            WHERE {where} AND (%(before)s::bigint IS NULL OR a.id < %(before)s::bigint)
            ORDER BY a.id DESC LIMIT %(n)s""",
        {
            "c": customer_id,
            "signin": list(SIGNIN_ACTIONS),
            "emails": emails,
            "prefixes": [p + "%" for p in SETTING_PREFIXES],
            "before": before,
            "n": limit,
        },
    ).fetchall()
