"""Secrets at rest (ADR 0020): OAuth tokens and private-app tokens are
encrypted with Fernet (AES-128-CBC with HMAC-SHA256) under a key read from
EXA_SECRETS_KEY. The plaintext is never returned by the API and never logged.

Make a key with:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import json
import os
from typing import Any

import psycopg
from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    """Secrets can't be stored or read; the message is safe to show."""


def _fernet() -> Fernet:
    key = os.environ.get("EXA_SECRETS_KEY", "")
    if not key:
        raise VaultError("Secure storage is not set up: the controller needs EXA_SECRETS_KEY.")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise VaultError("EXA_SECRETS_KEY is not a valid Fernet key.") from None


def configured() -> bool:
    try:
        _fernet()
        return True
    except VaultError:
        return False


def put(conn: psycopg.Connection, customer_id: Any, purpose: str, value: dict, ref: str = "") -> str:
    """Encrypt and store `value`; returns its reference. Replaces `ref` if given."""
    token = _fernet().encrypt(json.dumps(value).encode()).decode()
    if ref:
        row = conn.execute(
            "UPDATE commai_secrets SET ciphertext = %s, updated_at = now() WHERE id::text = %s AND customer_id = %s"
            " RETURNING id",
            (token, ref, customer_id),
        ).fetchone()
        if row:
            return str(row["id"])
    row = conn.execute(
        "INSERT INTO commai_secrets (customer_id, purpose, ciphertext) VALUES (%s, %s, %s) RETURNING id",
        (customer_id, purpose, token),
    ).fetchone()
    return str(row["id"])


def get(conn: psycopg.Connection, customer_id: Any, ref: str) -> dict | None:
    if not ref:
        return None
    row = conn.execute(
        "SELECT ciphertext FROM commai_secrets WHERE id::text = %s AND customer_id = %s", (ref, customer_id)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(_fernet().decrypt(row["ciphertext"].encode()))
    except InvalidToken:
        raise VaultError("A stored secret could not be decrypted (was EXA_SECRETS_KEY changed?).") from None


def delete(conn: psycopg.Connection, customer_id: Any, ref: str) -> None:
    if ref:
        conn.execute("DELETE FROM commai_secrets WHERE id::text = %s AND customer_id = %s", (ref, customer_id))
