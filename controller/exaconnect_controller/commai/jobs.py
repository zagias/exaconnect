"""Durable work queue on Postgres (ADR 0016).

Jobs are rows. A worker claims ready jobs with FOR UPDATE SKIP LOCKED and a
lease; a worker that dies leaves its lease to expire, and the job is picked
up again. Handlers must therefore be safe to run twice: each one keys its
external effect on something stable (a message id, an idempotency key).

A dedupe key makes enqueueing itself idempotent: the same key never creates
a second job, so a retried request or a repeated provider callback cannot
queue the same work twice.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import db

log = logging.getLogger("exaconnect.commai.jobs")

Handler = Callable[[psycopg.Connection, dict], "Later | None"]
_handlers: dict[str, Handler] = {}

LEASE_S = 60


class Retry(Exception):
    """Raise from a handler to roll back its work and try again later."""

    def __init__(self, message: str, delay_s: float = 30):
        super().__init__(message)
        self.delay_s = delay_s


class Later:
    """Return from a handler to keep (commit) its work and try again later,
    for example after recording a failed delivery attempt."""

    def __init__(self, message: str, delay_s: float = 30):
        self.message = message
        self.delay_s = delay_s


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Register the function that runs jobs of this kind."""

    def wrap(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return wrap


def enqueue(
    conn: psycopg.Connection,
    kind: str,
    payload: dict | None = None,
    *,
    customer_id: Any = None,
    dedupe_key: str | None = None,
    delay_s: float = 0,
    max_attempts: int = 6,
) -> int | None:
    """Queue a job in the caller's transaction. Returns its id, or None when the
    dedupe key was already used (the job exists already)."""
    row = conn.execute(
        """INSERT INTO jobs (kind, customer_id, payload, dedupe_key, run_after, max_attempts)
           VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s)
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
        (kind, customer_id, Jsonb(payload or {}), dedupe_key, delay_s, max_attempts),
    ).fetchone()
    return row["id"] if row else None


def _backoff(attempts: int) -> float:
    return min(5 * 2 ** (attempts - 1), 3600)


def claim(limit: int = 20, kinds: list[str] | None = None) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            f"""UPDATE jobs SET status = 'running', attempts = attempts + 1,
                       locked_until = now() + interval '{LEASE_S} seconds'
                WHERE id IN (
                  SELECT id FROM jobs
                  WHERE ((status = 'queued' AND run_after <= now())
                         OR (status = 'running' AND locked_until < now()))
                    AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s::text[]))
                  ORDER BY run_after, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED)
                RETURNING *""",
            {"limit": limit, "kinds": kinds},
        ).fetchall()


def _finish(job_id: int, ok: bool, error: str = "", delay_s: float | None = None, dead: bool = False) -> None:
    with db.tx() as conn:
        if ok:
            conn.execute(
                "UPDATE jobs SET status = 'done', finished_at = now(), last_error = '' WHERE id = %s", (job_id,)
            )
        elif dead:
            conn.execute(
                "UPDATE jobs SET status = 'dead', finished_at = now(), last_error = %s WHERE id = %s",
                (error[:2000], job_id),
            )
        else:
            conn.execute(
                """UPDATE jobs SET status = 'queued', locked_until = NULL, last_error = %s,
                          run_after = now() + make_interval(secs => %s) WHERE id = %s""",
                (error[:2000], delay_s or 0, job_id),
            )


def run_job(job: dict) -> bool:
    """Run one claimed job in its own transaction. True if it finished."""
    fn = _handlers.get(job["kind"])
    if fn is None:
        _finish(job["id"], False, f"no handler for {job['kind']}", dead=True)
        return False
    try:
        with db.tx() as conn:
            out = fn(conn, job)
    except Retry as e:
        dead = job["attempts"] >= job["max_attempts"]
        _finish(job["id"], False, str(e), delay_s=e.delay_s, dead=dead)
        if dead:
            _on_dead(job, str(e))
        return False
    except Exception as e:  # noqa: BLE001 - a failing handler must not stop the worker
        log.warning("job %s (%s) failed: %s", job["id"], job["kind"], e)
        err = f"{type(e).__name__}: {e}"
        dead = job["attempts"] >= job["max_attempts"]
        _finish(job["id"], False, err, delay_s=_backoff(job["attempts"]), dead=dead)
        if dead:
            log.error("job %s dead after %s attempts\n%s", job["id"], job["attempts"], traceback.format_exc())
            _on_dead(job, err)
        return False
    if isinstance(out, Later):
        dead = job["attempts"] >= job["max_attempts"]
        _finish(job["id"], False, out.message, delay_s=out.delay_s * 2 ** (job["attempts"] - 1), dead=dead)
        if dead:
            _on_dead(job, out.message)
        return False
    _finish(job["id"], True)
    return True


_dead_hooks: dict[str, Callable[[dict, str], None]] = {}


def on_dead(kind: str) -> Callable[[Callable[[dict, str], None]], Callable[[dict, str], None]]:
    """Register what to do when a job of this kind gives up (mark a message failed...)."""

    def wrap(fn):
        _dead_hooks[kind] = fn
        return fn

    return wrap


def _on_dead(job: dict, error: str) -> None:
    hook = _dead_hooks.get(job["kind"])
    if hook:
        try:
            hook(job, error)
        except Exception:  # noqa: BLE001
            log.exception("dead-job hook for %s failed", job["kind"])


def run_pending(max_rounds: int = 50, kinds: list[str] | None = None) -> int:
    """Run everything that is ready now (tests and the worker). Returns jobs run."""
    n = 0
    for _ in range(max_rounds):
        batch = claim(kinds=kinds)
        if not batch:
            break
        for job in batch:
            run_job(job)
            n += 1
    return n


async def loop(interval_s: float = 1.0) -> None:
    while True:
        try:
            await asyncio.to_thread(run_pending, 5)
        except Exception:  # noqa: BLE001
            log.exception("job worker pass failed")
        await asyncio.sleep(interval_s)
