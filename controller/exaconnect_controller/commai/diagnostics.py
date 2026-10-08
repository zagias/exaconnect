"""Diagnostic checks the platform assistant runs (ADR 0016).

Each module registers checks that look at one business's real configuration
and recent failures and return findings:

  {"area": "whatsapp", "status": "ok" | "problem" | "unknown",
   "confidence": "confirmed" | "likely" | "unknown",
   "summary": "...", "evidence": [str, ...], "fix": {"id": "...", "label": "...", "params": {...}} | None}

A check only reads. Fixes are applied by the assistant through its own
fixed list, after an admin approves.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg

Check = Callable[[psycopg.Connection, Any], list[dict]]
_checks: dict[str, Check] = {}


def register(name: str) -> Callable[[Check], Check]:
    def wrap(fn: Check) -> Check:
        _checks[name] = fn
        return fn

    return wrap


def run(conn: psycopg.Connection, customer_id: Any, only: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    for name, fn in sorted(_checks.items()):
        if only and name not in only:
            continue
        try:
            out.extend(fn(conn, customer_id))
        except Exception as e:  # noqa: BLE001 - a broken check is itself a finding
            out.append(
                {
                    "area": name,
                    "status": "unknown",
                    "confidence": "unknown",
                    "summary": f"The {name} check could not run.",
                    "evidence": [f"{type(e).__name__}"],
                    "fix": None,
                }
            )
    return out


def names() -> list[str]:
    return sorted(_checks)
