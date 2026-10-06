"""Developer platform API: webhooks, the event log and actions (ADR 0016)."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, actions, connectors, events, webhooks
from .common import errors, page

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: platform"])

ENDPOINT_COLUMNS = "id, url, description, events, active, created_by, created_at"


class EndpointIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    description: str = Field(default="", max_length=200)
    events: list[str] = Field(default_factory=lambda: ["*"], max_length=50)


def _check_events(names: list[str]) -> list[str]:
    families = {t.split(".")[0] + ".*" for t in events.TYPES}
    bad = [n for n in names if n != "*" and n not in events.TYPES and n not in families]
    if bad:
        raise HTTPException(422, f"Unknown event types: {', '.join(bad)}.")
    return names or ["*"]


@router.get("/webhooks")
def list_webhooks(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        return conn.execute(
            f"""SELECT {ENDPOINT_COLUMNS},
                   (SELECT count(*) FROM webhook_deliveries d
                    WHERE d.endpoint_id = e.id AND d.status = 'failed') AS failed,
                   (SELECT max(delivered_at) FROM webhook_deliveries d WHERE d.endpoint_id = e.id) AS last_delivered_at
                FROM webhook_endpoints e WHERE customer_id = %s ORDER BY created_at""",
            (customer_id,),
        ).fetchall()


@router.post("/webhooks", status_code=201)
def create_webhook(customer_id: str, body: EndpointIn, user: UserDep) -> dict:
    """Add an endpoint. The signing secret is shown once, in this response."""
    access.check(user, customer_id, "commai:admin")
    try:
        webhooks.check_url(body.url)
    except webhooks.UnsafeURL as e:
        raise HTTPException(422, str(e)) from e
    secret = "whsec_" + secrets.token_urlsafe(32)
    with db.tx() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM webhook_endpoints WHERE customer_id = %s", (customer_id,)
        ).fetchone()
        if n["n"] >= 20:
            raise HTTPException(400, "A business can have up to 20 webhook endpoints.")
        row = conn.execute(
            f"""INSERT INTO webhook_endpoints (customer_id, url, description, events, secret, created_by)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING {ENDPOINT_COLUMNS}""",
            (customer_id, body.url, body.description, _check_events(body.events), secret, user.actor),
        ).fetchone()
        audit.record(conn, user.actor, "commai.webhook.create", body.url, customer_id, {"events": body.events})
    return {**row, "secret": secret}


@router.delete("/webhooks/{endpoint_id}", status_code=204)
def delete_webhook(customer_id: str, endpoint_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        row = conn.execute(
            "DELETE FROM webhook_endpoints WHERE id = %s AND customer_id = %s RETURNING url", (endpoint_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Webhook not found.")
        audit.record(conn, user.actor, "commai.webhook.delete", row["url"], customer_id)


@router.post("/webhooks/{endpoint_id}/test", status_code=202)
def test_webhook(customer_id: str, endpoint_id: str, user: UserDep) -> dict:
    """Send a signed `webhook.test` event to this endpoint."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        ep = conn.execute(
            "SELECT id FROM webhook_endpoints WHERE id = %s AND customer_id = %s", (endpoint_id, customer_id)
        ).fetchone()
        if ep is None:
            raise HTTPException(404, "Webhook not found.")
        event_id = events.emit(conn, customer_id, "webhook.test", {"endpoint_id": endpoint_id, "by": user.email})
    return {"event_id": event_id}


@router.get("/webhooks/{endpoint_id}/deliveries")
def list_deliveries(customer_id: str, endpoint_id: str, user: UserDep, limit: int = Query(50, ge=1, le=200)) -> list:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        return conn.execute(
            """SELECT id, event_id, event_type, status, attempts, response_code, last_error, created_at, delivered_at
               FROM webhook_deliveries WHERE endpoint_id = %s AND customer_id = %s ORDER BY id DESC LIMIT %s""",
            (endpoint_id, customer_id, limit),
        ).fetchall()


@router.get("/events")
def list_events(
    customer_id: str, user: UserDep, type: str | None = None, after: int = 0, limit: int = Query(100, ge=1, le=500)
) -> dict:
    """The recorded event log, oldest first from `after` (a seq number)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT id, seq, type, subject, data, at FROM commai_events WHERE customer_id = %s AND seq > %s
               AND (%s::text IS NULL OR type = %s) ORDER BY seq LIMIT %s""",
            (customer_id, after, type, type, limit + 1),
        ).fetchall()
    return page(rows, limit, "seq")


@router.get("/event-types")
def event_types(customer_id: str, user: UserDep) -> list[str]:
    access.check(user, customer_id, "commai:read")
    return sorted(events.TYPES | {"webhook.test"})


# ---- actions -----------------------------------------------------------------------


@router.get("/actions")
def list_actions(
    customer_id: str,
    user: UserDep,
    status: str | None = None,
    conversation_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return conn.execute(
            """SELECT * FROM action_runs WHERE customer_id = %s AND (%s::text IS NULL OR status = %s)
               AND (%s::uuid IS NULL OR conversation_id = %s::uuid) ORDER BY created_at DESC LIMIT %s""",
            (customer_id, status, status, conversation_id, conversation_id, limit),
        ).fetchall()


class ProposeIn(BaseModel):
    app: str = Field(max_length=60)
    action: str = Field(max_length=60)
    inputs: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None
    test: bool = False


@router.post("/actions", status_code=201)
def propose_action(customer_id: str, body: ProposeIn, user: UserDep) -> dict:
    """A person runs a connector action. It goes through the same checks as
    the AI's; sensitive ones still need a second person to approve."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        run = actions.propose(
            conn,
            customer_id,
            role="person",
            app=body.app,
            action=body.action,
            inputs=body.inputs,
            actor=user.actor,
            conversation_id=body.conversation_id,
            test=body.test,
            idempotency_key=f"person:{user.id}:{secrets.token_hex(8)}",
        )
        audit.record(
            conn,
            user.actor,
            "commai.action.propose",
            str(run["id"]),
            customer_id,
            {"app": body.app, "action": body.action},
        )
    return run


@router.post("/actions/{run_id}/approve")
def approve_action(customer_id: str, run_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        run = actions.approve(conn, customer_id, run_id, approver=user.actor)
        audit.record(conn, user.actor, "commai.action.approve", run_id, customer_id)
    return run


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.post("/actions/{run_id}/reject")
def reject_action(customer_id: str, run_id: str, body: RejectIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        run = actions.reject(conn, customer_id, run_id, actor=user.actor, reason=body.reason)
        audit.record(conn, user.actor, "commai.action.reject", run_id, customer_id)
    return run


@router.get("/connectors")
def catalogue(customer_id: str, user: UserDep) -> list[dict]:
    """Every app CommAI can connect to and the exact actions each supports."""
    access.check(user, customer_id, "commai:read")
    return connectors.catalogue()
