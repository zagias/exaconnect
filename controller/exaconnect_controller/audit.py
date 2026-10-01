"""Audit log: every write through the API records who did what."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def record(
    conn: psycopg.Connection,
    actor: str,
    action: str,
    target: str = "",
    customer_id: Any = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (customer_id, actor, action, target, detail) VALUES (%s, %s, %s, %s, %s)",
        (customer_id, actor, action, target, Jsonb(detail or {})),
    )
