"""Shared pieces for the voice module (ADR 0021): errors, money, settings."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..inbox import InboxError, settings

CENT = Decimal("0.01")
MILLI = Decimal("0.0001")


class VoiceError(InboxError):
    """A voice request the service refuses; `code` maps to an HTTP status."""


def money(value: Any) -> Decimal:
    """Exact money from a string, int or Decimal. Floats are refused."""
    if isinstance(value, float):
        raise TypeError("money must not be a float")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value if value not in (None, "") else "0"))


def q4(d: Decimal) -> Decimal:
    return d.quantize(MILLI, rounding=ROUND_HALF_UP)


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def s(d: Decimal) -> str:
    """Money to a string for JSON (never a float)."""
    return format(d, "f")


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def month_start(at: dt.datetime | dt.date) -> dt.date:
    return dt.date(at.year, at.month, 1)


def next_month(d: dt.date) -> dt.date:
    return dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)


DEFAULT_POLICY = {
    # Who may play call recordings: "none", "own" (staff hear their own calls) or "admins".
    "recording_access": "own",
}


def policy(conn: psycopg.Connection, customer_id: Any) -> dict:
    """The business's voice settings, from commai_settings.config["voice"]."""
    cfg = (settings(conn, customer_id)["config"] or {}).get("voice") or {}
    return {**DEFAULT_POLICY, **cfg}


def set_policy(conn: psycopg.Connection, customer_id: Any, values: dict) -> dict:
    merged = {**policy(conn, customer_id), **values}
    conn.execute(
        "UPDATE commai_settings SET config = jsonb_set(config, '{voice}', %s), updated_at = now()"
        " WHERE customer_id = %s",
        (Jsonb(merged), customer_id),
    )
    return merged


def domain(customer_id: Any) -> str:
    """The business's PBX tenant (SIP domain)."""
    return f"c{str(customer_id).replace('-', '')[:12]}.voice.exacarib.internal"


def digits(number: str) -> str:
    return "".join(ch for ch in str(number) if ch.isdigit())
