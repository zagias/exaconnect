"""Module entitlements (ADR 0033).

CommAI is sold as four modules: messaging, voice, AI agents and automation.
The shared inbox, contacts, notes, the developer platform, the platform
assistant, support cases, reports and the bill belong to every business.

Each module's routes check the business has the module, on the server, as a
router dependency (`dependencies_for`). A business with no entitlement rows
has every module, so businesses that existed before modules were sold keep
everything. Only an ExaCarib admin sets entitlements.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import Depends, HTTPException, Request

from .. import db
from . import events

MODULES = {
    "messaging": "Messaging (WhatsApp, SMS, email and website chat)",
    "voice": "Voice (phone system and calls)",
    "ai_agents": "AI agents",
    "automation": "Automation (workflows and integrations)",
}

# Which API module each router file belongs to. Router files not listed are core.
ROUTER_MODULES = {
    "channels": "messaging",
    "channels_global": "messaging",
    "voice": "voice",
    "voice_global": "voice",
    "ai": "ai_agents",
    "automation": "automation",
    "connectors_more": "automation",
    "integrations_catalogue": "automation",
}

# Paths inside a module's router that every business keeps: the platform
# assistant, support cases, outcome reports and usage limits.
CORE_PATHS = ("/assistant", "/support-cases", "/reports/", "/usage-limits")

events.register("entitlement.changed")


class NotEntitled(Exception):
    def __init__(self, module: str):
        super().__init__(f"{MODULES[module].split(' (')[0]} is not part of this business's plan.")
        self.module = module
        self.code = 403


def all_for(conn: psycopg.Connection, customer_id: Any) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT module, enabled FROM commai_entitlements WHERE customer_id = %s", (customer_id,)
    ).fetchall()
    have = {r["module"]: r["enabled"] for r in rows}
    return {m: have.get(m, True) for m in MODULES}


def enabled(conn: psycopg.Connection, customer_id: Any, module: str) -> bool:
    row = conn.execute(
        "SELECT enabled FROM commai_entitlements WHERE customer_id = %s AND module = %s", (customer_id, module)
    ).fetchone()
    return True if row is None else bool(row["enabled"])


def require(conn: psycopg.Connection, customer_id: Any, module: str) -> None:
    if not enabled(conn, customer_id, module):
        raise NotEntitled(module)


def set_modules(
    conn: psycopg.Connection, customer_id: Any, modules: dict[str, bool], actor: str, note: str = ""
) -> dict:
    bad = sorted(set(modules) - set(MODULES))
    if bad:
        raise ValueError(f"Unknown modules: {', '.join(bad)}.")
    before = all_for(conn, customer_id)
    for m, on in modules.items():
        conn.execute(
            """INSERT INTO commai_entitlements (customer_id, module, enabled, note, updated_by)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, module) DO UPDATE SET enabled = EXCLUDED.enabled, note = EXCLUDED.note,
                 updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (customer_id, m, bool(on), note[:300], actor),
        )
    after = all_for(conn, customer_id)
    changed = {m: after[m] for m in MODULES if before[m] != after[m]}
    if changed:
        events.emit(conn, customer_id, "entitlement.changed", {"modules": changed, "by": actor}, "entitlements")
    return after


def _guard(module: str):
    from ..api.deps import current_user  # here, not at import: the API package imports this module

    def check(request: Request, user: Any = Depends(current_user)) -> None:  # signed in first, then the plan
        cid = request.path_params.get("customer_id")
        if not cid:
            return  # unauthenticated endpoints (widget, provider webhooks) carry no business in the path
        path = request.url.path
        if any(p in path for p in CORE_PATHS):
            return
        try:
            with db.tx() as conn:
                ok = enabled(conn, cid, module)
        except Exception:  # noqa: BLE001 - a malformed id is the route's own 404/422 to give
            return
        if not ok:
            raise HTTPException(403, str(NotEntitled(module)))

    return check


def dependencies_for(router_module: str) -> list:
    """Router dependencies for an API module file (by its short name)."""
    module = ROUTER_MODULES.get(router_module.rsplit(".", 1)[-1])
    return [Depends(_guard(module))] if module else []
