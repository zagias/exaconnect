"""How AI and workflows act safely (ADR 0016): propose, check, approve,
execute once, confirm.

1. Propose: a role (or a person) names an action from a connector's list.
2. Check: the service checks the role may use it, the business switched it
   on for that connection, and the inputs are valid. A refused proposal is
   recorded with the reason.
3. Approve: sensitive actions wait for a person. The AI cannot approve its
   own proposals: only a signed-in person can.
4. Execute: once, from a job, with an idempotency key that is the same on
   every retry, so a retry never books twice.
5. Confirm: only after the external system reports success does the
   customer hear that it worked. On failure the conversation goes to a
   person with a holding reply.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import channels, connectors, events, impact, inbox, jobs

AI_ROLES = ("customer_agent", "copilot", "platform_assistant", "workflow")
HOLDING_REPLY = (
    "Thanks for your patience. I couldn't finish that just now, so a member of our team will follow up here."
)


class ActionRefused(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def default_key(conversation_id: Any, app: str, action: str, inputs: dict) -> str:
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return f"{conversation_id or '-'}:{app}.{action}:{digest}"


def role_may(conn: psycopg.Connection, customer_id: Any, role: str, app: str, action: str) -> bool:
    if role == "person":
        return True
    return bool(
        conn.execute(
            "SELECT 1 FROM commai_role_tools WHERE customer_id = %s AND role = %s AND tool IN (%s, %s)",
            (customer_id, role, f"{app}.{action}", f"{app}.*"),
        ).fetchone()
    )


def _sensitive(conn, customer_id: Any, spec: connectors.ActionSpec, app: str) -> bool:
    if spec.sensitive or spec.kind in ("refund", "delete"):
        return True
    cfg = inbox.settings(conn, customer_id)["config"] or {}
    return f"{app}.{spec.name}" in (cfg.get("approval_required") or [])


def propose(
    conn: psycopg.Connection,
    customer_id: Any,
    *,
    role: str,
    app: str,
    action: str,
    inputs: dict,
    actor: str,
    conversation_id: Any = None,
    idempotency_key: str | None = None,
    on_success: dict | None = None,
    test: bool = False,
) -> dict:
    """Check and record a proposed action; queue it if no approval is needed.
    Raises ActionRefused (and records the refusal) when it may not run."""
    if role not in AI_ROLES and role != "person":
        raise ActionRefused(f"Unknown role {role}.")
    key = idempotency_key or default_key(conversation_id, app, action, inputs)
    existing = conn.execute(
        "SELECT * FROM action_runs WHERE customer_id = %s AND idempotency_key = %s", (customer_id, key)
    ).fetchone()
    if existing:
        return existing

    def refuse(msg: str, code: int = 400):
        conn.execute(
            """INSERT INTO action_runs (customer_id, conversation_id, role, app, action, inputs, idempotency_key,
                                        status, error, proposed_by, test)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'rejected', %s, %s, %s)""",
            (customer_id, conversation_id, role, app, action, Jsonb(inputs), key, msg, actor, test),
        )
        raise ActionRefused(msg, code)

    try:
        connector = connectors.get(app)
    except KeyError:
        refuse(f"There is no {app} integration.", 404)
    spec = connector.actions.get(action)
    if spec is None:
        refuse(f"{connector.label} has no action called {action}.", 404)
    if not role_may(conn, customer_id, role, app, action):
        refuse(f"The {role.replace('_', ' ')} is not allowed to {spec.label.lower()}.", 403)
    from .ai import governance  # daily action limits per role (ADR 0026)

    if over := governance.over_daily_limit(conn, customer_id, role):
        refuse(over, 429)
    c = connectors.connection(conn, customer_id, app)
    if c is None:
        refuse(f"{connector.label} is not connected.", 409)
    allowed_status = ("authorised", "testing", "live") if test else ("live",)
    if c["status"] not in allowed_status:
        refuse(f"{connector.label} is not switched on (it is {c['status']}).", 409)
    if action not in c["allowed_actions"]:
        refuse(f"{spec.label} is not switched on for {connector.label}.", 403)
    try:
        clean = connector.validate(action, inputs)
    except ValueError as e:
        refuse(str(e), 422)
    sensitive = _sensitive(conn, customer_id, spec, app)
    status = "awaiting_approval" if sensitive else "approved"
    # What the approver sees: the change and its impact, from read-only checks (ADR 0033).
    preview = impact.preview(conn, customer_id, connector, {**c, "test": test}, spec, clean) if sensitive else {}
    run = conn.execute(
        """INSERT INTO action_runs (customer_id, conversation_id, role, app, action, inputs, idempotency_key,
                                    sensitive, status, on_success, proposed_by, test, preview)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            conversation_id,
            role,
            app,
            action,
            Jsonb(clean),
            key,
            sensitive,
            status,
            Jsonb(on_success or {}),
            actor,
            test,
            Jsonb(preview),
        ),
    ).fetchone()
    events.emit(
        conn,
        customer_id,
        "action.proposed",
        {"run_id": str(run["id"]), "app": app, "action": action, "status": status, "role": role},
        run["id"],
    )
    if status == "approved":
        jobs.enqueue(
            conn,
            "action.execute",
            {"run_id": str(run["id"])},
            customer_id=customer_id,
            dedupe_key=f"action:{run['id']}",
        )
    return run


