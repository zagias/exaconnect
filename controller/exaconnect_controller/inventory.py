"""Inventory writes shared by the API and the lab seed. Each one is audited
and refreshes the affected desired states."""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from . import audit, desired
from .security import new_token, token_hash


def ensure_customer(conn: psycopg.Connection, name: str, actor: str) -> Any:
    row = conn.execute("SELECT id FROM customers WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    cid = conn.execute("INSERT INTO customers (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
    audit.record(conn, actor, "customer.create", name, cid)
    return cid


def ensure_carrier(conn: psycopg.Connection, name: str, actor: str) -> Any:
    row = conn.execute("SELECT id FROM carriers WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    cid = conn.execute("INSERT INTO carriers (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
    audit.record(conn, actor, "carrier.create", name)
    return cid


def upsert_site(conn: psycopg.Connection, customer_id: Any, actor: str, **f: Any) -> Any:
    row = conn.execute(
        """INSERT INTO sites (customer_id, name, kind, location, timezone, asn, lan_prefixes, overlay_host)
           VALUES (%(customer_id)s, %(name)s, %(kind)s, %(location)s, %(timezone)s, %(asn)s,
                   %(lan_prefixes)s::cidr[], %(overlay_host)s)
           ON CONFLICT (customer_id, name) DO UPDATE SET
             kind = EXCLUDED.kind, location = EXCLUDED.location, timezone = EXCLUDED.timezone,
             asn = EXCLUDED.asn, lan_prefixes = EXCLUDED.lan_prefixes, overlay_host = EXCLUDED.overlay_host
           RETURNING id""",
        {"customer_id": customer_id, **f},
    ).fetchone()
    audit.record(conn, actor, "site.upsert", f["name"], customer_id, {k: str(v) for k, v in f.items()})
    desired.refresh(conn, customer_id)
    return row["id"]


def upsert_link(conn: psycopg.Connection, customer_id: Any, site_id: Any, actor: str, **f: Any) -> Any:
    row = conn.execute(
        """INSERT INTO links (customer_id, site_id, carrier_id, path, underlay_type, underlay_interface,
                              underlay_ip, commit_mbps, cost_per_mbps, burst_price)
           VALUES (%(customer_id)s, %(site_id)s, %(carrier_id)s, %(path)s, %(underlay_type)s,
                   %(underlay_interface)s, %(underlay_ip)s, %(commit_mbps)s, %(cost_per_mbps)s, %(burst_price)s)
           ON CONFLICT (site_id, path) DO UPDATE SET
             carrier_id = EXCLUDED.carrier_id, underlay_type = EXCLUDED.underlay_type,
             underlay_interface = EXCLUDED.underlay_interface, underlay_ip = EXCLUDED.underlay_ip,
             commit_mbps = EXCLUDED.commit_mbps, cost_per_mbps = EXCLUDED.cost_per_mbps,
             burst_price = EXCLUDED.burst_price
           RETURNING id""",
        {"customer_id": customer_id, "site_id": site_id, **f},
    ).fetchone()
    audit.record(conn, actor, "link.upsert", f["path"], customer_id, {k: str(v) for k, v in f.items()})
    desired.refresh(conn, customer_id)
    return row["id"]


def issue_token(conn: psycopg.Connection, site_id: Any, actor: str, ttl_hours: int = 24) -> tuple[str, dt.datetime]:
    site = conn.execute("SELECT id, customer_id, name FROM sites WHERE id = %s", (site_id,)).fetchone()
    if site is None:
        raise LookupError("site not found")
    token = new_token()
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=ttl_hours)
    conn.execute(
        "INSERT INTO enrolment_tokens (customer_id, site_id, token_hash, created_by, expires_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (site["customer_id"], site_id, token_hash(token), actor, expires),
    )
    audit.record(conn, actor, "enrolment_token.issue", site["name"], site["customer_id"], {"expires_at": str(expires)})
    return token, expires
