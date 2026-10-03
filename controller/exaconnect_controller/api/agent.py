"""Agent-facing API. Enrolment uses a one-time token over server-authenticated
TLS; everything under /agent requires the client certificate issued at
enrolment, checked by the TLS proxy (deploy/nginx)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, IPvAnyAddress

from .. import audit, db, desired, pki
from ..routing import maps
from ..security import token_hash
from .deps import NodeDep

router = APIRouter(tags=["agent"])


@router.get("/ca.pem", response_class=PlainTextResponse)
def ca_pem(request: Request) -> str:
    return request.app.state.ca.pem.decode()


class EnrolIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    node_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    csr_pem: str = Field(max_length=8192)
    wg_public_key: str = Field(pattern=r"^[A-Za-z0-9+/]{43}=$")


class EnrolOut(BaseModel):
    node_id: str
    cert_pem: str
    ca_pem: str


@router.post("/enrol")
def enrol(body: EnrolIn, request: Request) -> EnrolOut:
    try:
        return _enrol(body, request)
    except _BadToken:
        # Recorded in its own transaction so it survives the error response.
        with db.tx() as conn:
            audit.record(conn, f"node:{body.node_name}", "enrol_failed", body.node_name, detail={"reason": "bad token"})
        raise HTTPException(401, "This enrolment token is unknown, used or expired.") from None


class _BadToken(Exception):
    pass


def _enrol(body: EnrolIn, request: Request) -> EnrolOut:
    ca: pki.CA = request.app.state.ca
    with db.tx() as conn:
        tok = conn.execute(
            """SELECT t.id, t.site_id, t.customer_id, s.name AS site_name
               FROM enrolment_tokens t JOIN sites s ON s.id = t.site_id
               WHERE t.token_hash = %s AND t.used_at IS NULL AND t.expires_at > now()
               FOR UPDATE OF t""",
            (token_hash(body.token),),
        ).fetchone()
        if tok is None:
            raise _BadToken()
        if tok["site_name"] != body.node_name:
            raise HTTPException(400, f"This token is for {tok['site_name']}, not {body.node_name}.")
        try:
            cert_pem, serial = ca.sign_client(body.csr_pem.encode(), body.node_name)
        except pki.CSRError as e:
            raise HTTPException(400, str(e)) from None
        node = conn.execute(
            """INSERT INTO nodes (customer_id, site_id, name, wg_public_key, cert_serial)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (site_id) DO UPDATE SET
                 wg_public_key = EXCLUDED.wg_public_key, cert_serial = EXCLUDED.cert_serial,
                 enrolled_at = now(), applied_version = 0, apply_ok = NULL, apply_error = NULL
               RETURNING id""",
            (tok["customer_id"], tok["site_id"], body.node_name, body.wg_public_key, serial),
        ).fetchone()
        conn.execute("UPDATE enrolment_tokens SET used_at = now(), node_id = %s WHERE id = %s", (node["id"], tok["id"]))
        audit.record(conn, f"node:{body.node_name}", "enrol", body.node_name, tok["customer_id"], {"serial": serial})
        _event(conn, tok["customer_id"], node["id"], "enrolled", {"node": body.node_name})
        desired.refresh(conn, tok["customer_id"])
    return EnrolOut(node_id=str(node["id"]), cert_pem=cert_pem.decode(), ca_pem=ca.pem.decode())


@router.get("/agent/desired-state")
def desired_state(node: NodeDep, have: int = 0) -> Any:
    with db.tx() as conn:
        state = desired.latest(conn, node.id)
    if state is None:
        raise HTTPException(404, "No desired state yet.")
    if state["version"] == have:
        return Response(status_code=204)
    return state


@router.get("/agent/steering")
def steering(node: NodeDep, have: int = 0) -> Any:
    """The steering map: classes, paths and the ordered path list per class."""
    with db.tx() as conn:
        m = maps.latest(conn, node.id)
    if m is None:
        raise HTTPException(404, "No steering map yet.")
    if m["version"] == have:
        return Response(status_code=204)
    return m


class StatusIn(BaseModel):
    applied_version: int
    ok: bool
    error: str | None = Field(default=None, max_length=4000)
    agent_version: str = Field(default="", max_length=200)


@router.post("/agent/status", status_code=204)
def status(body: StatusIn, node: NodeDep) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE nodes SET applied_version = %s, apply_ok = %s, apply_error = %s, agent_version = %s WHERE id = %s",
            (body.applied_version, body.ok, body.error, body.agent_version, node.id),
        )
        audit.record(
            conn,
            f"node:{node.name}",
            "desired_state.applied" if body.ok else "desired_state.failed",
            node.name,
            node.customer_id,
            body.model_dump(),
        )


class ProbeWindow(BaseModel):
    path: str = Field(max_length=16)
    start: dt.datetime
    end: dt.datetime
    sent: int = Field(ge=0)
    received: int = Field(ge=0)
    loss_pct: float
    rtt_avg_ms: float
    rtt_min_ms: float
    rtt_max_ms: float
    jitter_ms: float


class Counter(BaseModel):
    at: dt.datetime
    ifname: str = Field(max_length=16)
    rx_bytes: int = Field(ge=0)
    tx_bytes: int = Field(ge=0)


class EventIn(BaseModel):
    at: dt.datetime
    kind: str = Field(max_length=64)
    detail: dict[str, str] | None = None


class TunnelIn(BaseModel):
    name: str = Field(max_length=16)
    path: str = Field(max_length=16)
    handshake_age_s: int
    bfd: str | None = None


class ChoiceIn(BaseModel):
    class_: str = Field(alias="class", max_length=32)
    dst: str = Field(default="", max_length=64)
    path: str = Field(default="", max_length=16)
    paused: bool = False
    failover: bool = False


class FlowIn(BaseModel):
    """Traffic leaving the site in the last minute, per destination and port
    (from connection tracking), for application detection (ADR 0007)."""

    at: dt.datetime
    proto: Literal["tcp", "udp"]
    dst: IPvAnyAddress
    dport: int = Field(ge=0, le=65535)
    class_: str = Field(default="", alias="class", max_length=32)
    flows: int = Field(ge=0)
    bytes_out: int = Field(ge=0)
    bytes_in: int = Field(ge=0)
    pkts_out: int = Field(ge=0)
    pkts_in: int = Field(ge=0)


class CircuitIn(BaseModel):
    """One virtual circuit's state on this node (ADR 0009)."""

    id: int
    name: str = Field(default="", max_length=16)
    ike: str = Field(default="", max_length=16)
    bgp: str = Field(default="", max_length=32)
    prefixes_received: int = Field(default=0, ge=0)
    routes: list[str] = Field(default=[], max_length=20)
    sent: int = Field(default=0, ge=0)
    received: int = Field(default=0, ge=0)
    rtt_ms: float | None = None
    bytes_in: int = Field(default=0, ge=0)
    bytes_out: int = Field(default=0, ge=0)


