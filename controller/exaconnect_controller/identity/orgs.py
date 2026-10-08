"""Shared accounts (ADR 0023): memberships, roles and invitations.

A customer account can belong to several organisations (customers), with a
role in each:

  owner    everything an admin can do, and hand ownership on
  admin    manage people and settings (SSO, SCIM, Jibsy settings)
  member   make changes (classes, traffic rules, Storm Mode, conversations)
  viewer   read only

An organisation always keeps at least one owner. People whose rights come
from the organisation's directory (SCIM or SSO, `managed_by`) are changed and
removed there, not in the portal.

Invitations carry a one-time token. Only its SHA-256 is stored; the link is
shown once to whoever made it, because no email is sent yet."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import psycopg

from ..security import new_token, token_hash

INVITE_DAYS = 7
INVITE_ROLES = ("admin", "member", "viewer")
MANAGER_ROLES = ("owner", "admin")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")
MANAGED = "Your directory manages this person's access here. Change it there."


class OrgError(Exception):
    """A rule was broken. The message is safe to show."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def memberships(conn: psycopg.Connection, user_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT m.customer_id, c.name, m.role, m.managed_by, m.created_at, c.products
           FROM org_memberships m JOIN customers c ON c.id = m.customer_id
           WHERE m.user_id = %s ORDER BY c.name""",
        (user_id,),
    ).fetchall()


def membership(conn: psycopg.Connection, customer_id: Any, user_id: Any, lock: bool = False) -> dict | None:
    return conn.execute(
        "SELECT * FROM org_memberships WHERE customer_id = %s AND user_id = %s" + (" FOR UPDATE" if lock else ""),
        (customer_id, user_id),
    ).fetchone()


def members(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT u.id AS user_id, u.email, u.display_name AS name, m.role, m.managed_by, m.created_at, m.apps,
                  u.disabled_at IS NOT NULL AS disabled, u.customer_id = m.customer_id AS primary_org
           FROM org_memberships m JOIN users u ON u.id = m.user_id
           WHERE m.customer_id = %s ORDER BY (m.role = 'owner') DESC, lower(u.email)""",
        (customer_id,),
    ).fetchall()


def _owners(conn: psycopg.Connection, customer_id: Any) -> int:
    # Locks the organisation's owner rows so two demotions can't both pass the check.
    rows = conn.execute(
        "SELECT user_id FROM org_memberships WHERE customer_id = %s AND role = 'owner' FOR UPDATE", (customer_id,)
    ).fetchall()
    return len(rows)


def _last_owner(conn: psycopg.Connection, customer_id: Any, m: dict) -> bool:
    return m["role"] == "owner" and _owners(conn, customer_id) <= 1


def _target(conn: psycopg.Connection, customer_id: Any, user_id: Any) -> dict:
    try:
        m = membership(conn, customer_id, user_id, lock=True)
    except psycopg.errors.InvalidTextRepresentation:
        m = None
    if m is None:
        raise OrgError("That person is not a member of this organisation.", 404)
    return m


def set_role(conn: psycopg.Connection, customer_id: Any, user_id: Any, role: str, *, by_role: str | None) -> dict:
    """Change a member's role. by_role: the actor's role here (None for ExaCarib admins)."""
    m = _target(conn, customer_id, user_id)
    if m["managed_by"]:
        raise OrgError(MANAGED, 409)
    if by_role not in (None, "owner") and "owner" in (role, m["role"]):
        raise OrgError("Only an owner can make someone an owner or change an owner's role.", 403)
    if m["role"] == role:
        return m
    if role != "owner" and _last_owner(conn, customer_id, m):
        raise OrgError("An organisation needs at least one owner. Make someone else an owner first.", 409)
    return conn.execute(
        """UPDATE org_memberships SET role = %s, updated_at = now() WHERE customer_id = %s AND user_id = %s
           RETURNING *""",
        (role, customer_id, user_id),
    ).fetchone()


