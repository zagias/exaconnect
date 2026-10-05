"""DDoS protection on the PoP's public address (ADR 0012). ExaCarib's admins
set the limits and keep the block list; it applies to every customer on the
PoP, since they share the address."""

from __future__ import annotations

import datetime as dt
import ipaddress
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from .. import audit, db, desired
from .deps import AdminDep

router = APIRouter(tags=["protection"])

PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "0.0.0.0/8")

SETTINGS = {
    "enabled": "ddos_enabled",
    "new_per_source": "ddos_new_per_source",
    "syn_per_s": "ddos_syn_per_s",
    "block_minutes": "ddos_block_minutes",
}


class ProtectionPatch(BaseModel):
    enabled: bool | None = None
    new_per_source: int | None = Field(default=None, ge=1, le=100000)
    syn_per_s: int | None = Field(default=None, ge=10, le=1000000)
    block_minutes: int | None = Field(default=None, ge=1, le=1440)


class BlockIn(BaseModel):
    prefix: str = Field(max_length=18)
    reason: str = Field(default="", max_length=200)
    hours: int | None = Field(default=None, ge=1, le=24 * 365)


def _refresh_all(conn) -> None:
    for r in conn.execute("SELECT DISTINCT customer_id FROM sites WHERE kind = 'pop'").fetchall():
        desired.refresh(conn, r["customer_id"])


def _view(conn) -> dict[str, Any]:
    pops = conn.execute(
        """SELECT s.id, s.name, s.ddos_enabled, s.ddos_new_per_source, s.ddos_syn_per_s, s.ddos_block_minutes,
                  st.counters, st.auto_blocked, st.updated_at
           FROM sites s LEFT JOIN nodes n ON n.site_id = s.id LEFT JOIN internet_state st ON st.node_id = n.id
           WHERE s.kind = 'pop' ORDER BY s.name"""
    ).fetchall()
    first = pops[0] if pops else None
    dropped = {"blocked": 0, "auto": 0, "flood": 0, "syn": 0}
    auto: list[dict] = []
    for p in pops:
        for c in p["counters"] or []:
            if c.get("kind") in dropped:
                dropped[c["kind"]] += int(c.get("packets") or 0)
        # Times left are as of the PoP's last report: count them down from then,
        # and drop blocks that have run out since, so a silent PoP shows nothing stale.
        age = (dt.datetime.now(dt.UTC) - p["updated_at"]).total_seconds() if p["updated_at"] else 0
        for b in p["auto_blocked"] or []:
            left = b.get("expires_s")
            if left is not None:
                left = int(left - age)
                if left <= 0:
                    continue
            auto.append({**b, "expires_s": left, "pop": p["name"]})
    return {
        "settings": {k: first[col] for k, col in SETTINGS.items()} if first else None,
        "pops": [p["name"] for p in pops],
        "dropped": dropped,
        "auto_blocked": sorted(auto, key=lambda b: -b.get("expires_s", 0)),
        "blocklist": conn.execute(
            """SELECT id, prefix::text AS prefix, reason, created_by, created_at, expires_at FROM blocked_sources
               WHERE expires_at IS NULL OR expires_at > now() ORDER BY created_at DESC"""
        ).fetchall(),
        "updated_at": max((p["updated_at"] for p in pops if p["updated_at"]), default=None),
    }


@router.get("/admin/protection")
def get_protection(user: AdminDep) -> dict:
    with db.tx() as conn:
        return _view(conn)


@router.patch("/admin/protection")
def set_protection(body: ProtectionPatch, user: AdminDep) -> dict:
    """The limits for every PoP."""
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    with db.tx() as conn:
        for k, v in changes.items():
            conn.execute(f"UPDATE sites SET {SETTINGS[k]} = %s WHERE kind = 'pop'", (v,))  # noqa: S608
        audit.record(conn, user.actor, "protection.update", "PoP DDoS protection", None, changes)
        _refresh_all(conn)
        return _view(conn)


@router.post("/admin/protection/blocklist", status_code=201)
def block(body: BlockIn, user: AdminDep) -> dict:
    try:
        net = ipaddress.ip_network(body.prefix.strip(), strict=False)
    except ValueError:
        raise HTTPException(400, "Give an address or range, like 203.0.113.9 or 203.0.113.0/24.") from None
    if net.version != 4 or net.prefixlen < 8:
        raise HTTPException(400, "Block an IPv4 address or a range no wider than a /8.")
    if any(net.subnet_of(ipaddress.ip_network(p)) for p in PRIVATE):
        raise HTTPException(400, "That is a private address: it can't reach the PoP's public address.")
    with db.tx() as conn:
        row = conn.execute(
            """INSERT INTO blocked_sources (prefix, reason, created_by, expires_at)
               VALUES (%s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(hours => %s) END)
               RETURNING id, prefix::text AS prefix, reason, created_by, created_at, expires_at""",
            (str(net), body.reason.strip(), user.actor, body.hours, body.hours),
        ).fetchone()
        audit.record(conn, user.actor, "protection.block", str(net), None, {"reason": body.reason, "hours": body.hours})
        _refresh_all(conn)
        return row


@router.delete("/admin/protection/blocklist/{block_id}", status_code=204)
def unblock(block_id: int, user: AdminDep) -> Response:
    with db.tx() as conn:
        row = conn.execute(
            "DELETE FROM blocked_sources WHERE id = %s RETURNING prefix::text AS p", (block_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Not on the block list.")
        audit.record(conn, user.actor, "protection.unblock", row["p"], None)
        _refresh_all(conn)
    return Response(status_code=204)
