"""Sandboxes and sandbox keys (ADR 0031).

A sandbox is a copy of a business: its own customers row (``sandbox_of`` points
at the real one) with the same CommAI settings and a simulated copy of each
channel account. Sandbox keys belong to the sandbox's own account, so tenant
checks keep them inside the sandbox: they can never read or change the real
business.

Nothing real leaves a sandbox. A database trigger forces every channel
account of a sandbox onto the simulated provider, whatever the API is asked,
and refuses number and porting orders. No integration credentials are copied.
"""

from __future__ import annotations

import secrets
from typing import Any

import psycopg

from ..api.deps import API_KEY_PREFIX
from ..security import new_token, token_hash
from .access import SCOPES

SANDBOX_SCOPES = [s for s in SCOPES if s != "connect"]
MAX_KEYS = 10
UNUSABLE_PASSWORD = "!sandbox"
KEY_COLUMNS = "id, name, prefix, scopes, sandbox, created_at, last_used_at, expires_at"


class SandboxError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def of(conn: psycopg.Connection, customer_id: Any) -> dict | None:
    return conn.execute(
        "SELECT id, name, sandbox_of, created_at FROM customers WHERE sandbox_of = %s", (customer_id,)
    ).fetchone()


def is_sandbox(conn: psycopg.Connection, customer_id: Any) -> bool:
    row = conn.execute("SELECT sandbox_of FROM customers WHERE id = %s", (customer_id,)).fetchone()
    return bool(row and row["sandbox_of"])


def _account(conn: psycopg.Connection, sandbox_id: Any) -> dict:
    row = conn.execute(
        "SELECT * FROM users WHERE customer_id = %s AND provisioned_by = 'sandbox'", (sandbox_id,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            """INSERT INTO users (email, password_hash, role, customer_id, access_scopes, display_name, provisioned_by)
               VALUES (%s, %s, 'customer', %s, %s, 'Sandbox keys', 'sandbox') RETURNING *""",
            (f"sandbox+{sandbox_id}@sandbox.invalid", UNUSABLE_PASSWORD, sandbox_id, SANDBOX_SCOPES),
        ).fetchone()
        # Sandbox keys set the sandbox up as well (channels, settings): an admin there
        # (ADR 0023), still limited to the sandbox scopes.
        conn.execute(
            "UPDATE org_memberships SET role = 'admin' WHERE customer_id = %s AND user_id = %s",
            (sandbox_id, row["id"]),
        )
    return row


def create(conn: psycopg.Connection, customer_id: Any) -> dict:
    """Make (or return) the business's sandbox."""
    if is_sandbox(conn, customer_id):
        raise SandboxError("This is already a sandbox.", 409)
    existing = of(conn, customer_id)
    if existing:
        return existing
    real = conn.execute("SELECT name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    if real is None:
        raise SandboxError("No such business.", 404)
    name = f"{real['name']} (sandbox)"
    if conn.execute("SELECT 1 FROM customers WHERE name = %s", (name,)).fetchone():
        name = f"{real['name']} (sandbox {secrets.token_hex(3)})"
    sb = conn.execute(
        "INSERT INTO customers (name, sandbox_of) VALUES (%s, %s) RETURNING id, name, sandbox_of, created_at",
        (name, customer_id),
    ).fetchone()
    # The business's CommAI settings, so the sandbox behaves the same.
    cols = [
        r["column_name"]
        for r in conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'commai_settings' AND column_name NOT IN ('customer_id')"""
        ).fetchall()
    ]
    if cols and conn.execute("SELECT 1 FROM commai_settings WHERE customer_id = %s", (customer_id,)).fetchone():
        col_list = ", ".join(f'"{c}"' for c in cols)
        conn.execute(
            f"INSERT INTO commai_settings (customer_id, {col_list}) SELECT %s, {col_list} FROM commai_settings "
            "WHERE customer_id = %s ON CONFLICT DO NOTHING",
            (sb["id"], customer_id),
        )
    # A simulated twin of each channel account (the trigger forces 'simulated' anyway).
    from .channels import messaging

    for a in conn.execute(
        "SELECT channel, name, address FROM channel_accounts WHERE customer_id = %s", (customer_id,)
    ).fetchall():
        conn.execute(
            """INSERT INTO channel_accounts (customer_id, channel, provider, name, address, status, hook_token,
                 hook_secret, created_by) VALUES (%s, %s, 'simulated', %s, %s, 'live', %s, %s, 'sandbox')
               ON CONFLICT DO NOTHING""",
            (sb["id"], a["channel"], a["name"], a["address"], messaging.new_hook_token(),
             "chs_" + secrets.token_urlsafe(24)),
        )  # fmt: skip
    _account(conn, sb["id"])
    return sb


def create_key(conn: psycopg.Connection, customer_id: Any, name: str, scopes: list[str] | None) -> dict:
    sb = of(conn, customer_id)
    if sb is None:
        raise SandboxError("Make the sandbox first.", 409)
    scopes = sorted(set(scopes or SANDBOX_SCOPES))
    bad = sorted(set(scopes) - set(SANDBOX_SCOPES))
    if bad:
        raise SandboxError(f"Sandbox key scopes are {', '.join(SANDBOX_SCOPES)}.", 422)
    acct = _account(conn, sb["id"])
    n = conn.execute(
        "SELECT count(*) AS n FROM api_keys WHERE user_id = %s AND revoked_at IS NULL", (acct["id"],)
    ).fetchone()["n"]
    if n >= MAX_KEYS:
        raise SandboxError(f"A sandbox can have {MAX_KEYS} keys. Revoke one first.", 400)
    token = API_KEY_PREFIX + "sbx_" + new_token()
    row = conn.execute(
        f"""INSERT INTO api_keys (user_id, name, prefix, token_hash, scopes, sandbox)
            VALUES (%s, %s, %s, %s, %s, true) RETURNING {KEY_COLUMNS}""",
        (acct["id"], name.strip() or "Sandbox key", token[:12], token_hash(token), scopes),
    ).fetchone()
    return {**row, "token": token, "sandbox_customer_id": str(sb["id"])}


def list_keys(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    sb = of(conn, customer_id)
    if sb is None:
        return []
    return conn.execute(
        f"""SELECT {KEY_COLUMNS} FROM api_keys k WHERE sandbox AND revoked_at IS NULL AND user_id IN
              (SELECT id FROM users WHERE customer_id = %s AND provisioned_by = 'sandbox') ORDER BY created_at""",
        (sb["id"],),
    ).fetchall()


def revoke_key(conn: psycopg.Connection, customer_id: Any, key_id: int) -> dict:
    sb = of(conn, customer_id)
    row = None
    if sb:
        row = conn.execute(
            """UPDATE api_keys SET revoked_at = now() WHERE id = %s AND sandbox AND revoked_at IS NULL
                 AND user_id IN (SELECT id FROM users WHERE customer_id = %s AND provisioned_by = 'sandbox')
               RETURNING prefix, name""",
            (key_id, sb["id"]),
        ).fetchone()
    if row is None:
        raise SandboxError("Key not found.", 404)
    return row