def remove(conn: psycopg.Connection, customer_id: Any, user_id: Any, *, by_role: str | None, leaving: bool) -> dict:
    """Take someone out of an organisation (or let them leave). Their sessions stop
    acting for it, keys made in it are revoked, and their Jibsy seat, teams and
    voice rights there go. Their account, and other memberships, stay."""
    m = _target(conn, customer_id, user_id)
    if m["managed_by"]:
        raise OrgError(
            "Your directory manages your access here. Ask your IT team to remove you." if leaving else MANAGED, 409
        )
    if not leaving and m["role"] == "owner" and by_role not in (None, "owner"):
        raise OrgError("Only an owner can remove an owner.", 403)
    if _last_owner(conn, customer_id, m):
        raise OrgError(
            "You are the only owner. Hand ownership to someone else before you leave."
            if leaving
            else "An organisation needs at least one owner. Make someone else an owner first.",
            409,
        )
    conn.execute("DELETE FROM org_memberships WHERE customer_id = %s AND user_id = %s", (customer_id, user_id))
    conn.execute(
        "UPDATE sessions SET customer_id = NULL WHERE user_id = %s AND customer_id = %s", (user_id, customer_id)
    )
    conn.execute(
        "UPDATE api_keys SET revoked_at = now() WHERE user_id = %s AND customer_id = %s AND revoked_at IS NULL",
        (user_id, customer_id),
    )
    conn.execute("DELETE FROM commai_members WHERE customer_id = %s AND user_id = %s", (customer_id, user_id))
    conn.execute(
        """DELETE FROM commai_team_members tm USING commai_teams t
           WHERE tm.team_id = t.id AND t.customer_id = %s AND tm.user_id = %s""",
        (customer_id, user_id),
    )
    conn.execute("DELETE FROM voice_permissions WHERE customer_id = %s AND user_id = %s", (customer_id, user_id))
    # If this was their primary organisation, the next one they belong to takes over.
    conn.execute(
        """UPDATE users SET customer_id = (SELECT customer_id FROM org_memberships WHERE user_id = %(u)s
                                            ORDER BY created_at, customer_id LIMIT 1), updated_at = now()
           WHERE id = %(u)s AND customer_id = %(c)s""",
        {"u": user_id, "c": customer_id},
    )
    return m


def transfer(conn: psycopg.Connection, customer_id: Any, from_user: Any, to_user: Any) -> None:
    """Hand ownership on: the new person becomes an owner, the old owner an admin."""
    if str(from_user) == str(to_user):
        raise OrgError("You already own this organisation.")
    to = _target(conn, customer_id, to_user)
    if to["managed_by"]:
        raise OrgError(MANAGED, 409)
    conn.execute(
        "UPDATE org_memberships SET role = 'owner', updated_at = now() WHERE customer_id = %s AND user_id = %s",
        (customer_id, to_user),
    )
    if from_user is not None:
        conn.execute(
            """UPDATE org_memberships SET role = 'admin', updated_at = now()
               WHERE customer_id = %s AND user_id = %s AND role = 'owner'""",
            (customer_id, from_user),
        )


def ensure_owner(conn: psycopg.Connection, customer_id: Any) -> str | None:
    """If the organisation has no owner left, its longest-standing hand-managed
    admin becomes one. Returns that person's email, or None."""
    if _owners(conn, customer_id):
        return None
    row = conn.execute(
        """UPDATE org_memberships m SET role = 'owner', updated_at = now() FROM users u
           WHERE u.id = m.user_id AND m.customer_id = %(c)s AND (m.customer_id, m.user_id) = (
             SELECT o.customer_id, o.user_id FROM org_memberships o
             WHERE o.customer_id = %(c)s AND o.role = 'admin' AND o.managed_by IS NULL
             ORDER BY o.created_at, o.user_id LIMIT 1)
           RETURNING u.email""",
        {"c": customer_id},
    ).fetchone()
    return row["email"] if row else None


