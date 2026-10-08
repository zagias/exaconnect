"""Storm Mode (CLAUDE.md §4.4, ADR 0004): one switch per site.

On: the satellite path joins the steering lists of classes allowed on it at
that site, its probes speed up so it is measured warm, and the engine
shortens its hold time and lengthens its forecast horizon there. Off: all of
that reverses, and any class on satellite is moved back by the engine's next
pass. Sites away from the storm carry on as normal.

The customer row keeps a summary (on while any site is on, with the latest
switch's time and actor) for the portal header and the API.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit, desired


def _switch_site(conn: psycopg.Connection, site: dict, on: bool, actor: str) -> bool:
    if bool(site["storm_mode"]) == on:
        return False
    conn.execute(
        "UPDATE sites SET storm_mode = %s, storm_since = now(), storm_by = %s WHERE id = %s", (on, actor, site["id"])
    )
    node = conn.execute("SELECT id FROM nodes WHERE site_id = %s", (site["id"],)).fetchone()
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, %s, %s)",
        (
            dt.datetime.now(dt.UTC),
            site["customer_id"],
            node["id"] if node else None,
            "storm_on" if on else "storm_off",
            Jsonb({"by": actor, "site": site["name"]}),
        ),
    )
    audit.record(conn, actor, "storm_mode.on" if on else "storm_mode.off", site["name"], site["customer_id"])
    return True


def _summarise(conn: psycopg.Connection, customer_id: Any, actor: str) -> None:
    any_on = conn.execute(
        "SELECT bool_or(storm_mode) AS on FROM sites WHERE customer_id = %s", (customer_id,)
    ).fetchone()["on"]
    conn.execute(
        """UPDATE customers SET storm_mode = %s, storm_since = now(), storm_by = %s
           WHERE id = %s""",
        (bool(any_on), actor, customer_id),
    )


def set_storm(
    conn: psycopg.Connection,
    customer_id: Any,
    on: bool,
    actor: str,
    allow_bulk_sat: bool | None = None,
    site_ids: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Switch Storm Mode for some of a customer's sites (all of them by default)."""
    row = conn.execute("SELECT storm_allow_bulk_sat FROM customers WHERE id = %s FOR UPDATE", (customer_id,)).fetchone()
    if row is None:
        raise LookupError("customer not found")
    wanted = None if site_ids is None else {str(s) for s in site_ids}
    sites = conn.execute(
        "SELECT id, customer_id, name, storm_mode FROM sites WHERE customer_id = %s AND kind = 'site' FOR UPDATE",
        (customer_id,),
    ).fetchall()
    if wanted is not None and not wanted <= {str(s["id"]) for s in sites}:
        raise LookupError("site not found")
    changed = [
        s["name"] for s in sites if (wanted is None or str(s["id"]) in wanted) and _switch_site(conn, s, on, actor)
    ]
    if changed:
        _summarise(conn, customer_id, actor)
    bulk = row["storm_allow_bulk_sat"] if allow_bulk_sat is None else allow_bulk_sat
    if bulk != row["storm_allow_bulk_sat"]:
        conn.execute("UPDATE customers SET storm_allow_bulk_sat = %s WHERE id = %s", (bulk, customer_id))
        audit.record(conn, actor, "storm_mode.allow_bulk_satellite", str(bulk), customer_id)
    # Desired state carries the probe rates; it also refreshes the steering maps.
    desired.refresh(conn, customer_id)
    return {"changed": changed}
