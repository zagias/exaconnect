"""Enterprise single sign-on: connections, email-domain routing and the rights
of people who arrive through a business's own identity provider."""

from __future__ import annotations

import re
import secrets
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import psycopg

# Directory-provisioned and SSO-created people start with these rights: Jibsy
# work (read, write, private notes), no network API and no business settings.
MEMBER_SCOPES = ["commai:read", "commai:write", "commai:notes"]
UNUSABLE_PASSWORD = "!sso"  # verify_password() never accepts it

# Nobody can claim a mailbox provider's domain for their business.
PUBLIC_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "gmx.com",
    "zoho.com",
    "yandex.com",
}
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_MD = "urn:oasis:names:tc:SAML:2.0:metadata"


class SsoError(ValueError):
    """A problem with what was asked for. The message is safe to show."""


def normalise_domain(d: str) -> str:
    d = (d or "").strip().lower().lstrip("@").rstrip(".")
    if not _DOMAIN.match(d):
        raise SsoError(f"{d or 'That'} is not an email domain.")
    if d in PUBLIC_DOMAINS:
        raise SsoError(f"{d} is a public mailbox domain and can't be routed to one organisation.")
    return d


def domain_of(email: str) -> str:
    return (email or "").rsplit("@", 1)[-1].strip().lower()


def new_alias(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")[:24] or "sso"
    return f"sso-{slug}-{secrets.token_hex(3)}"


def check_saml_metadata(xml: str) -> str:
    """The entity ID of a SAML 2.0 IdP metadata document, or SsoError."""
    if len(xml) > 512_000:
        raise SsoError("The metadata file is too large.")
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise SsoError("The metadata file may not contain a DOCTYPE.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise SsoError("The metadata is not valid XML.") from e
    ed = root if root.tag == f"{{{_MD}}}EntityDescriptor" else root.find(f"{{{_MD}}}EntityDescriptor")
    if ed is None or not ed.get("entityID"):
        raise SsoError("The metadata has no EntityDescriptor with an entityID.")
    idp = ed.find(f"{{{_MD}}}IDPSSODescriptor")
    if idp is None or idp.find(f"{{{_MD}}}SingleSignOnService") is None:
        raise SsoError("The metadata does not describe an identity provider's sign-in service.")
    return ed.get("entityID")


def check_url(url: str) -> str:
    u = urllib.parse.urlparse((url or "").strip())
    if u.scheme != "https" or not u.netloc:
        raise SsoError("Use an https:// address.")
    return u.geturl()


def routed_connection(conn: psycopg.Connection, email: str) -> dict | None:
    """The enabled connection an approved domain claim sends this email to."""
    return conn.execute(
        """SELECT c.* FROM sso_domains d JOIN sso_connections c ON c.id = d.connection_id
           WHERE d.domain = %s AND d.status = 'approved' AND c.status = 'enabled' LIMIT 1""",
        (domain_of(email),),
    ).fetchone()


def requires_sso(conn: psycopg.Connection, email: str) -> dict | None:
    c = routed_connection(conn, email)
    return c if c and c["require_sso"] else None


def domain_approved_for(conn: psycopg.Connection, connection_id: Any, email: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sso_domains WHERE connection_id = %s AND domain = %s AND status = 'approved'",
            (connection_id, domain_of(email)),
        ).fetchone()
        is not None
    )


def provision(conn: psycopg.Connection, connection: dict, email: str, claims: dict) -> dict:
    """A new account for someone the business's own identity provider vouched for."""
    row = conn.execute(
        """INSERT INTO users (email, password_hash, role, customer_id, access_scopes, provisioned_by,
                              display_name, given_name, family_name)
           VALUES (%s, %s, 'customer', %s, %s, 'sso', %s, %s, %s) RETURNING *""",
        (
            email.lower(),
            UNUSABLE_PASSWORD,
            connection["customer_id"],
            MEMBER_SCOPES,
            str(claims.get("name") or "")[:200],
            str(claims.get("given_name") or "")[:100],
            str(claims.get("family_name") or "")[:100],
        ),
    ).fetchone()
    conn.execute(
        "INSERT INTO commai_members (customer_id, user_id, seat) VALUES (%s, %s, 'agent') ON CONFLICT DO NOTHING",
        (connection["customer_id"], row["id"]),
    )
    return row
