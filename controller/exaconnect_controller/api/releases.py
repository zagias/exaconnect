"""Releases on this server (ADR 0025): what is live, what came before, and every rollback."""

from __future__ import annotations

import os

from fastapi import APIRouter

from .. import __version__, db
from .deps import AdminDep

router = APIRouter(tags=["ops"])


def build_commit() -> str:
    return os.environ.get("EXA_BUILD_COMMIT", "") or "dev"


@router.get("/releases")
def list_releases(user: AdminDep, limit: int = 30) -> dict:
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT id, commit, previous, started_at, finished_at, status, kind, backup IS NOT NULL AS backed_up,
                      detail
               FROM releases ORDER BY id DESC LIMIT %s""",
            (min(max(limit, 1), 200),),
        ).fetchall()
    return {"running": {"version": __version__, "commit": build_commit()}, "releases": rows}
