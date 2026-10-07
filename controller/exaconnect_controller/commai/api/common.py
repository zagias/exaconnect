"""Shared pieces for the CommAI routers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException

from ..actions import ActionRefused
from ..attachments import AttachmentError
from ..inbox import InboxError


@contextmanager
def errors() -> Iterator[None]:
    """Turn service refusals into the API's one error shape: {"detail": "..."}."""
    try:
        yield
    except InboxError as e:
        raise HTTPException(e.code, str(e)) from e
    except ActionRefused as e:
        raise HTTPException(e.code, str(e)) from e
    except AttachmentError as e:
        raise HTTPException(e.code, str(e)) from e


def page(rows: list[dict], limit: int, key: str = "id") -> dict:
    """{"items": [...], "next": cursor or None}. Callers fetch limit + 1 rows."""
    more = len(rows) > limit
    items = rows[:limit]
    return {"items": items, "next": str(items[-1][key]) if more and items else None}


def page_after(rows: list[dict], cursor: str, limit: int, key: str = "id") -> dict:
    """Cursor paging over a short, fully loaded list (teams, rules, members...).
    `cursor` is the `next` value of the previous page ("" for the first page)."""
    if cursor:
        keys = [str(r[key]) for r in rows]
        if cursor not in keys:
            raise HTTPException(400, "That cursor is not valid any more. Start again from the first page.")
        rows = rows[keys.index(cursor) + 1 :]
    return page(rows[: limit + 1], limit, key)


def actor_of(user: Any) -> str:
    return user.actor
