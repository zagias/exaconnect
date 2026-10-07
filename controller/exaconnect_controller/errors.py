"""One error shape for the whole API, and a request id on every response (ADR 0038).

Every error answers JSON like:

    {"detail": "Conversation not found.", "code": "not_found", "request_id": "3f2c..."}

`detail` is the message, as before, so existing clients keep working. `code`
is a stable word a program can branch on. Validation errors also carry
`errors`, the list of fields that were wrong, and their `detail` is the first
one in words. `request_id` is also in the X-Request-ID response header; a
caller may send its own X-Request-ID (letters, digits, '.', '_', '-', up to 64)
and it is echoed back, so support can find the request in the logs.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("exaconnect.errors")

HEADER = b"x-request-id"
_VALID = re.compile(r"[A-Za-z0-9._-]{1,64}")

CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "too_large",
    415: "unsupported_type",
    422: "invalid",
    423: "locked",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "unavailable",
    504: "timeout",
}


def code_for(status: int) -> str:
    if status in CODES:
        return CODES[status]
    return "internal_error" if status >= 500 else "error"


def request_id(scope: Scope) -> str:
    return (scope.get("state") or {}).get("request_id", "")


def body(status: int, detail: Any, scope: Scope | None = None, code: str = "", **extra: Any) -> dict:
    out = {"detail": detail, "code": code or code_for(status), "request_id": request_id(scope or {})}
    out.update(extra)
    return out


class RequestId:
    """Gives every request an id (the caller's X-Request-ID when it is sane)
    and returns it in the X-Request-ID header of every HTTP response."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        given = ""
        for k, v in scope.get("headers") or []:
            if k == HEADER:
                given = v.decode("latin-1").strip()
                break
        rid = given if _VALID.fullmatch(given) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers") or [] if k.lower() != HEADER]
                headers.append((HEADER, rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_with_id)


def _validation_detail(errors: list[dict]) -> str:
    if not errors:
        return "The request is not valid."
    first = errors[0]
    loc = [str(x) for x in first.get("loc", ()) if x not in ("body", "query", "path", "header")]
    where = ".".join(loc) or "request"
    return f"{where}: {first.get('msg', 'is not valid')}"


def install(app: FastAPI) -> None:
    """Install the request id middleware and the exception handlers."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if exc.detail is not None else ""
        return JSONResponse(
            body(exc.status_code, detail, request.scope), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors()
        ]
        return JSONResponse(
            body(422, _validation_detail(errors), request.scope, code="validation_error", errors=errors),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error, request %s", request_id(request.scope))
        return JSONResponse(
            body(500, "Something went wrong on our side. Quote the request id if you contact support.", request.scope),
            status_code=500,
            # This handler answers outside the middleware stack: add the id here.
            headers={"X-Request-ID": request_id(request.scope)},
        )

    app.add_middleware(RequestId)