def approve(conn: psycopg.Connection, customer_id: Any, run_id: Any, *, approver: str) -> dict:
    """A person approves. `approver` must be a person (user:...), never an AI role."""
    if not approver.startswith("user:"):
        raise ActionRefused("Only a person can approve an action.", 403)
    run = conn.execute(
        "SELECT * FROM action_runs WHERE id = %s AND customer_id = %s FOR UPDATE", (run_id, customer_id)
    ).fetchone()
    if run is None:
        raise ActionRefused("Action not found.", 404)
    if run["status"] != "awaiting_approval":
        raise ActionRefused(f"This action is {run['status'].replace('_', ' ')}, not waiting for approval.", 409)
    if run["proposed_by"] == approver:
        raise ActionRefused("Someone other than the person who proposed it must approve.", 403)
    run = conn.execute(
        "UPDATE action_runs SET status = 'approved', approved_by = %s WHERE id = %s RETURNING *", (approver, run_id)
    ).fetchone()
    jobs.enqueue(
        conn, "action.execute", {"run_id": str(run_id)}, customer_id=customer_id, dedupe_key=f"action:{run_id}"
    )
    return run


def reject(conn: psycopg.Connection, customer_id: Any, run_id: Any, *, actor: str, reason: str = "") -> dict:
    run = conn.execute(
        """UPDATE action_runs SET status = 'rejected', error = %s, finished_at = now(), approved_by = %s
           WHERE id = %s AND customer_id = %s AND status = 'awaiting_approval' RETURNING *""",
        (reason or "Rejected by a person.", actor, run_id, customer_id),
    ).fetchone()
    if run is None:
        raise ActionRefused("No action waiting for approval with that id.", 404)
    return run


def _render(template: str, result: dict) -> str:
    try:
        return template.format_map({k: v for k, v in result.items() if isinstance(v, str | int | float)})
    except (KeyError, ValueError):
        return template


def _confirm(conn, run: dict, result: dict) -> None:
    text = (run["on_success"] or {}).get("reply")
    if not text or not run["conversation_id"]:
        return
    conv = conn.execute("SELECT * FROM conversations WHERE id = %s FOR UPDATE", (run["conversation_id"],)).fetchone()
    body = _render(text, result)
    try:
        channels.get(conv["channel"]).check_send(conn, conv, body, "")
    except channels.SendBlocked as e:
        inbox.add_note(
            conn, conv["customer_id"], conv["id"], author="CommAI", body=f"Confirmation not sent ({e}): {body}"
        )
        return
    inbox._insert_out(conn, conv, body, "system", "CommAI")


