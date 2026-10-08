"""Voice permissions (ADR 0021).

- ExaCarib admins can do everything, for any business.
- A business's voice admins add, remove and move users, numbers and devices,
  and change routing. They need the commai:admin scope (any signed-in account
  has it; an API key only if it was given it).
- Spend permission is separate: any change that alters the bill (a monthly fee
  or a one-time fee) needs a person with spend permission to save it, and a
  voice order needs one to approve it.
- Until a business names its voice people (no rows in voice_permissions),
  every account of that business counts as a voice admin with spend
  permission, so a new business is not locked out. Naming the first person
  ends that.
- Everyone else in the business is staff: they change only their own
  settings (forwarding, do not disturb, voicemail) through /voice/me.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import HTTPException, status

from ...api.deps import User, can_manage_org


def _row(conn: psycopg.Connection, user: User, customer_id: Any) -> dict | None:
    return conn.execute(
        "SELECT voice_admin, spend FROM voice_permissions WHERE customer_id = %s AND user_id = %s",
        (customer_id, user.id),
    ).fetchone()


def configured(conn: psycopg.Connection, customer_id: Any) -> bool:
    return (
        conn.execute("SELECT 1 FROM voice_permissions WHERE customer_id = %s LIMIT 1", (customer_id,)).fetchone()
        is not None
    )


def _has_admin_scope(user: User) -> bool:
    # An organisation's members and viewers never administer voice (ADR 0023).
    if user.role == "customer" and not can_manage_org(user):
        return False
    return user.scopes is None or "commai:admin" in user.scopes


def is_voice_admin(conn: psycopg.Connection, user: User, customer_id: Any) -> bool:
    if user.role == "admin":
        return _has_admin_scope(user)
    if user.role != "customer" or not _has_admin_scope(user):
        return False
    if not configured(conn, customer_id):
        return True
    row = _row(conn, user, customer_id)
    return bool(row and row["voice_admin"])


def can_spend(conn: psycopg.Connection, user: User, customer_id: Any) -> bool:
    if not is_voice_admin(conn, user, customer_id):
        return False
    if user.role == "admin" or not configured(conn, customer_id):
        return True
    row = _row(conn, user, customer_id)
    return bool(row and row["spend"])


def require_voice_admin(conn: psycopg.Connection, user: User, customer_id: Any) -> None:
    if not is_voice_admin(conn, user, customer_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only your company's voice admins can change the phone system.")


def require_spend(conn: psycopg.Connection, user: User, customer_id: Any) -> None:
    if not can_spend(conn, user, customer_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This changes your bill. Ask someone in your company with spend permission to save it.",
        )


def require_exacarib(user: User) -> None:
    """Prices, bundles, invoices and the supplier side: ExaCarib sets them."""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "ExaCarib sets prices and issues invoices.")