def add(conn: psycopg.Connection, customer_id: Any, user_id: Any, role: str, added_by: str) -> dict:
    return conn.execute(
        """INSERT INTO org_memberships (customer_id, user_id, role, added_by) VALUES (%s, %s, %s, %s)
           ON CONFLICT (customer_id, user_id) DO UPDATE SET role = EXCLUDED.role, updated_at = now()
           RETURNING *""",
        (customer_id, user_id, role, added_by),
    ).fetchone()


# ---- invitations -------------------------------------------------------------------


def normalise_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not EMAIL.match(e):
        raise OrgError("Enter an email address.", 422)
    return e


def invite(conn: psycopg.Connection, customer_id: Any, email: str, role: str, actor: str) -> tuple[dict, str]:
    """(invite, token). The token is returned once and never stored."""
    if role not in INVITE_ROLES:
        raise OrgError("Invite people as an admin, member or viewer. Hand on ownership once they have joined.", 422)
    email = normalise_email(email)
    existing = conn.execute(
        """SELECT u.role, m.role AS org_role FROM users u
           LEFT JOIN org_memberships m ON m.user_id = u.id AND m.customer_id = %s
           WHERE lower(u.email) = %s""",
        (customer_id, email),
    ).fetchone()
    if existing and existing["role"] != "customer":
        raise OrgError("That email belongs to an ExaCarib or carrier account, which can't join organisations.", 409)
    if existing and existing["org_role"]:
        raise OrgError("That person is already a member of this organisation.", 409)
    # A new invitation replaces any earlier one for the same address.
    conn.execute(
        """UPDATE org_invites SET revoked_at = now()
           WHERE customer_id = %s AND email = %s AND accepted_at IS NULL AND revoked_at IS NULL""",
        (customer_id, email),
    )
    token = new_token()
    row = conn.execute(
        f"""INSERT INTO org_invites (customer_id, email, role, token_hash, invited_by, expires_at)
            VALUES (%s, %s, %s, %s, %s, now() + interval '{INVITE_DAYS} days')
            RETURNING id, email, role, invited_by, created_at, expires_at""",
        (customer_id, email, role, token_hash(token), actor),
    ).fetchone()
    return row, token


def pending(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT id, email, role, invited_by, created_at, expires_at, expires_at <= now() AS expired
           FROM org_invites WHERE customer_id = %s AND accepted_at IS NULL AND revoked_at IS NULL
           ORDER BY created_at DESC""",
        (customer_id,),
    ).fetchall()


def revoke(conn: psycopg.Connection, customer_id: Any, invite_id: str) -> dict:
    try:
        row = conn.execute(
            """UPDATE org_invites SET revoked_at = now()
               WHERE id = %s::uuid AND customer_id = %s AND accepted_at IS NULL AND revoked_at IS NULL
               RETURNING email, role""",
            (invite_id, customer_id),
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None:
        raise OrgError("Invitation not found.", 404)
    return row


def lookup(conn: psycopg.Connection, token: str) -> dict:
    """What an invitation link is for, without using it."""
    row = conn.execute(
        """SELECT i.*, c.name AS organisation,
                  EXISTS (SELECT 1 FROM users u WHERE lower(u.email) = i.email) AS has_account
           FROM org_invites i JOIN customers c ON c.id = i.customer_id WHERE i.token_hash = %s""",
        (token_hash(token or ""),),
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise OrgError("This invitation link isn't valid. Ask for a new one.", 404)
    if row["accepted_at"] is not None:
        raise OrgError("This invitation has already been used.", 410)
    if row["expires_at"] <= dt.datetime.now(dt.UTC):
        raise OrgError("This invitation has expired. Ask for a new one.", 410)
    return row


def claim(conn: psycopg.Connection, token: str, user_id: Any) -> dict:
    """Use the invitation once. Raises OrgError if it is unknown, used, revoked or expired."""
    lookup(conn, token)
    row = conn.execute(
        """UPDATE org_invites SET accepted_at = now(), accepted_by = %s
           WHERE token_hash = %s AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()
           RETURNING *""",
        (user_id, token_hash(token)),
    ).fetchone()
    if row is None:
        raise OrgError("This invitation has already been used.", 410)
    return row
