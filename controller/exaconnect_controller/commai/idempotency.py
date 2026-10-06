"""Idempotency keys and rate limits for the API (ADR 0016).

Idempotency: any POST, PUT, PATCH or DELETE under /api/v1 may carry an
`Idempotency-Key` header. The first response for (caller, key) is stored for
24 hours and replayed for every repeat, with `Idempotent-Replayed: true`. A
repeat with a different body is refused (422) and a repeat that arrives
while the first is still running gets 409. The caller is identified by a
hash of its credentials, so two callers never share a key.

Rate limit: a simple per-caller budget (EXA_API_RATE_PER_MIN, default 600
requests a minute) answered with 429 and Retry-After. It is per process; a
shared limiter comes with more than one API process.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict, deque

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import db

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_STORED = 1_000_000
KEEP = "24 hours"


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return ""


def principal(scope: Scope) -> str:
    """A stable, non-reversible id for the caller's credentials."""
    auth = _header(scope, b"authorization")
    if not auth:
        cookie = _header(scope, b"cookie")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "exa_session":
                auth = "cookie:" + v
    return hashlib.sha256(auth.encode()).hexdigest() if auth else ""


async def _send_json(send: Send, status: int, detail: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            + (extra or []),
        }
    )
    await send({"type": "http.response.body", "body": body})


class RateLimit:
    def __init__(self, app: ASGIApp, per_minute: int | None = None):
        self.app = app
        self.per_minute = per_minute or int(os.environ.get("EXA_API_RATE_PER_MIN", "600"))
        self.hits: dict[str, deque] = defaultdict(deque)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            return await self.app(scope, receive, send)
        who = principal(scope) or (scope.get("client") or ("anon",))[0]
        now = time.monotonic()
        q = self.hits[who]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.per_minute:
            retry = max(1, int(60 - (now - q[0])))
            return await _send_json(
                send,
                429,
                "Too many requests. Slow down and try again shortly.",
                [(b"retry-after", str(retry).encode())],
            )
        q.append(now)
        if len(self.hits) > 50_000:  # forget idle callers
            for k in [k for k, v in self.hits.items() if not v or v[-1] < now - 60]:
                del self.hits[k]
        return await self.app(scope, receive, send)


class Idempotency:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in WRITE_METHODS
            or not scope["path"].startswith("/api/v1/")
            or db._pool is None
        ):
            return await self.app(scope, receive, send)
        key = _header(scope, b"idempotency-key").strip()
        if not key:
            return await self.app(scope, receive, send)
        if len(key) > 200:
            return await _send_json(send, 400, "Idempotency-Key must be at most 200 characters.")
        who = principal(scope)
        if not who:
            return await self.app(scope, receive, send)  # unauthenticated: the endpoint answers 401

        # Read the whole body so it can be hashed and replayed to the app.
        chunks: list[bytes] = []
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        body_hash = hashlib.sha256(scope["method"].encode() + scope["path"].encode() + body).hexdigest()

        with db.tx() as conn:
            conn.execute(f"DELETE FROM idempotency_keys WHERE created_at < now() - interval '{KEEP}'")
            row = conn.execute(
                """INSERT INTO idempotency_keys (principal, key, method, path, body_hash) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (principal, key) DO NOTHING RETURNING key""",
                (who, key, scope["method"], scope["path"], body_hash),
            ).fetchone()
            existing = None
            if row is None:
                existing = conn.execute(
                    "SELECT * FROM idempotency_keys WHERE principal = %s AND key = %s", (who, key)
                ).fetchone()
        if existing is not None:
            if existing["body_hash"] != body_hash:
                return await _send_json(send, 422, "This Idempotency-Key was already used for a different request.")
            if existing["status_code"] is None:
                return await _send_json(send, 409, "A request with this Idempotency-Key is still being processed.")
            stored = bytes(existing["response"] or b"")
            await send(
                {
                    "type": "http.response.start",
                    "status": existing["status_code"],
                    "headers": [
                        (b"content-type", existing["content_type"].encode()),
                        (b"content-length", str(len(stored)).encode()),
                        (b"idempotent-replayed", b"true"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": stored})
            return

        sent_body = False

        async def replay_receive() -> Message:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status_code = 500
        content_type = "application/json"
        out: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal status_code, content_type
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message.get("headers") or []:
                    if k.lower() == b"content-type":
                        content_type = v.decode("latin-1")
            elif message["type"] == "http.response.body":
                out.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_receive, capture)
        except Exception:
            with db.tx() as conn:
                conn.execute("DELETE FROM idempotency_keys WHERE principal = %s AND key = %s", (who, key))
            raise
        response = b"".join(out)
        with db.tx() as conn:
            if status_code >= 500 or status_code in (401, 429) or len(response) > MAX_STORED:
                # Not a final answer: let the caller retry with the same key.
                conn.execute("DELETE FROM idempotency_keys WHERE principal = %s AND key = %s", (who, key))
            else:
                conn.execute(
                    """UPDATE idempotency_keys SET status_code = %s, response = %s, content_type = %s
                       WHERE principal = %s AND key = %s""",
                    (status_code, response, content_type, who, key),
                )
