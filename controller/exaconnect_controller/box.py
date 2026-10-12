"""Cloud management of ExaCarib boxes (ADR 0049).

A box that runs the box console (ADR 0046) has a "Managed from" setting:

- ``site``: only the box console changes the box's own settings;
- ``cloud``: only the portal does, and the console reads, apart from an
  emergency take-back that switches the box to ``site``;
- ``both``: either does, and the newest change wins.

The portal's copy lives on the node (``nodes.box_managed``) and goes down in
desired state; the box's copy comes up with telemetry. Each carries the time
it was set, and each side keeps the newest, so a switch made on either side
reaches the other however the two were cut off from each other meanwhile.
Switching never changes the box's settings themselves.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import audit, desired

MODES = ("site", "cloud", "both")
DEFAULT = "both"
NAMES = {"site": "on site", "cloud": "the cloud", "both": "on site and the cloud"}
_EPOCH = dt.datetime(1, 1, 1, tzinfo=dt.UTC)


def _at(m: dict | None) -> dt.datetime:
    if not m or not m.get("at"):
        return _EPOCH
    at = m["at"]
    if isinstance(at, str):
        at = dt.datetime.fromisoformat(at)
    return at if at.tzinfo else at.replace(tzinfo=dt.UTC)


def managed(node: dict) -> dict[str, Any]:
    """The node's setting as the portal knows it, with the default filled in."""
    m = node.get("box_managed") or {}
    return {"mode": m.get("mode") if m.get("mode") in MODES else DEFAULT, "at": m.get("at"), "by": m.get("by")}


def block(node: dict) -> dict[str, Any] | None:
    """The desired-state ``box`` block for a node, or None for nodes without a box console."""
    if not (node.get("box") or {}).get("console"):
        return None
    m = node.get("box_managed")
    if not m:
        return {}
    return {"managed_from": {"mode": m["mode"], "at": m["at"], "by": m.get("by") or ""}}


def set_from_portal(conn: psycopg.Connection, node: dict, mode: str, actor: str) -> dict[str, Any]:
    """Switch a box from the portal. Returns the new setting."""
    if mode not in MODES:
        raise ValueError(mode)
    now = dt.datetime.now(dt.UTC)
    cur = node.get("box_managed")
    if _at(cur) >= now:
        # The box's clock ran ahead of ours: still make this the newest.
        now = _at(cur) + dt.timedelta(milliseconds=1)
    m = {"mode": mode, "at": now.isoformat(), "by": f"Connect portal ({actor.removeprefix('user:')})"}
    conn.execute("UPDATE nodes SET box_managed = %s WHERE id = %s", (Jsonb(m), node["id"]))
    audit.record(conn, actor, "box.managed", node["name"], node["customer_id"], {"mode": mode})
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, 'box_managed', %s)",
        (now, node["customer_id"], node["id"], Jsonb({"mode": mode, "by": m["by"], "where": "portal"})),
    )
    desired.refresh(conn, node["customer_id"])
    return m


def from_box(conn: psycopg.Connection, node: Any, reported: dict | None) -> bool:
    """Take the box's setting when it is newer than the portal's. Returns whether it changed."""
    if not reported or reported.get("mode") not in MODES:
        return False
    at = _at(reported)
    if at == _EPOCH:
        return False  # never set on the box: the portal's stands
    row = conn.execute("SELECT box_managed FROM nodes WHERE id = %s FOR UPDATE", (node.id,)).fetchone()
    cur = row["box_managed"] if row else None
    if at <= _at(cur):
        return False
    # Kept as the box sent it, even from a box clock running fast, so the two
    # copies agree and stop; a portal switch after it still counts as newer.
    m = {"mode": reported["mode"], "at": at.isoformat(), "by": (reported.get("by") or "box console")[:120]}
    conn.execute("UPDATE nodes SET box_managed = %s WHERE id = %s", (Jsonb(m), node.id))
    if not cur or cur.get("mode") != m["mode"]:
        audit.record(
            conn, f"node:{node.name}", "box.managed", node.name, node.customer_id, {"mode": m["mode"], "by": m["by"]}
        )
    desired.refresh(conn, node.customer_id)
    return True
