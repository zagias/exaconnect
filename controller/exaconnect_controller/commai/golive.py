"""Go-live registry (ADR 0028).

Jibsy switches on a country, channel, language, carrier or region only after
it has been tested there. Each capability has written criteria; an ExaCarib
admin records each one as met (with evidence) and only then may set the
capability to ``pilot`` (named customers) or ``on`` (everyone).

Modules declare their capabilities and criteria at import time with
``declare``; ``sync`` writes them to the database (new rows start off, and
existing status and checks are kept). Code asks ``enabled`` before using one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

KINDS = ("country", "channel", "language", "carrier", "region", "feature")

# Criteria every capability carries, whatever its kind.
BASE_CRITERIA: dict[str, str] = {
    "tested": "Tested end to end in this setting, with the test run recorded.",
    "security-review": "Security review done: data flows, credentials and tenant isolation checked.",
    "operations": "Runbook written: monitoring, alerts, support contacts and how to switch it off.",
}


class GoLiveError(Exception):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


@dataclass
class Capability:
    kind: str
    key: str
    name: str
    criteria: dict[str, str] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


_DECLARED: dict[tuple[str, str], Capability] = {}


def declare(
    kind: str,
    key: str,
    name: str,
    criteria: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
) -> Capability:
    """Declare a capability (idempotent). Criteria add to BASE_CRITERIA."""
    if kind not in KINDS:
        raise ValueError(f"unknown capability kind {kind!r}")
    cap = Capability(kind, key, name, {**BASE_CRITERIA, **(criteria or {})}, details or {})
    _DECLARED[(kind, key)] = cap
    return cap


def declared() -> list[Capability]:
    return list(_DECLARED.values())


def sync(conn: psycopg.Connection) -> None:
    """Write declared capabilities and criteria. Never changes status or checks."""
    for cap in _DECLARED.values():
        conn.execute(
            """INSERT INTO commai_capabilities (kind, key, name, details) VALUES (%s, %s, %s, %s)
               ON CONFLICT (kind, key) DO UPDATE SET name = EXCLUDED.name,
                 details = EXCLUDED.details || commai_capabilities.details""",
            (cap.kind, cap.key, cap.name, Jsonb(cap.details)),
        )
        for cid, text in cap.criteria.items():
            conn.execute(
                """INSERT INTO commai_capability_criteria (kind, key, criterion, text) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (kind, key, criterion) DO UPDATE SET text = EXCLUDED.text""",
                (cap.kind, cap.key, cid, text),
            )


def enabled(conn: psycopg.Connection, kind: str, key: str, customer_id: Any = None) -> bool:
    """True when the capability is on, or in pilot for this customer."""
    row = conn.execute("SELECT status FROM commai_capabilities WHERE kind = %s AND key = %s", (kind, key)).fetchone()
    if not row or row["status"] == "off":
        return False
    if row["status"] == "on":
        return True
    if customer_id is None:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM commai_capability_pilots WHERE kind = %s AND key = %s AND customer_id = %s",
            (kind, key, customer_id),
        ).fetchone()
        is not None
    )


def require(conn: psycopg.Connection, kind: str, key: str, customer_id: Any = None) -> None:
    if not enabled(conn, kind, key, customer_id):
        cap = _DECLARED.get((kind, key))
        label = cap.name if cap else key
        raise GoLiveError(f"{label} is not switched on yet. ExaCarib switches it on once its go-live checks pass.")


def get(conn: psycopg.Connection, kind: str, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM commai_capabilities WHERE kind = %s AND key = %s", (kind, key)).fetchone()
    if not row:
        return None
    row["criteria"] = conn.execute(
        """SELECT criterion, text, met, evidence, checked_by, checked_at FROM commai_capability_criteria
           WHERE kind = %s AND key = %s ORDER BY criterion""",
        (kind, key),
    ).fetchall()
    row["pilots"] = [
        str(r["customer_id"])
        for r in conn.execute(
            "SELECT customer_id FROM commai_capability_pilots WHERE kind = %s AND key = %s", (kind, key)
        ).fetchall()
    ]
    return row


def check(conn: psycopg.Connection, kind: str, key: str, criterion: str, met: bool, evidence: str, actor: str) -> None:
    if met and not evidence.strip():
        raise GoLiveError("Say what shows the criterion is met (a test run, a document, a review).", 422)
    cur = conn.execute(
        """UPDATE commai_capability_criteria SET met = %s, evidence = %s, checked_by = %s, checked_at = now()
           WHERE kind = %s AND key = %s AND criterion = %s""",
        (met, evidence.strip(), actor, kind, key, criterion),
    )
    if cur.rowcount == 0:
        raise GoLiveError("No such criterion.", 404)
    if not met:
        # A criterion that stops holding switches the capability off.
        conn.execute(
            "UPDATE commai_capabilities SET status = 'off', updated_by = %s, updated_at = now() "
            "WHERE kind = %s AND key = %s AND status <> 'off'",
            (actor, kind, key),
        )


def set_status(
    conn: psycopg.Connection, kind: str, key: str, status: str, actor: str, pilots: list[str] | None = None
) -> None:
    if status not in ("off", "pilot", "on"):
        raise GoLiveError("Status is off, pilot or on.", 422)
    cap = conn.execute(
        "SELECT 1 FROM commai_capabilities WHERE kind = %s AND key = %s FOR UPDATE", (kind, key)
    ).fetchone()
    if not cap:
        raise GoLiveError("No such capability.", 404)
    if status != "off":
        unmet = conn.execute(
            "SELECT text FROM commai_capability_criteria WHERE kind = %s AND key = %s AND NOT met ORDER BY criterion",
            (kind, key),
        ).fetchall()
        if unmet:
            raise GoLiveError("Not every go-live criterion is met: " + "; ".join(r["text"] for r in unmet))
    if status == "pilot" and not pilots:
        raise GoLiveError("Name at least one customer for a pilot.", 422)
    conn.execute(
        "UPDATE commai_capabilities SET status = %s, updated_by = %s, updated_at = now() WHERE kind = %s AND key = %s",
        (status, actor, kind, key),
    )
    if pilots is not None:
        conn.execute("DELETE FROM commai_capability_pilots WHERE kind = %s AND key = %s", (kind, key))
        for cid in pilots:
            conn.execute(
                "INSERT INTO commai_capability_pilots (kind, key, customer_id) VALUES (%s, %s, %s)", (kind, key, cid)
            )
