"""The approvals queue (ADR 0033): every sensitive action and workflow step
waiting for a person, with what it will do. Approving and rejecting use the
existing endpoints (/actions/{id}/approve, /workflow-runs/{id}/approve)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ... import audit, db
from ...api.deps import UserDep
from .. import access, impact

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: approvals"])


@router.get("/approvals")
def approvals(customer_id: str, user: UserDep, limit: int = Query(100, ge=1, le=200)) -> dict:
    """Pending actions (with their impact preview) and workflow approval steps, oldest first."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        acts = conn.execute(
            """SELECT a.id, a.app, a.action, a.role, a.inputs, a.preview, a.proposed_by, a.conversation_id,
                      a.test, a.created_at, c.subject AS conversation_subject, c.channel
               FROM action_runs a LEFT JOIN conversations c ON c.id = a.conversation_id
               WHERE a.customer_id = %s AND a.status = 'awaiting_approval' ORDER BY a.created_at LIMIT %s""",
            (customer_id, limit),
        ).fetchall()
        flows = conn.execute(
            """SELECT r.id, r.workflow_id, w.name AS workflow, r.conversation_id, r.wait->>'prompt' AS prompt,
                      r.wait->>'since' AS since, r.wait->>'until' AS expires_at
               FROM commai_workflow_runs r JOIN commai_workflows w ON w.id = r.workflow_id
               WHERE r.customer_id = %s AND r.status = 'awaiting_approval' ORDER BY r.updated_at LIMIT %s""",
            (customer_id, limit),
        ).fetchall()
    me = user.actor
    for a in acts:
        a["own_proposal"] = a["proposed_by"] == me  # the proposer can't approve their own
    return {"actions": acts, "workflow_steps": flows, "count": len(acts) + len(flows)}


@router.get("/actions/{run_id}")
def get_action(customer_id: str, run_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM action_runs WHERE id::text = %s AND customer_id = %s", (run_id, customer_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Action not found.")
    return row


@router.post("/actions/{run_id}/preview")
def refresh_preview(customer_id: str, run_id: str, user: UserDep) -> dict:
    """Work the impact out again before approving (a calendar may have changed)."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        run = conn.execute(
            "SELECT * FROM action_runs WHERE id::text = %s AND customer_id = %s FOR UPDATE", (run_id, customer_id)
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Action not found.")
        if run["status"] != "awaiting_approval":
            raise HTTPException(409, "This action is not waiting for approval.")
        out = impact.refresh(conn, customer_id, run)
        audit.record(conn, user.actor, "commai.action.preview", run_id, customer_id)
    return out
