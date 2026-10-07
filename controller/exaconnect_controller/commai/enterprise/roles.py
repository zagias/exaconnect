"""Roles and per-team permissions (ADR 0024).

A business admin builds roles from a fixed list of permissions and gives them
to people, across the business or for one team only. Roles narrow what a
person may do; they never widen it past their account, seat, API key scopes
or voice permissions (ADR 0016, 0021).

A person with no role assignments keeps exactly the rights they had before
roles existed. The built-in roles describe those rights: a full business
account is a Business admin, an agent seat is an Agent, an internal seat is
Internal.

Enforcement is in `commai/access.py`: every CommAI endpoint already calls
`access.check(user, customer_id, scope)`; for a person with roles it also
needs the permission that scope and endpoint stand for (`required`).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg

PERMISSIONS: dict[str, str] = {
    "read_inbox": "Read the inbox",
    "reply": "Reply to customers",
    "notes": "Read and write private notes",
    "manage_teams": "Manage teams, routing, hours and service settings",
    "manage_channels": "Manage channels and webhooks",
    "manage_integrations": "Manage integrations, workflows and AI settings",
    "approve_spending": "Approve spending and usage limits",
    "voice_admin": "Manage the phone system",
    "view_reports": "View reports",
    "export_data": "Export data",
    "manage_security": "Manage roles, security and data settings",
}

# The section after /customers/{id}/ and the permission its admin endpoints need.
ADMIN_SECTIONS = {
    "teams": "manage_teams",
    "members": "manage_teams",
    "routing-rules": "manage_teams",
    "settings": "manage_teams",
    "organisation": "manage_teams",
    "widget-keys": "manage_channels",
    "channel-accounts": "manage_channels",
    "whatsapp-templates": "manage_channels",
    "webhooks": "manage_channels",
    "integrations": "manage_integrations",
    "workflows": "manage_integrations",
    "workflow-runs": "manage_integrations",
    "onboarding": "manage_integrations",
    "assistant": "manage_integrations",
    "support-cases": "manage_integrations",
    "ai": "manage_integrations",
    "usage-limits": "approve_spending",
    "voice": "voice_admin",
    "roles": "manage_security",
    "security": "manage_security",
    "data": "manage_security",
}
_CUSTOMER_PATH = re.compile(r"^/api/v1/commai/customers/[^/]+/(.*)$")
_CONVERSATION = re.compile(r"^conversations/([0-9a-fA-F-]{36})(?:/|$)")


class RoleError(ValueError):
    """A role change we refuse. The message is safe to show."""


def section(path: str) -> list[str]:
    m = _CUSTOMER_PATH.match(path or "")
    return m.group(1).strip("/").split("/") if m else []


def required(scope: str, path: str) -> str:
    """The permission a CommAI request needs, from its scope and endpoint."""
    parts = section(path)
    head = parts[0] if parts else ""
    tail = parts[-1] if parts else ""
    if scope == "commai:notes" or (head == "conversations" and tail == "notes"):
        return "notes"
    if head == "conversations" and tail == "export":
        return "export_data"
    if head == "reports":
        return "view_reports"
    if head == "actions" and tail in ("approve", "reject"):
        return "approve_spending"
    if head == "voice" and len(parts) >= 3 and parts[1] == "orders" and tail == "approve":
        return "approve_spending"
    if head == "data" and ("export" in parts or "exports" in parts or "download" in parts):
        return "export_data"
    if (head == "data" and tail == "processing") or (head == "roles" and tail == "me"):
        return "read_inbox"
    if scope == "commai:admin":
        return ADMIN_SECTIONS.get(head, "manage_security")
    if scope == "commai:write":
        return "reply"
    if head == "usage-limits":
        return "view_reports"
    return "read_inbox"


def conversation_in_path(path: str) -> str | None:
    m = _CONVERSATION.match("/".join(section(path)))
    return m.group(1) if m else None


# ---- loading a person's rights -------------------------------------------------------


def load(conn: psycopg.Connection, user_id: Any, customer_id: Any) -> tuple[frozenset[str] | None, dict[str, set]]:
    """(business-wide permissions or None when the person has no roles,
    {team_id: permissions held only for that team})."""
    rows = conn.execute(
        """SELECT a.team_id, r.permissions FROM commai_role_assignments a JOIN commai_roles r ON r.id = a.role_id
           WHERE a.user_id = %s AND a.customer_id = %s""",
        (user_id, customer_id),
    ).fetchall()
    if not rows:
        return None, {}
    whole: set[str] = set()
    teams: dict[str, set] = {}
    for r in rows:
        if r["team_id"] is None:
            whole.update(r["permissions"])
        else:
            teams.setdefault(str(r["team_id"]), set()).update(r["permissions"])
    return frozenset(whole), teams


def effective(conn: psycopg.Connection, user_id: Any, customer_id: Any) -> dict:
    whole, teams = load(conn, user_id, customer_id)
    return {
        "has_roles": whole is not None,
        "permissions": sorted(whole) if whole is not None else sorted(PERMISSIONS),
        "teams": {t: sorted(p) for t, p in teams.items()},
    }


# ---- roles and assignments --------------------------------------------------------------


def check_permissions(perms: list[str]) -> list[str]:
    bad = [p for p in perms if p not in PERMISSIONS]
    if bad:
        raise RoleError(f"Unknown permission: {', '.join(bad)}.")
    return sorted(set(perms))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "role"


def get_role(conn: psycopg.Connection, customer_id: Any, role_id: Any) -> dict:
    try:
        uuid.UUID(str(role_id))
    except ValueError as e:
        raise RoleError("Role not found.") from e
    row = conn.execute(
        "SELECT * FROM commai_roles WHERE id = %s AND (customer_id = %s OR customer_id IS NULL)",
        (role_id, customer_id),
    ).fetchone()
    if row is None:
        raise RoleError("Role not found.")
    return row


def security_holders(conn: psycopg.Connection, customer_id: Any) -> int:
    """Active people of the business who can still manage security: those with
    no roles (full account rights, no scope limit) or a business-wide role that
    includes manage_security."""
    return conn.execute(
        """SELECT count(*) AS n FROM users u
           WHERE u.customer_id = %(c)s AND u.role = 'customer' AND u.disabled_at IS NULL
             AND (u.access_scopes IS NULL OR 'commai:admin' = ANY(u.access_scopes))
             AND (NOT EXISTS (SELECT 1 FROM commai_role_assignments a WHERE a.user_id = u.id AND a.customer_id = %(c)s)
                  OR EXISTS (SELECT 1 FROM commai_role_assignments a JOIN commai_roles r ON r.id = a.role_id
                             WHERE a.user_id = u.id AND a.customer_id = %(c)s AND a.team_id IS NULL
                               AND 'manage_security' = ANY(r.permissions)))""",
        {"c": customer_id},
    ).fetchone()["n"]


def guard_lockout(conn: psycopg.Connection, customer_id: Any) -> None:
    if security_holders(conn, customer_id) == 0:
        raise RoleError("That would leave nobody in your organisation able to manage security. Keep at least one.")
