"""Who may do what in CommAI (ADR 0016).

Three layers, all enforced by the service, never by a prompt:

- The account role (admin, customer, carrier) and the customer it belongs to.
  Carrier accounts never reach CommAI. Customer accounts reach their own
  business only.
- The seat: 'agent' (the default) replies to customers; 'internal' reads
  conversations and writes private notes but never replies.
- API key scopes. A key made with scopes can do only those things:

    connect        the network API
    commai:read    contacts, conversations and messages (read)
    commai:write   contacts, conversations and messages (write, send replies)
    commai:notes   private notes (read and write)
    commai:admin   settings, teams, channels, webhooks, integrations, workflows, voice

  A "customer-facing" key is any key without commai:notes. It can never read
  a note, whatever else it holds.
- The person's role in the organisation (ADR 0023). commai:admin work
  (settings) needs an owner or admin; viewers never write (enforced for every
  write in api/deps.py).
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import HTTPException, status

from ..api.deps import User, can_manage_org, check_customer

SCOPES = ("connect", "commai:read", "commai:write", "commai:notes", "commai:admin")
NOT_ORG_ADMIN = "Only this organisation's owners and admins can change these settings."


def require_scope(user: User, scope: str) -> None:
    if scope == "commai:admin" and user.role == "customer" and not can_manage_org(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_ORG_ADMIN)
    if user.scopes is None or scope in user.scopes:
        return
    # A write scope implies read for the same records.
    if scope == "commai:read" and "commai:write" in user.scopes:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, f"This API key does not have the {scope} scope.")


def can_read_notes(user: User) -> bool:
    return user.scopes is None or "commai:notes" in user.scopes


def check(user: User, customer_id: Any, scope: str) -> None:
    """The caller may act on this business with this scope."""
    if user.role == "carrier":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for this account.")
    check_customer(user, customer_id)
    require_scope(user, scope)


def seat(conn: psycopg.Connection, user: User, customer_id: Any) -> str:
    """'admin' for ExaCarib admins, else the member's seat ('agent' by default)."""
    if user.role == "admin":
        return "admin"
    row = conn.execute(
        "SELECT seat FROM commai_members WHERE customer_id = %s AND user_id = %s", (customer_id, user.id)
    ).fetchone()
    return row["seat"] if row else "agent"


def require_reply_seat(conn: psycopg.Connection, user: User, customer_id: Any) -> None:
    if seat(conn, user, customer_id) == "internal":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Your seat can read conversations and write private notes, but not reply."
        )


def require_business_admin(user: User) -> None:
    """Settings that change how the business runs: ExaCarib admins, or the
    business's own accounts using a key with commai:admin (or no key)."""
    require_scope(user, "commai:admin")
