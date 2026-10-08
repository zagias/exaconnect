"""One open insight per (customer, key). Raising it again refreshes it;
resolving keys that were not seen in a pass closes the rest.

No duplicate notifications: an insight lands in the event timeline once,
when it is first raised, and again only if its severity goes up. A key
that comes back within a day (a feed that drops an event for one pass, a
storm that wobbles back into range) reopens its old insight, keeping its
acknowledgement, rather than raising a new one.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

RANK = {"info": 0, "warning": 1, "critical": 2}


def _event(
    conn: psycopg.Connection,
    customer_id: Any,
    insight_id: int,
    kind: str,
    severity: str,
    title: str,
    escalated: bool = False,
) -> None:
    detail = {"insight": insight_id, "kind": kind, "severity": severity, "title": title}
    if escalated:
        detail["escalated"] = True
    conn.execute(
        "INSERT INTO events (time, customer_id, kind, detail) VALUES (now(), %s, 'insight', %s)",
        (customer_id, Jsonb(detail)),
    )


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
    """Returns (id, notified): notified is true when the insight went into the
    event timeline (new, or more severe than before)."""
    old = conn.execute(
        """SELECT id, severity, resolved_at FROM insights
           WHERE customer_id = %s AND key = %s
             AND (resolved_at IS NULL OR resolved_at > now() - interval '24 hours')
           ORDER BY resolved_at IS NULL DESC, resolved_at DESC LIMIT 1""",
        (customer_id, key),
    ).fetchone()
    if old:
        escalated = RANK[severity] > RANK[old["severity"]]
        conn.execute(
            """UPDATE insights SET severity = %s, title = %s, detail = %s, data = %s, site_id = %s,
                 last_seen = now(), resolved_at = NULL,
                 acknowledged_by = CASE WHEN %s THEN NULL ELSE acknowledged_by END,
                 acknowledged_at = CASE WHEN %s THEN NULL ELSE acknowledged_at END
               WHERE id = %s""",
            (severity, title, detail, Jsonb(data or {}), site_id, escalated, escalated, old["id"]),
        )
        if escalated:
            _event(conn, customer_id, old["id"], kind, severity, title, escalated=True)
        return old["id"], escalated
    row = conn.execute(
        """INSERT INTO insights (customer_id, kind, key, severity, site_id, link_id, carrier_id, title, detail,
                                 data, example)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (customer_id, kind, key, severity, site_id, link_id, carrier_id, title, detail, Jsonb(data or {}), example),
    ).fetchone()
    _event(conn, customer_id, row["id"], kind, severity, title)
    return row["id"], True


def resolve_others(conn: psycopg.Connection, customer_id: Any, kind: str, seen: set[str], example: bool = False) -> int:
    """Resolves open insights of this kind whose key was not raised this pass."""
    return conn.execute(
        """UPDATE insights SET resolved_at = now()
           WHERE customer_id = %s AND kind = %s AND example = %s AND resolved_at IS NULL AND NOT (key = ANY(%s))""",
        (customer_id, kind, example, list(seen)),
    ).rowcount


def resolve_kind(conn: psycopg.Connection, kind: str) -> int:
    """Resolves every open insight of a kind (not example data), for a watch
    that has been switched off: nothing will refresh them any more."""
    return conn.execute(
        "UPDATE insights SET resolved_at = now() WHERE kind = %s AND NOT example AND resolved_at IS NULL", (kind,)
    ).rowcount


def open_customers(conn: psycopg.Connection, kind: str, example: bool) -> list[Any]:
    """Customers with open insights of this kind, so a pass can resolve them
    even when the customer no longer has a site it would look at."""
    return [
        r["customer_id"]
        for r in conn.execute(
            "SELECT DISTINCT customer_id FROM insights WHERE kind = %s AND example = %s AND resolved_at IS NULL",
            (kind, example),
        )
    ]


def note_storm_mode(conn: psycopg.Connection, warning: dict, advice: str) -> None:
    """When every site the warning marks critical is already in Storm Mode,
    say so instead of advising the customer to switch it on."""
    critical = [s for s in warning["data"].get("sites", []) if s.get("severity") == "critical"]
    if not critical:
        return
    on = {
        str(r["id"])
        for r in conn.execute(
            "SELECT id FROM sites WHERE id = ANY(%s::uuid[]) AND storm_mode", ([s["id"] for s in critical],)
        )
    }
    if all(s["id"] in on for s in critical):
        names = ", ".join(s["name"] for s in critical)
        warning["detail"] = warning["detail"].replace(advice, f"Storm Mode is already on at {names}.")
        warning["data"]["storm_mode_on"] = True
