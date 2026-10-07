"""API versions, deprecation and the changelog (ADR 0031, docs/commai/api-policy.md).

- The API version is in the path (/api/v1). Within v1, changes only add.
- A deprecated endpoint keeps working until its sunset date (at least six
  months after it is deprecated) and every response carries:
    Deprecation: @<unix time>             (RFC 9745)
    Sunset: <HTTP date>                   (RFC 8594)
    Link: <successor>; rel="successor-version", <policy>; rel="deprecation"
- Every change is in the changelog, served at /api/v1/commai/changelog.

`apply(router)` marks the routes listed in DEPRECATIONS before the CommAI
router is mounted, so the headers come from one place.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from email.utils import format_datetime

from fastapi import Depends, Response
from fastapi.routing import APIRoute

API_VERSION = "v1"
POLICY_URL = "/api/v1/commai/api-policy"  # the policy and every deprecation, as JSON
MIN_NOTICE = dt.timedelta(days=182)


@dataclass(frozen=True)
class Deprecation:
    method: str
    path: str  # as mounted, e.g. /api/v1/commai/customers/{customer_id}/event-types
    deprecated: dt.date
    sunset: dt.date
    successor: str
    reason: str


DEPRECATIONS: list[Deprecation] = [
    Deprecation(
        "GET",
        "/api/v1/commai/customers/{customer_id}/event-types",
        dt.date(2026, 10, 7),
        dt.date(2027, 4, 7),
        "/api/v1/commai/customers/{customer_id}/event-catalogue",
        "Returns bare names. The event catalogue gives each type with its family and whether webhooks can carry it.",
    ),
]

CHANGELOG: list[dict] = [
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "Partners: links, switching into a linked business, consolidated view and statements."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "White-label branding and custom domains with DNS verification."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "Regions, home region and data-location report."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "OAuth 2.0 for partner apps (authorisation code with PKCE, refresh, revoke)."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "Sandboxes and sandbox keys: simulated providers only, no real sends."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "GET /customers/{customer_id}/event-catalogue."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "deprecated",
     "change": "GET /customers/{customer_id}/event-types: use event-catalogue. Sunset 2027-04-07."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "Website chat SDK: ExaCaribChat.on/off events, isOpen, and a queue for calls made before it loads."},
    {"date": "2026-10-07", "version": API_VERSION, "kind": "added",
     "change": "Go-live registry for countries, channels, languages, carriers and regions."},
]  # fmt: skip


def _midnight(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC)


def headers(d: Deprecation) -> dict[str, str]:
    return {
        "Deprecation": f"@{int(_midnight(d.deprecated).timestamp())}",
        "Sunset": format_datetime(_midnight(d.sunset), usegmt=True),
        "Link": f'<{d.successor}>; rel="successor-version", <{POLICY_URL}>; rel="deprecation"',
    }


def _dependency(d: Deprecation):
    h = headers(d)

    def mark(response: Response) -> None:
        response.headers.update(h)

    return mark


def check(deprecations: list[Deprecation] = DEPRECATIONS) -> None:
    for d in deprecations:
        if d.sunset - d.deprecated < MIN_NOTICE:
            raise ValueError(f"{d.method} {d.path}: sunset must be at least six months after deprecation")


def apply(router, mount_prefix: str = "/api/v1") -> int:
    """Mark deprecated routes on the CommAI router (before it is included in /api/v1)."""
    check()
    return _walk(router.routes, mount_prefix)


def _walk(routes, prefix: str) -> int:
    # Newer FastAPI keeps included routers as lazy wrappers (original_router and
    # include_context); the leaf APIRoute's dependencies and `deprecated` flag
    # are read when the effective route is built, so marking the leaf is enough.
    n = 0
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            n += _walk(inner.routes, prefix + getattr(route.include_context, "prefix", ""))
            continue
        if not isinstance(route, APIRoute):
            continue
        for d in DEPRECATIONS:
            if prefix + route.path == d.path and d.method in route.methods and not route.deprecated:
                route.dependencies.append(Depends(_dependency(d)))
                route.deprecated = True
                n += 1
    return n


def deprecations_view() -> list[dict]:
    return [
        {"method": d.method, "path": d.path, "deprecated": d.deprecated.isoformat(), "sunset": d.sunset.isoformat(),
         "successor": d.successor, "reason": d.reason}
        for d in DEPRECATIONS
    ]  # fmt: skip
