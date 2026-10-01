"""Storm Mode (CLAUDE.md §4.4, ADR 0004): one switch per customer.

On: the satellite path joins the steering lists of classes allowed on it, its
probes speed up so it is measured warm, and the engine shortens its hold
time and lengthens its forecast horizon. Off: all of that reverses, and any
class on satellite is moved back by the engine's next pass.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit, desired


def set_storm(
    conn: psycopg.Connection, customer_id: Any, on: bool, actor: str, allow_bulk_sat: bool | None = None
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT storm_mode, storm_allow_bulk_sat FROM customers WHERE id = %s FOR UPDATE", (customer_id,)
    ).fetchone()
    if row is None:
        raise LookupError("customer not found")
    bulk = row["storm_allow_bulk_sat"] if allow_bulk_sat is None else allow_bulk_sat
    changed = row["storm_mode"] != on
    if changed:
        conn.execute(
            "UPDATE customers SET storm_mode = %s, storm_since = now(), storm_by = %s WHERE id = %s",
            (on, actor, customer_id),
        )
        conn.execute(
            "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, NULL, %s, %s)",
            (dt.datetime.now(dt.UTC), customer_id, "storm_on" if on else "storm_off", Jsonb({"by": actor})),
        )
        audit.record(conn, actor, "storm_mode.on" if on else "storm_mode.off", "", customer_id)
    if bulk != row["storm_allow_bulk_sat"]:
        conn.execute("UPDATE customers SET storm_allow_bulk_sat = %s WHERE id = %s", (bulk, customer_id))
        audit.record(conn, actor, "storm_mode.allow_bulk_satellite", str(bulk), customer_id)
    # Desired state carries the probe rates; it also refreshes the steering maps.
    desired.refresh(conn, customer_id)
    return {"changed": changed}