def _fail(conn, run: dict, error: str, cause: str) -> None:
    conn.execute(
        "UPDATE action_runs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
        (f"{error} ({cause})" if cause else error, run["id"]),
    )
    conn.execute(
        """UPDATE integration_connections SET last_failure_at = now(), last_error = %s,
                  status = CASE WHEN %s IN ('expired_signin', 'permission') THEN 'broken' ELSE status END
           WHERE customer_id = %s AND app = %s""",
        (error[:500], cause, run["customer_id"], run["app"]),
    )
    events.emit(
        conn,
        run["customer_id"],
        "action.failed",
        {"run_id": str(run["id"]), "app": run["app"], "action": run["action"], "cause": cause, "error": error},
        run["id"],
    )
    if run["conversation_id"] and run["role"] in AI_ROLES:
        inbox.hand_over(
            conn,
            run["customer_id"],
            run["conversation_id"],
            reason=f"{run['app']}.{run['action']} failed: {error}",
            packet={
                "action": run["action"],
                "app": run["app"],
                "inputs": run["inputs"],
                "error": error,
                "cause": cause,
            },
            holding_reply=HOLDING_REPLY,
        )


@jobs.handler("action.execute")
def _execute(conn: psycopg.Connection, job: dict):
    run = conn.execute("SELECT * FROM action_runs WHERE id = %s FOR UPDATE", (job["payload"]["run_id"],)).fetchone()
    if run is None or run["status"] not in ("approved", "executing"):
        return None
    conn.execute("UPDATE action_runs SET status = 'executing' WHERE id = %s", (run["id"],))
    connector = connectors.get(run["app"])
    c = connectors.connection(conn, run["customer_id"], run["app"])
    if c is None:
        _fail(conn, run, "The integration was removed.", "permission")
        return None
    c = {**c, "test": run["test"]}
    try:
        with conn.transaction():  # a savepoint: a failed attempt leaves nothing half-written
            result = connector.execute(conn, c, run["action"], run["inputs"], run["idempotency_key"])
    except connectors.ConnectorError as e:
        if e.cause == "provider" and job["attempts"] < job["max_attempts"]:
            conn.execute("UPDATE action_runs SET status = 'approved', error = %s WHERE id = %s", (str(e), run["id"]))
            return jobs.Later(str(e), delay_s=10)
        _fail(conn, run, str(e), e.cause)
        return None
    except Exception as e:  # noqa: BLE001 - unknown failure: retry with the same key
        if job["attempts"] < job["max_attempts"]:
            conn.execute(
                "UPDATE action_runs SET status = 'approved', error = %s WHERE id = %s",
                (f"{type(e).__name__}: {e}"[:500], run["id"]),
            )
            return jobs.Later(str(e), delay_s=10)
        _fail(conn, run, f"{type(e).__name__}: {e}", "provider")
        return None
    conn.execute(
        "UPDATE action_runs SET status = 'succeeded', result = %s, error = '', finished_at = now() WHERE id = %s",
        (Jsonb(result), run["id"]),
    )
    conn.execute(
        """UPDATE integration_connections SET last_success_at = now(),
                  status = CASE WHEN status = 'broken' THEN 'live' ELSE status END
           WHERE customer_id = %s AND app = %s""",
        (run["customer_id"], run["app"]),
    )
    events.emit(
        conn,
        run["customer_id"],
        "action.succeeded",
        {"run_id": str(run["id"]), "app": run["app"], "action": run["action"], "test": run["test"]},
        run["id"],
    )
    spec = connector.actions.get(run["action"])
    if spec and spec.kind == "create" and "start" in run["inputs"] and not run["test"]:
        events.emit(
            conn,
            run["customer_id"],
            "booking.confirmed",
            {
                "run_id": str(run["id"]),
                "app": run["app"],
                "conversation_id": str(run["conversation_id"] or ""),
                "start": run["inputs"]["start"],
            },
            run["conversation_id"] or run["id"],
        )
    _confirm(conn, run, result)
    return None
