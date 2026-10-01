"""One open insight per (customer, key). Raising it again refreshes it;
resolving keys that were not seen in a pass closes the rest."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def raise_insight(
    conn: psycopg.Connection,
    customer_id: Any,
    kind: str,
    key: str,
    severity: str,
    title: str,
    detail: str,
    data: dict | None = None,
    site_id: Any = None,
    link_id: Any = None,
    carrier_id: Any = None,
    example: bool = False,
) -> tuple[int, bool]:
    """Returns (id, new). A new insight also lands in the event timeline."""
    row = conn.execute(
        """UPDATE insights SET severity = %s, title = %s, detail = %s, data = %s, last_seen = now()
           WHERE customer_id = %s AND key = %s AND resolved_at IS NULL RETURNING id""",
        (severity, title, detail, Jsonb(data or {}), customer_id, key),
    ).fetchone()
    if row:
        return row["id"], False
    row = conn.execute(
        """INSERT INTO insights (customer_id, kind, key, severity, site_id, link_id, carrier_id, title, detail,
                                 data, example)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (customer_id, kind, key, severity, site_id, link_id, carrier_id, title, detail, Jsonb(data or {}), example),
    ).fetchone()
    conn.execute(
        "INSERT INTO events (time, customer_id, kind, detail) VALUES (now(), %s, 'insight', %s)",
        (customer_id, Jsonb({"insight": row["id"], "kind": kind, "severity": severity, "title": title})),
    )
    return row["id"], True


def resolve_others(conn: psycopg.Connection, customer_id: Any, kind: str, seen: set[str], example: bool = False) -> int:
    """Resolves open insights of this kind whose key was not raised this pass."""
    return conn.execute(
        """UPDATE insights SET resolved_at = now()
           WHERE customer_id = %s AND kind = %s AND example = %s AND resolved_at IS NULL AND NOT (key = ANY(%s))""",
        (customer_id, kind, example, list(seen)),
    ).rowcount
