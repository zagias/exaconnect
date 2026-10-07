"""A small HTTP layer for connectors (ADR 0020).

Connectors call `request()`; tests replace `transport` with a fake so no
real provider is ever called. Errors never include tokens: the
Authorization header is not logged and response bodies are trimmed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("exaconnect.commai.http")


@dataclass
class Response:
    status: int
    body: Any  # parsed JSON, or text when the body isn't JSON
    headers: dict


Transport = Callable[[str, str, dict, bytes | None, float], Response]


def _urllib_transport(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> Response:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed provider hosts
            raw, status, hdrs = r.read(2_000_000), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(200_000), e.code, dict(e.headers or {})
    text = raw.decode("utf-8", "replace")
    try:
        body: Any = json.loads(text) if text else {}
    except ValueError:
        ctype = str({k.lower(): v for k, v in hdrs.items()}.get("content-type", ""))
        # XML (WebDAV multistatus), iCalendar and vCard documents are kept whole.
        body = text if any(t in ctype for t in ("xml", "calendar", "vcard")) else text[:2000]
    return Response(status, body, hdrs)


transport: Transport = _urllib_transport


class NetworkError(Exception):
    pass


def request(
    method: str,
    url: str,
    *,
    token: str = "",
    json_body: Any = None,
    form: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    content: bytes | None = None,
    basic: tuple[str, str] | None = None,
    timeout: float = 20,
) -> Response:
    """`content` sends raw bytes (set Content-Type in `headers`), for XML
    (CalDAV, CardDAV) and other non-JSON bodies. `basic` is HTTP Basic auth."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    h = {"Accept": "application/json", **(headers or {})}
    data: bytes | None = None
    if token:
        h["Authorization"] = f"Bearer {token}"
    elif basic:
        import base64

        h["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    if json_body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
    elif content is not None:
        data = content
    elif form is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    try:
        return transport(method, url, h, data, timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("%s %s unreachable: %s", method, url.split("?")[0], type(e).__name__)
        raise NetworkError(f"{urllib.parse.urlsplit(url).hostname} could not be reached.") from None
