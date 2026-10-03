"""Virtual circuits and the cloud router (ADR 0009). Customers manage their
own; admins act for any customer. Carrier users have no access. The
pre-shared key is write-only: it is never returned, logged or audited."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, fabric
from .deps import UserDep, check_customer

router = APIRouter(tags=["fabric"])

SELECT = """SELECT c.*, a.name AS a_site, b.name AS b_site FROM circuits c
            LEFT JOIN sites a ON a.id = c.a_site_id LEFT JOIN sites b ON b.id = c.b_site_id
            WHERE c.customer_id = %s AND c.deleted_at IS NULL"""


class CircuitPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    bandwidth_mbps: int | None = Field(default=None, ge=1, le=1000)
    enabled: bool | None = None
    a_site_id: str | None = None
    a_prefixes: list[str] | None = Field(default=None, max_length=50)
    a_vlan: int | None = Field(default=None, ge=1, le=4094)
    b_site_id: str | None = None
    b_vlan: int | None = Field(default=None, ge=1, le=4094)
    provider: str | None = Field(default=None, max_length=20)
    region: str | None = Field(default=None, max_length=40)
    peer_address: str | None = Field(default=None, max_length=45)
    peer_asn: int | None = Field(default=None, ge=1, le=4294967294)
    inside_cidr: str | None = Field(default=None, max_length=20)
    psk: str | None = Field(default=None, max_length=64, repr=False)
    cloud_prefixes: list[str] | None = Field(default=None, max_length=50)
    class_name: str | None = Field(default=None, max_length=20)


class CircuitIn(CircuitPatch):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["cloud", "site"]
    bandwidth_mbps: int = Field(ge=1, le=1000)


@router.get("/circuits/providers")
def providers(user: UserDep) -> dict:
    """Cloud providers with their usual gateway ASN and where to find the tunnel details."""
    return fabric.PROVIDERS


def _mbps(rows: list[dict]) -> tuple[float | None, float | None]:
    """In and out Mbps from cumulative counters: first and last sample per node, busiest node."""
    by_node: dict[Any, list[dict]] = {}
    for r in rows:
        by_node.setdefault(r["node_id"], []).append(r)
    best: tuple[float | None, float | None] = (None, None)
    for samples in by_node.values():
        first, last = samples[0], samples[-1]
        secs = (last["time"] - first["time"]).total_seconds()
        if secs <= 0:
            continue
        d_in, d_out = last["bytes_in"] - first["bytes_in"], last["bytes_out"] - first["bytes_out"]
        if d_in < 0 or d_out < 0:  # counters reset (interface recreated)
            continue
        rate = (round(d_in * 8 / secs / 1e6, 3), round(d_out * 8 / secs / 1e6, 3))
        if best[0] is None or rate[0] + rate[1] > (best[0] or 0) + (best[1] or 0):
            best = rate
    return best


def views(conn, customer_id: Any, circuit_id: Any = None) -> list[dict]:
    rows = conn.execute(
        SELECT + (" AND c.id = %s" if circuit_id is not None else "") + " ORDER BY c.id",
        (customer_id, circuit_id) if circuit_id is not None else (customer_id,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    now = dt.datetime.now(dt.UTC)
    states: dict[Any, list[dict]] = {}
    for s in conn.execute("SELECT * FROM circuit_state WHERE circuit_id = ANY(%s)", (ids,)):
        states.setdefault(s["circuit_id"], []).append(s)
    metrics: dict[Any, list[dict]] = {}
    for m in conn.execute(
        """SELECT circuit_id, node_id, time, sent, received, rtt_avg_ms, bytes_in, bytes_out FROM circuit_metrics
           WHERE circuit_id = ANY(%s) AND time > now() - interval '2 minutes' ORDER BY time""",
        (ids,),
    ):
        metrics.setdefault(m["circuit_id"], []).append(m)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for c in rows:
        st = sorted(states.get(c["id"], []), key=lambda s: s["updated_at"], reverse=True)
        ms = metrics.get(c["id"], [])
        last_min = [m for m in ms if (now - m["time"]).total_seconds() <= 60]
        sent = sum(m["sent"] for m in last_min)
        received = sum(m["received"] for m in last_min)
        rtts = [m["rtt_avg_ms"] for m in last_min if m["rtt_avg_ms"] is not None]
        mbps_in, mbps_out = _mbps(ms)
        ours, cloud = fabric.inside_pair(c["inside_cidr"]) if c["inside_cidr"] else (None, None)
        view = {k: v for k, v in c.items() if k not in ("psk", "deleted_at", "customer_id")}
        view.update(
            {
                "a_prefixes": [str(p) for p in c["a_prefixes"]],
                "cloud_prefixes": [str(p) for p in c["cloud_prefixes"]],
                "inside_cidr": str(c["inside_cidr"]) if c["inside_cidr"] else None,
                "peer_address": str(c["peer_address"]) if c["peer_address"] else None,
                "price_per_mbps_month": float(c["price_per_mbps_month"]),
                "our_inside": ours,
                "cloud_inside": cloud,
                "has_psk": bool(c["psk"]),
                "status": fabric.status(c, st, received, now),
                "ike": st[0]["ike"] if st else "",
                "bgp": st[0]["bgp"] if st else "",
                "prefixes_received": st[0]["prefixes_received"] if st else 0,
                "routes": list(st[0]["routes"]) if st else [],
                "rtt_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
                "loss_pct": round(100 * (sent - received) / sent, 2) if sent else None,
                "mbps_in": mbps_in,
                "mbps_out": mbps_out,
                "month_to_date": fabric.charges(conn, c, month_start, now)["total"],
            }
        )
        out.append(view)
    return out


def _one(conn, customer_id: str, circuit_id: int) -> dict:
    row = conn.execute(SELECT + " AND c.id = %s FOR UPDATE OF c", (customer_id, circuit_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Circuit not found.")
    return row


@router.get("/customers/{customer_id}/circuits")
def list_circuits(customer_id: str, user: UserDep) -> list[dict]:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return views(conn, customer_id)


@router.post("/customers/{customer_id}/circuits", status_code=201)
def create_circuit(customer_id: str, body: CircuitIn, user: UserDep) -> dict:
    """A circuit to a cloud VPN gateway, or a layer 2 circuit between two sites.
    Agents set it up on their next poll (within 10 seconds)."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(404, "Customer not found.")
        try:
            cid = fabric.create(conn, customer_id, body.model_dump(), user.actor)
        except fabric.CircuitError as e:
            raise HTTPException(400, str(e)) from None
        return views(conn, customer_id, cid)[0]