class TelemetryIn(BaseModel):
    at: dt.datetime
    probes: list[ProbeWindow] | None = Field(default=None, max_length=5000)
    counters: list[Counter] | None = Field(default=None, max_length=5000)
    events: list[EventIn] | None = Field(default=None, max_length=1000)
    tunnels: list[TunnelIn] | None = Field(default=None, max_length=16)
    steering: list[ChoiceIn] | None = Field(default=None, max_length=1000)
    steering_version: int = 0
    flows: list[FlowIn] | None = Field(default=None, max_length=2000)
    circuits: list[CircuitIn] | None = Field(default=None, max_length=200)


@router.post("/agent/telemetry", status_code=204)
def telemetry(body: TelemetryIn, node: NodeDep) -> None:
    with db.tx() as conn, conn.cursor() as cur:
        # Agents report per tunnel interface; store the path name.
        path_of = {r["tunnel"]: r["name"] for r in conn.execute("SELECT tunnel, name FROM paths").fetchall()}
        if body.probes:
            cur.executemany(
                """INSERT INTO path_metrics (time, customer_id, node_id, path, sent, received, loss_pct,
                                             rtt_avg_ms, rtt_min_ms, rtt_max_ms, jitter_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (
                        p.end,
                        node.customer_id,
                        node.id,
                        path_of.get(p.path, p.path),
                        p.sent,
                        p.received,
                        p.loss_pct,
                        p.rtt_avg_ms if p.received else None,
                        p.rtt_min_ms if p.received else None,
                        p.rtt_max_ms if p.received else None,
                        p.jitter_ms,
                    )
                    for p in body.probes
                ],
            )
        if body.counters:
            cur.executemany(
                "INSERT INTO iface_counters (time, customer_id, node_id, ifname, rx_bytes, tx_bytes)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [(c.at, node.customer_id, node.id, c.ifname, c.rx_bytes, c.tx_bytes) for c in body.counters],
            )
        for e in body.events or []:
            _event(conn, node.customer_id, node.id, e.kind, e.detail or {}, e.at)
        for t in body.tunnels or []:
            conn.execute(
                """INSERT INTO tunnel_state (node_id, tunnel, path, handshake_age_s, bfd, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (node_id, tunnel) DO UPDATE SET path = EXCLUDED.path,
                     handshake_age_s = EXCLUDED.handshake_age_s, bfd = EXCLUDED.bfd,
                     updated_at = EXCLUDED.updated_at""",
                (node.id, t.name, t.path, t.handshake_age_s, t.bfd, body.at),
            )
        if body.flows:
            cur.executemany(
                """INSERT INTO flow_stats (time, customer_id, node_id, proto, dst, dport, class_name, flows,
                                           bytes_out, bytes_in, pkts_out, pkts_in)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (
                        f.at,
                        node.customer_id,
                        node.id,
                        f.proto,
                        str(f.dst),
                        f.dport,
                        f.class_,
                        f.flows,
                        f.bytes_out,
                        f.bytes_in,
                        f.pkts_out,
                        f.pkts_in,
                    )
                    for f in body.flows
                ],
            )
        if body.circuits:
            _circuits(conn, node, body)
        if body.steering is not None:
            conn.execute("DELETE FROM steering_actual WHERE node_id = %s", (node.id,))
            cur.executemany(
                """INSERT INTO steering_actual (node_id, class_name, dst, path, paused, failover, version, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                [
                    (node.id, c.class_, c.dst, c.path or None, c.paused, c.failover, body.steering_version, body.at)
                    for c in body.steering
                ],
            )


def _event(conn, customer_id, node_id, kind: str, detail: dict, at: dt.datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, %s, %s)",
        (at or dt.datetime.now(dt.UTC), customer_id, node_id, kind, Jsonb(detail)),
    )


def _circuits(conn, node, body: TelemetryIn) -> None:
    """Circuit state and metrics, for this customer's own circuits only."""
    own = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM circuits WHERE customer_id = %s AND id = ANY(%s)",
            (node.customer_id, [c.id for c in body.circuits or []]),
        )
    }
    for c in body.circuits or []:
        if c.id not in own:
            continue
        conn.execute(
            """INSERT INTO circuit_state (circuit_id, node_id, ike, bgp, prefixes_received, routes, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (circuit_id, node_id) DO UPDATE SET ike = EXCLUDED.ike, bgp = EXCLUDED.bgp,
                 prefixes_received = EXCLUDED.prefixes_received, routes = EXCLUDED.routes,
                 updated_at = EXCLUDED.updated_at""",
            (c.id, node.id, c.ike, c.bgp, c.prefixes_received, [r[:64] for r in c.routes], body.at),
        )
        conn.execute(
            """INSERT INTO circuit_metrics (time, customer_id, circuit_id, node_id, sent, received, rtt_avg_ms,
                                            bytes_in, bytes_out)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (body.at, node.customer_id, c.id, node.id, c.sent, c.received, c.rtt_ms, c.bytes_in, c.bytes_out),
        )
