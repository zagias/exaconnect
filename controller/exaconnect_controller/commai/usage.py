"""Metered usage, budgets and hard limits (ADR 0016).

Every billable unit (an AI reply, an outbound WhatsApp message, a call
minute) is recorded once, keyed by a reference so a retry never counts
twice. Reports read these rows directly, so they match the recorded events.

Meters in use: ai_reply, ai_tokens, copilot, message_out:<channel>,
voice_minute, ai_voice_minute. A hard monthly limit stops the metered
work; the alert level only warns.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import events

events.register("usage.alert", "usage.limit_reached")


def _month_start() -> dt.datetime:
    now = dt.datetime.now(dt.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def used(conn: psycopg.Connection, customer_id: Any, meter: str) -> float:
    row = conn.execute(
        "SELECT coalesce(sum(quantity), 0) AS q FROM usage_records WHERE customer_id = %s AND meter = %s AND at >= %s",
        (customer_id, meter, _month_start()),
    ).fetchone()
    return float(row["q"])


def allowed(conn: psycopg.Connection, customer_id: Any, meter: str, quantity: float = 1) -> bool:
    """False when this would pass the business's hard monthly limit."""
    lim = conn.execute(
        "SELECT monthly_hard FROM usage_limits WHERE customer_id = %s AND meter = %s", (customer_id, meter)
    ).fetchone()
    if not lim or lim["monthly_hard"] is None:
        return True
    return used(conn, customer_id, meter) + quantity <= float(lim["monthly_hard"])


def record(
    conn: psycopg.Connection,
    customer_id: Any,
    meter: str,
    quantity: float = 1,
    ref: str = "",
    detail: dict | None = None,
) -> bool:
    """Record usage once per (meter, ref). Returns False if it was already recorded."""
    row = conn.execute(
        """INSERT INTO usage_records (customer_id, meter, quantity, ref, detail) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, meter, ref) WHERE ref <> '' DO NOTHING RETURNING id""",
        (customer_id, meter, quantity, ref, Jsonb(detail or {})),
    ).fetchone()
    if row is None:
        return False
    lim = conn.execute(
        "SELECT monthly_alert, monthly_hard FROM usage_limits WHERE customer_id = %s AND meter = %s",
        (customer_id, meter),
    ).fetchone()
    if lim:
        total = used(conn, customer_id, meter)
        before = total - quantity
        for level, kind in ((lim["monthly_alert"], "usage.alert"), (lim["monthly_hard"], "usage.limit_reached")):
            if level is not None and before < float(level) <= total:
                events.emit(conn, customer_id, kind, {"meter": meter, "used": total, "level": float(level)}, meter)
    return True