@router.patch("/customers/{customer_id}/circuits/{circuit_id}")
def update_circuit(customer_id: str, circuit_id: int, body: CircuitPatch, user: UserDep) -> dict:
    """Change a circuit. A new bandwidth takes effect within 10 seconds and is billed from now."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        circuit = _one(conn, customer_id, circuit_id)
        try:
            fabric.update(conn, circuit, body.model_dump(exclude_unset=True), user.actor)
        except fabric.CircuitError as e:
            raise HTTPException(400, str(e)) from None
        return views(conn, customer_id, circuit_id)[0]


@router.delete("/customers/{customer_id}/circuits/{circuit_id}", status_code=204)
def delete_circuit(customer_id: str, circuit_id: int, user: UserDep) -> None:
    check_customer(user, customer_id)
    with db.tx() as conn:
        fabric.delete(conn, _one(conn, customer_id, circuit_id), user.actor)


@router.get("/customers/{customer_id}/circuits/{circuit_id}/charges")
def circuit_charges(customer_id: str, circuit_id: int, user: UserDep, month: str | None = None) -> dict:
    """Bandwidth charges for a month (default this one): each speed for the hours it was set."""
    check_customer(user, customer_id)
    now = dt.datetime.now(dt.UTC)
    try:
        start = dt.datetime.strptime(month, "%Y-%m").replace(tzinfo=dt.UTC) if month else now.replace(day=1)
    except ValueError:
        raise HTTPException(400, "month is YYYY-MM.") from None
    start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM circuits WHERE customer_id = %s AND id = %s", (customer_id, circuit_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Circuit not found.")
        return fabric.charges(conn, row, start, min(end, now))


@router.get("/customers/{customer_id}/circuits/{circuit_id}/metrics")
def circuit_metrics(customer_id: str, circuit_id: int, user: UserDep, minutes: int = 60) -> list[dict]:
    """Per minute: probes sent and received, round trip, and traffic in and out."""
    check_customer(user, customer_id)
    minutes = max(1, min(minutes, 24 * 60))
    with db.tx() as conn:
        _one(conn, customer_id, circuit_id)
        return conn.execute(
            """SELECT date_trunc('minute', time) AS time, sum(sent)::int AS sent, sum(received)::int AS received,
                      round(avg(rtt_avg_ms)::numeric, 1)::float AS rtt_ms,
                      round(((max(bytes_in) - min(bytes_in)) * 8
                             / greatest(extract(epoch FROM max(time) - min(time)), 1) / 1e6)::numeric, 3)::float
                        AS mbps_in,
                      round(((max(bytes_out) - min(bytes_out)) * 8
                             / greatest(extract(epoch FROM max(time) - min(time)), 1) / 1e6)::numeric, 3)::float
                        AS mbps_out
               FROM circuit_metrics
               WHERE circuit_id = %(c)s AND time > now() - make_interval(mins => %(m)s)
                 -- one end's counters: both ends of a site circuit report their own
                 AND node_id = (SELECT node_id FROM circuit_metrics WHERE circuit_id = %(c)s
                                ORDER BY node_id LIMIT 1)
               GROUP BY 1 ORDER BY 1""",
            {"c": circuit_id, "m": minutes},
        ).fetchall()
