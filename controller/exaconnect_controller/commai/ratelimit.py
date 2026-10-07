"""API rate limits shared by every API process, kept in Postgres (ADR 0032).

Each caller (a hash of its credentials; the client address when it has none)
has a budget of requests a minute: EXA_API_RATE_PER_MIN (default 600), or the
key's own `api_keys.rate_per_min` when an ExaCarib admin has set one. One row
per caller per minute counts the requests (a fixed window, so a caller can
burst up to twice the budget across a minute boundary; simple and shared).

Every /api/v1 response carries X-RateLimit-Limit and X-RateLimit-Remaining; a
refusal is 429 with Retry-After, in the API's one error shape. Without a
database (unit tests of the app factory) it falls back to a per-process count.

`hit()` is also used directly for budgets that are not per caller, such as a
website visitor starting AI calls (bucket "widget-call:<key>").
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import defaultdict, deque

import psycopg
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import db, errors
from ..security import token_hash
from .idempotency import _header, principal

DEFAULT_PER_MIN = 600


def default_limit() -> int:
    return int(os.environ.get("EXA_API_RATE_PER_MIN", str(DEFAULT_PER_MIN)))


def hit(conn: psycopg.Connection, bucket: str, limit: int, window_s: int = 60) -> tuple[bool, int, int]:
    """Count one request in `bucket`. Returns (allowed, remaining, seconds until the window resets)."""
    row = conn.execute(
        """WITH w AS (SELECT to_timestamp(floor(extract(epoch FROM now()) / %(w)s) * %(w)s) AS start)
           INSERT INTO api_rate_counters (bucket, window_start, hits) SELECT %(b)s, start, 1 FROM w
           ON CONFLICT (bucket, window_start) DO UPDATE SET hits = api_rate_counters.hits + 1
           RETURNING hits,
                     ceil(extract(epoch FROM window_start + make_interval(secs => %(w)s) - now()))::int AS reset""",
        {"b": bucket, "w": window_s},
    ).fetchone()
    if random.random() < 0.01:  # forget old windows now and then
        conn.execute("DELETE FROM api_rate_counters WHERE window_start < now() - interval '1 day'")
    hits = int(row["hits"])
    return hits <= limit, max(0, limit - hits), max(1, int(row["reset"] or 1))


def _key_limit(conn: psycopg.Connection, auth: str) -> int | None:
    token = auth.removeprefix("Bearer ").strip()
    if not auth.startswith("Bearer exa_"):
        return None
    row = conn.execute(
        "SELECT rate_per_min FROM api_keys WHERE token_hash = %s AND revoked_at IS NULL", (token_hash(token),)
    ).fetchone()
    return int(row["rate_per_min"]) if row and row["rate_per_min"] else None


def check(who: str, auth: str, default: int) -> tuple[bool, int, int, int]:
    """(allowed, limit, remaining, reset seconds) for one API request."""
    with db.tx() as conn:
        limit = _key_limit(conn, auth) or default
        ok, remaining, reset = hit(conn, f"api:{who}", limit)
    return ok, limit, remaining, reset


async def _refuse(scope: Scope, send: Send, retry: int, limit: int) -> None:
    payload = json.dumps(errors.body(429, "Too many requests. Slow down and try again shortly.", scope)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"retry-after", str(retry).encode()),
                (b"x-ratelimit-limit", str(limit).encode()),
                (b"x-ratelimit-remaining", b"0"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RateLimit:
    def __init__(self, app: ASGIApp, per_minute: int | None = None):
        self.app = app
        self.per_minute = per_minute or default_limit()
        self.local: dict[str, deque] = defaultdict(deque)

    def _local(self, who: str) -> tuple[bool, int, int, int]:
        now = time.monotonic()
        q = self.local[who]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False, self.per_minute, 0, max(1, int(60 - (now - q[0])))
        q.append(now)
        if len(self.local) > 50_000:
            for k in [k for k, v in self.local.items() if not v or v[-1] < now - 60]:
                del self.local[k]
        return True, self.per_minute, self.per_minute - len(q), 60

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            return await self.app(scope, receive, send)
        who = principal(scope) or "ip:" + str((scope.get("client") or ("anon",))[0])
        if db._pool is None:
            ok, limit, remaining, reset = self._local(who)
        else:
            try:
                ok, limit, remaining, reset = await asyncio.to_thread(
                    check, who, _header(scope, b"authorization"), self.per_minute
                )
            except psycopg.Error:
                ok, limit, remaining, reset = self._local(who)  # the database is down: still limit
        if not ok:
            return await _refuse(scope, send, reset, limit)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        *(message.get("headers") or []),
                        (b"x-ratelimit-limit", str(limit).encode()),
                        (b"x-ratelimit-remaining", str(remaining).encode()),
                    ],
                }
            await send(message)

        return await self.app(scope, receive, send_with_headers)
