"""Cloud on-ramps ordered through the adapters (ADR 0026).

Used by the on-ramps API, by plain-English ordering (the ``onramp`` action
in ordering.py), and by the TMF622 and MEF LSO Sonata order APIs.
"""

from __future__ import annotations

import functools
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit
from .adapters.clouds import ADAPTERS, OnrampError
from .transport import Transport

PRICE_PER_MBPS_MONTH = Decimal("3.0")


def adapter(provider: str):
    a = ADAPTERS.get(provider)
    if a is None:
        raise OnrampError(f"On-ramps go to {', '.join(ADAPTERS)}.")
    return a


def _http(conn, o: dict, a) -> Transport:
    return Transport(conn, None, f"onramp:{a.key}", a.live(), {}, functools.partial(a.simulate, o))


def review(conn, customer_id: Any, req: dict) -> dict:
    """Check an on-ramp request: the normalised request, or OnrampError."""
    a = adapter(str(req.get("provider") or ""))
    detail = a.validate(req)
    name = str(req.get("name") or "").strip()[:80] or f"{a.name.split(' (')[0]} {detail.get('region', '')}".strip()
    site_id = None
    if req.get("site"):
        row = conn.execute(
            "SELECT id FROM sites WHERE customer_id = %s AND (name = %s OR id::text = %s)",
            (customer_id, str(req["site"]), str(req["site"])),
        ).fetchone()
        if row is None:
            raise OnrampError(f"There is no site called {req['site']}.")
        site_id = row["id"]
    return {"provider": a.key, "name": name, "site_id": site_id, "detail": detail}


def create(conn: psycopg.Connection, customer_id: Any, req: dict, actor: str, order_id: int | None = None) -> dict:
    r = review(conn, customer_id, req)
    a = adapter(r["provider"])
    o = conn.execute(
        """INSERT INTO connect_onramps (customer_id, provider, name, site_id, bandwidth_mbps, detail, simulated,
                                        order_id, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            a.key,
            r["name"],
            r["site_id"],
            r["detail"]["bandwidth_mbps"],
            Jsonb(r["detail"]),
            not a.live(),
            order_id,
            actor,
        ),
    ).fetchone()
    out = a.order(_http(conn, o, a), o)
    o = conn.execute(
        """UPDATE connect_onramps SET external_id = %s, provider_state = %s, status = %s, pairing = %s,
                  detail = detail || %s, updated_at = now() WHERE id = %s RETURNING *""",
        (
            out.get("external_id", ""),
            out.get("provider_state", ""),
            out.get("status", "pending"),
            Jsonb(out.get("pairing", {})),
            Jsonb({"next_step": out.get("next_step", "")}),
            o["id"],
        ),
    ).fetchone()
    audit.record(conn, actor, "onramp.create", o["name"], customer_id, {"id": o["id"], "provider": a.key})
    return o


def refresh(conn: psycopg.Connection, o: dict) -> dict:
    if o["status"] in ("deleted", "failed") or not o["external_id"]:
        return o
    a = adapter(o["provider"])
    st = a.check(_http(conn, o, a), o)
    return conn.execute(
        "UPDATE connect_onramps SET provider_state = %s, status = %s, updated_at = now() WHERE id = %s RETURNING *",
        (st["provider_state"], st["status"], o["id"]),
    ).fetchone()


def delete(conn: psycopg.Connection, o: dict, actor: str) -> dict:
    a = adapter(o["provider"])
    if o["external_id"] and o["status"] not in ("deleted",):
        a.delete(_http(conn, o, a), o)
    o = conn.execute(
        "UPDATE connect_onramps SET status = 'deleted', updated_at = now() WHERE id = %s RETURNING *", (o["id"],)
    ).fetchone()
    audit.record(conn, actor, "onramp.delete", o["name"], o["customer_id"], {"id": o["id"]})
    return o


def refresh_all(conn: psycopg.Connection) -> int:
    n = 0
    for o in conn.execute(
        "SELECT * FROM connect_onramps WHERE status IN ('ordering', 'pending') ORDER BY id LIMIT 50"
    ).fetchall():
        try:
            refresh(conn, o)
            n += 1
        except OnrampError:
            continue
    return n


def public(o: dict) -> dict:
    return {
        k: o[k]
        for k in (
            "id",
            "customer_id",
            "provider",
            "name",
            "site_id",
            "bandwidth_mbps",
            "external_id",
            "pairing",
            "provider_state",
            "status",
            "simulated",
            "order_id",
            "detail",
            "created_by",
            "created_at",
            "updated_at",
        )
    }
