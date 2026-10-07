"""Cursor pagination for lists (ADR 0039).

A list endpoint keeps its old answer (a plain list) when no `cursor` is
given. Passing `cursor` (empty for the first page) returns
{"items": [...], "next": cursor or None}. The cursor is the last row's
time and id, so rows with the same time are neither repeated nor skipped.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import HTTPException


def decode(cursor: str | None) -> tuple[dt.datetime | None, str]:
    if not cursor:
        return None, ""
    at, _, key = cursor.partition("|")
    try:
        return dt.datetime.fromisoformat(at), key
    except ValueError:
        raise HTTPException(422, "That cursor is not valid. Start again without one.") from None


def where(cursor: str | None, time_col: str = "created_at", id_col: str = "id", desc: bool = True) -> tuple[str, list]:
    """An SQL condition (starting with AND) and its parameters for rows after the cursor."""
    at, key = decode(cursor)
    if at is None:
        return "", []
    op = "<" if desc else ">"
    return f" AND ({time_col}, {id_col}::text) {op} (%s::timestamptz, %s)", [at, key]


def result(rows: list[dict], cursor: str | None, limit: int, time_key: str = "created_at", id_key: str = "id") -> Any:
    """Old shape without a cursor; a page (callers fetch limit + 1 rows) with one."""
    if cursor is None:
        return rows[:limit]
    more = len(rows) > limit
    items = rows[:limit]
    nxt = None
    if more and items:
        last = items[-1]
        t = last[time_key]
        nxt = f"{t.isoformat() if hasattr(t, 'isoformat') else t}|{last[id_key]}"
    return {"items": items, "next": nxt}
