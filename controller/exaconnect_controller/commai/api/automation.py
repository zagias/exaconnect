"""Automation API (ADR 0020): integration setup and health, workflows, AI
onboarding, the platform assistant, support cases, outcome reports and
usage limits.

Reads need commai:read. Changes need commai:admin and a seat that may change
settings (not the internal, notes-only seat). Every write is audited.
Tokens and secrets are accepted only through secure entry and are never
returned.
"""

from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, diagnostics
from ..automation import assistant, compose, integrations, onboarding, reports, vault, workflows
from . import paging
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: automation"])
public = APIRouter(tags=["commai: automation"])


@contextmanager
def _errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except integrations.SetupError as e:
            raise HTTPException(e.code, str(e)) from e
        except workflows.WorkflowError as e:
            raise HTTPException(e.code, str(e)) from e
        except onboarding.OnboardingError as e:
            raise HTTPException(e.code, str(e)) from e
        except assistant.FixError as e:
            raise HTTPException(e.code, str(e)) from e
        except vault.VaultError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e


def _admin(conn, user, customer_id: str) -> None:
    access.require_business_admin(user)
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change automation settings.")


def _settings(request: Request) -> Any:
    return request.app.state.settings


# ==== integrations ===================================================================


@router.get("/integrations")
def list_integrations(customer_id: str, user: UserDep) -> list[dict]:
    """The catalogue: every app, the exact actions it supports, and this business's connection."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return integrations.catalogue(conn, customer_id)


@router.post("/integrations/{app}/connect")
def connect_integration(customer_id: str, app: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = integrations.connect(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.connect", app, customer_id)
    return out


@router.post("/integrations/{app}/sign-in")
def sign_in(customer_id: str, app: str, user: UserDep) -> dict:
    """Start OAuth sign-in: returns the provider's sign-in URL to open."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        url = integrations.start_sign_in(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.sign_in", app, customer_id)
    return {"url": url}


@public.get("/oauth/{app}/callback", include_in_schema=False)
def oauth_callback(app: str, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    """Where Google or HubSpot sends the browser back after sign-in."""
    portal = os.environ.get("EXA_PORTAL_URL", "").rstrip("/")
    dest = f"{portal}/commai/integrations/{urllib.parse.quote(app)}"
    if error or not code:
        return RedirectResponse(f"{dest}?signin=failed&reason={urllib.parse.quote(error or 'cancelled')[:60]}", 303)
    try:
        with db.tx() as conn:
            row = integrations.finish_sign_in(conn, app, code, state)
            audit.record(conn, "oauth:" + app, "commai.integration.signed_in", app, row["customer_id"])
    except integrations.SetupError as e:
        return RedirectResponse(f"{dest}?signin=failed&reason={urllib.parse.quote(str(e))[:200]}", 303)
    return RedirectResponse(f"{dest}?signin=ok", 303)


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=400)


@router.post("/integrations/{app}/token")
def enter_token(customer_id: str, app: str, body: TokenIn, user: UserDep) -> dict:
    """Secure entry of a private-app token. Stored encrypted and never shown again."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.enter_token(conn, customer_id, app, body.token, user.actor)
        audit.record(conn, user.actor, "commai.integration.token", app, customer_id)  # never the token
    return row


class AllowedIn(BaseModel):
    actions: list[str] = Field(max_length=50)


@router.put("/integrations/{app}/actions")
def set_actions(customer_id: str, app: str, body: AllowedIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.set_allowed(conn, customer_id, app, body.actions, user.actor)
        audit.record(conn, user.actor, "commai.integration.actions", app, customer_id, {"actions": body.actions})
    return {**row, "permissions": integrations.permissions(app, row["allowed_actions"])}


class SettingsIn(BaseModel):
    settings: dict[str, Any]


SAFE_SETTINGS = {"calendar_id", "test_calendar_id", "open_hour", "close_hour", "slot_minutes", "test_writes"}


@router.put("/integrations/{app}/settings")
def set_integration_settings(customer_id: str, app: str, body: SettingsIn, user: UserDep) -> dict:
    """Non-secret settings (calendar id, hours). Example apps also take
    simulate_failure to rehearse a broken integration."""
    access.check(user, customer_id, "commai:admin")
    from psycopg.types.json import Jsonb

    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        # The app's own settings and, on a stand-in, simulate_failure (ADR 0028).
        allowed = SAFE_SETTINGS | integrations.extra_settings(conn, customer_id, app)
        bad = sorted(set(body.settings) - allowed)
        if bad:
            raise HTTPException(422, f"Unknown settings: {', '.join(bad)}.")
        integrations.check_settings(app, body.settings)
        row = conn.execute(
            """UPDATE integration_connections SET settings = settings || %s, updated_at = now()
               WHERE customer_id = %s AND app = %s RETURNING *""",
            (Jsonb(body.settings), customer_id, app),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Not connected.")
        audit.record(conn, user.actor, "commai.integration.settings", app, customer_id, {"keys": sorted(body.settings)})
    return integrations.public(row)


@router.get("/integrations/{app}/mapping")
def get_mapping(customer_id: str, app: str, request: Request, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        sug = integrations.suggest_mapping(conn, customer_id, app, _settings(request))
        row = integrations._row(conn, customer_id, app)
    return {**sug, "saved": (row or {}).get("mapping") or {}, "still_open": (row or {}).get("mapping_open") or []}


class MappingIn(BaseModel):
    mapping: dict[str, dict[str, str]]


@router.put("/integrations/{app}/mapping")
def save_mapping(customer_id: str, app: str, body: MappingIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.save_mapping(conn, customer_id, app, body.mapping, user.actor)
        audit.record(conn, user.actor, "commai.integration.mapping", app, customer_id)
    return row


@router.post("/integrations/{app}/mapping/accept")
def accept_mapping(customer_id: str, app: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.accept_suggestions(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.mapping", app, customer_id, {"accepted": True})
    return row


@router.post("/integrations/{app}/test")
def test_integration(customer_id: str, app: str, user: UserDep) -> dict:
    """Sample lookups with every allowed read action. Nothing is written."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = integrations.run_test(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.test", app, customer_id, {"ok": out["test"]["ok"]})
    return out


class TestActionIn(BaseModel):
    action: str = Field(max_length=60)
    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/integrations/{app}/test-action", status_code=201)
def test_action(customer_id: str, app: str, body: TestActionIn, user: UserDep) -> dict:
    """A controlled test action through the action service (marked as a test)."""
    import secrets

    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        run = integrations.test_action(
            conn, customer_id, app, body.action, body.inputs, user.actor, f"test:{user.id}:{secrets.token_hex(8)}"
        )
        audit.record(conn, user.actor, "commai.integration.test_action", app, customer_id, {"action": body.action})
    return run


@router.post("/integrations/{app}/approve")
def approve_integration(customer_id: str, app: str, user: UserDep) -> dict:
    """A person approves and switches the integration on."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.approve(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.live", app, customer_id)
    return row


@router.post("/integrations/{app}/pause")
def pause_integration(customer_id: str, app: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.pause(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.pause", app, customer_id)
    return row


@router.post("/integrations/{app}/resume")
def resume_integration(customer_id: str, app: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.resume(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.resume", app, customer_id)
    return row


@router.delete("/integrations/{app}", status_code=204)
def disconnect_integration(customer_id: str, app: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        integrations.disconnect(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.integration.disconnect", app, customer_id)


@router.get("/integrations/{app}/health")
def integration_health(customer_id: str, app: str, user: UserDep, check: bool = False) -> dict:
    """Status, sign-in, last success, failures, affected workflows, the cause,
    the evidence and the safe repair steps. check=true also asks the app now."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        return integrations.health(conn, customer_id, app, check=check)


class RepairIn(BaseModel):
    step: Literal["recheck", "retry_failed", "sign_in", "edit_mapping", "remove_action", "review"]


@router.post("/integrations/{app}/repair")
def repair_integration(customer_id: str, app: str, body: RepairIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = integrations.repair(conn, customer_id, app, body.step, user.actor)
        audit.record(
            conn, user.actor, "commai.integration.repair", app, customer_id, {"step": body.step, "ok": out["ok"]}
        )
    return out


# ==== workflows ======================================================================


class DefinitionIn(BaseModel):
    definition: dict[str, Any]


class TextIn(BaseModel):
    text: str = Field(min_length=3, max_length=2000)


@router.get("/workflows")
def list_workflows(
    customer_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(500, ge=1, le=500)
) -> Any:
    """Every workflow (a plain list), or a page with `cursor` (empty for the first page)."""
    access.check(user, customer_id, "commai:read")
    after, args = paging.where(cursor)
    with db.tx() as conn:
        rows = conn.execute(
            f"SELECT * FROM commai_workflows WHERE customer_id = %s{after} ORDER BY created_at DESC, id::text DESC"
            " LIMIT %s",
            (customer_id, *args, limit + 1),
        ).fetchall()
        return paging.result(
            [workflows.summary(conn, w) | {"created_at": w["created_at"]} for w in rows], cursor, limit
        )


@router.post("/workflows/draft")
def draft_workflow(customer_id: str, body: TextIn, request: Request, user: UserDep) -> dict:
    """Plain English to an editable draft (not saved)."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = compose.from_text(conn, customer_id, body.text, _settings(request))
        audit.record(conn, user.actor, "commai.workflow.draft", "", customer_id, {"source": out["source"]})
    return out


@router.post("/workflows/validate")
def validate_workflow(customer_id: str, body: DefinitionIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        v = workflows.validate(conn, customer_id, body.definition)
    return {**v, "preview": workflows.preview(v["definition"]) if v["definition"].get("trigger") else []}


@router.get("/workflows/packs")
def list_packs(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    return compose.packs()


class PackIn(BaseModel):
    names: list[str] | None = None


@router.post("/workflows/packs/{pack}", status_code=201)
def install_pack(customer_id: str, pack: str, body: PackIn, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = compose.install_pack(conn, customer_id, pack, actor=user.actor, names=body.names)
        audit.record(conn, user.actor, "commai.workflow.pack", pack, customer_id, {"count": len(out)})
    return out


@router.post("/workflows", status_code=201)
def create_workflow(customer_id: str, body: DefinitionIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = workflows.create(conn, customer_id, body.definition, actor=user.actor)
        audit.record(conn, user.actor, "commai.workflow.create", str(out["workflow"]["id"]), customer_id)
    return out


@router.get("/workflows/{workflow_id}")
def get_workflow(customer_id: str, workflow_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        wf = workflows.get(conn, customer_id, workflow_id)
        latest = workflows.version(conn, wf)
        versions = conn.execute(
            """SELECT version, source, note, created_by, created_at FROM commai_workflow_versions
               WHERE workflow_id = %s ORDER BY version DESC""",
            (wf["id"],),
        ).fetchall()
        v = workflows.validate(conn, customer_id, latest["definition"], strict=True)
        return {
            **workflows.summary(conn, wf),
            "definition": latest["definition"],
            "versions": versions,
            "preview": workflows.preview(latest["definition"]),
            "problems": v["problems"],
            "warnings": v["warnings"],
            "tools_needed": workflows.tools_needed(latest["definition"]),
            "schedule": workflows.schedule_of(conn, wf["id"]),
        }


@router.get("/workflows/{workflow_id}/versions/{n}")
def get_version(customer_id: str, workflow_id: str, n: int, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        wf = workflows.get(conn, customer_id, workflow_id)
        ver = workflows.version(conn, wf, n)
    return {**ver, "preview": workflows.preview(ver["definition"])}


@router.put("/workflows/{workflow_id}")
def update_workflow(customer_id: str, workflow_id: str, body: DefinitionIn, user: UserDep) -> dict:
    """Saves a new version. The live version keeps running until this one is published."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = workflows.update(conn, customer_id, workflow_id, body.definition, actor=user.actor)
        audit.record(
            conn, user.actor, "commai.workflow.update", workflow_id, customer_id, {"version": out["version"]["version"]}
        )
    return out


class PublishIn(BaseModel):
    version: int | None = None
    grant_tools: bool = False


@router.post("/workflows/{workflow_id}/publish")
def publish_workflow(customer_id: str, workflow_id: str, body: PublishIn, user: UserDep) -> dict:
    """A person makes a version live. With grant_tools, the person also allows
    the workflow role exactly the actions this version uses."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = workflows.publish(
            conn, customer_id, workflow_id, actor=user.actor, version_n=body.version, grant_tools=body.grant_tools
        )
        audit.record(
            conn,
            user.actor,
            "commai.workflow.publish",
            workflow_id,
            customer_id,
            {"version": out["version"]["version"], "grant_tools": body.grant_tools},
        )
    return out


@router.post("/workflows/{workflow_id}/pause")
def pause_workflow(customer_id: str, workflow_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        wf = workflows.pause(conn, customer_id, workflow_id, actor=user.actor)
        audit.record(conn, user.actor, "commai.workflow.pause", workflow_id, customer_id)
    return wf


@router.post("/workflows/{workflow_id}/resume")
def resume_workflow(customer_id: str, workflow_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        wf = workflows.resume(conn, customer_id, workflow_id, actor=user.actor)
        audit.record(conn, user.actor, "commai.workflow.resume", workflow_id, customer_id)
    return wf


@router.delete("/workflows/{workflow_id}", status_code=204)
def delete_workflow(customer_id: str, workflow_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        wf = workflows.get(conn, customer_id, workflow_id, lock=True)
        if wf["status"] == "live":
            raise HTTPException(409, "Pause the workflow before deleting it.")
        conn.execute("DELETE FROM commai_workflows WHERE id = %s", (wf["id"],))
        audit.record(conn, user.actor, "commai.workflow.delete", workflow_id, customer_id)


class TestIn(BaseModel):
    version: int | None = None
    event_id: str | None = None
    sample: dict[str, dict[str, Any]] | None = None


@router.post("/workflows/{workflow_id}/test")
def test_workflow(customer_id: str, workflow_id: str, body: TestIn, user: UserDep) -> dict:
    """A dry run against a recent event (or a sample): no messages, notes or actions."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = workflows.dry_run(
            conn,
            customer_id,
            workflow_id,
            actor=user.actor,
            version_n=body.version,
            event_id=body.event_id,
            sample=body.sample,
        )
        audit.record(conn, user.actor, "commai.workflow.test", workflow_id, customer_id)
    return out


class TriggerIn(BaseModel):
    conversation_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@router.post("/workflows/{workflow_id}/trigger", status_code=201)
def trigger_workflow(customer_id: str, workflow_id: str, body: TriggerIn, user: UserDep) -> dict:
    """Start a live workflow now (ADR 0033), optionally for one conversation.
    `data` is available to its steps as {{event.data.input.<name>}}."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, _errors():
        access.require_reply_seat(conn, user, customer_id)
        run = workflows.trigger(
            conn, customer_id, workflow_id, actor=user.actor, conversation_id=body.conversation_id, data=body.data
        )
        audit.record(conn, user.actor, "commai.workflow.trigger", workflow_id, customer_id, {"run": str(run["id"])})
    return {"run_id": str(run["id"]), "status": run["status"], "workflow_id": workflow_id}


@router.get("/workflows/{workflow_id}/sample-events")
def sample_events(customer_id: str, workflow_id: str, user: UserDep) -> list[dict]:
    """Recent events that could start this workflow, to test against."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        wf = workflows.get(conn, customer_id, workflow_id)
        ev = workflows.start_event_type(workflows.version(conn, wf)["definition"])
        return conn.execute(
            "SELECT id, type, subject, at FROM commai_events WHERE customer_id = %s AND type = %s"
            " ORDER BY seq DESC LIMIT 10",
            (customer_id, ev),
        ).fetchall()


@router.get("/workflows/{workflow_id}/runs")
def list_runs(
    customer_id: str,
    workflow_id: str,
    user: UserDep,
    test: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    access.check(user, customer_id, "commai:read")
    after, args = paging.where(cursor, "started_at")
    with db.tx() as conn:
        rows = conn.execute(
            f"""SELECT id, version, event_id, conversation_id, test, status, step_index, error, started_at, finished_at,
                      wait->>'prompt' AS approval_prompt
               FROM commai_workflow_runs WHERE workflow_id = %s AND customer_id = %s
               AND (%s::boolean IS NULL OR test = %s){after} ORDER BY started_at DESC, id::text DESC LIMIT %s""",
            (workflow_id, customer_id, test, test, *args, limit + 1),
        ).fetchall()
    return paging.result(rows, cursor, limit, "started_at")


@router.get("/workflow-runs/{run_id}")
def get_run(customer_id: str, run_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        run = conn.execute(
            """SELECT r.id, r.workflow_id, w.name AS workflow, r.version, r.event_id, r.conversation_id, r.test,
                      r.status, r.step_index, r.error, r.wait, r.started_at, r.finished_at, r.context->'vars' AS vars
               FROM commai_workflow_runs r JOIN commai_workflows w ON w.id = r.workflow_id
               WHERE r.id = %s AND r.customer_id = %s""",
            (run_id, customer_id),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Run not found.")
        steps = conn.execute(
            "SELECT step_index, step_id, type, status, summary, detail, at FROM commai_workflow_run_steps"
            " WHERE run_id = %s ORDER BY id",
            (run_id,),
        ).fetchall()
    run["wait"] = {k: v for k, v in (run["wait"] or {}).items() if k != "token"}
    return {**run, "steps": steps}


@router.get("/workflow-approvals")
def pending_approvals(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return conn.execute(
            """SELECT r.id, r.workflow_id, w.name AS workflow, r.conversation_id, r.wait->>'prompt' AS prompt,
                      r.wait->>'since' AS since
               FROM commai_workflow_runs r JOIN commai_workflows w ON w.id = r.workflow_id
               WHERE r.customer_id = %s AND r.status = 'awaiting_approval' ORDER BY r.updated_at""",
            (customer_id,),
        ).fetchall()


class DecideIn(BaseModel):
    note: str = Field(default="", max_length=300)


@router.post("/workflow-runs/{run_id}/approve")
def approve_run(customer_id: str, run_id: str, body: DecideIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, _errors():
        access.require_reply_seat(conn, user, customer_id)
        run = workflows.decide(conn, customer_id, run_id, approve=True, actor=user.actor, note=body.note)
        audit.record(conn, user.actor, "commai.workflow.approve", run_id, customer_id)
    return {"id": run["id"], "status": run["status"]}


@router.post("/workflow-runs/{run_id}/reject")
def reject_run(customer_id: str, run_id: str, body: DecideIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, _errors():
        access.require_reply_seat(conn, user, customer_id)
        run = workflows.decide(conn, customer_id, run_id, approve=False, actor=user.actor, note=body.note)
        audit.record(conn, user.actor, "commai.workflow.reject", run_id, customer_id)
    return {"id": run["id"], "status": run["status"]}


@router.post("/workflow-runs/{run_id}/cancel")
def cancel_run(customer_id: str, run_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        run = workflows.cancel_run(conn, customer_id, run_id, actor=user.actor)
        audit.record(conn, user.actor, "commai.workflow.cancel_run", run_id, customer_id)
    return {"id": run["id"], "status": run["status"]}


# ==== onboarding =====================================================================


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class OnboardingIn(BaseModel):
    business_name: str = Field(default="", max_length=120)
    business_type: str = Field(default="", max_length=80)
    website_text: str = Field(default="", max_length=60_000)
    website_url: str = Field(default="", max_length=300)
    hours: str = Field(default="", max_length=500)
    opening_hours: dict[str, list[str] | None] | None = None  # {"mon": ["09:00", "17:00"], "sun": None}
    locations: list[str] = Field(default_factory=list, max_length=20)
    channels: list[str] = Field(default_factory=list, max_length=10)
    teams: list[TeamIn | str] = Field(default_factory=list, max_length=20)


@router.post("/onboarding", status_code=201)
def start_onboarding(customer_id: str, body: OnboardingIn, request: Request, user: UserDep) -> dict:
    """Draft a profile, teams, knowledge, routing and starter workflows. Nothing
    goes live until each draft is approved."""
    access.check(user, customer_id, "commai:admin")
    data = body.model_dump()
    data["teams"] = [t if isinstance(t, str) else t["name"] for t in data["teams"]]
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = onboarding.draft(conn, customer_id, data, actor=user.actor, settings=_settings(request))
        audit.record(
            conn, user.actor, "commai.onboarding.draft", out["batch"], customer_id, {"drafts": len(out["drafts"])}
        )
    return out


@router.get("/onboarding")
def list_onboarding(customer_id: str, user: UserDep, batch: str | None = None) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        if not batch:
            last = conn.execute(
                "SELECT batch FROM commai_onboarding_drafts WHERE customer_id = %s ORDER BY created_at DESC LIMIT 1",
                (customer_id,),
            ).fetchone()
            batch = str(last["batch"]) if last else None
        drafts = (
            conn.execute(
                "SELECT * FROM commai_onboarding_drafts WHERE customer_id = %s AND batch::text = %s"
                " ORDER BY created_at",
                (customer_id, batch),
            ).fetchall()
            if batch
            else []
        )
    return {"batch": batch, "drafts": drafts}


class DraftEditIn(BaseModel):
    content: dict[str, Any]


@router.patch("/onboarding/{draft_id}")
def edit_draft(customer_id: str, draft_id: str, body: DraftEditIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = onboarding.edit(conn, customer_id, draft_id, body.content, user.actor)
        audit.record(conn, user.actor, "commai.onboarding.edit", draft_id, customer_id)
    return row


@router.post("/onboarding/{draft_id}/approve")
def approve_draft(customer_id: str, draft_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = onboarding.approve(conn, customer_id, draft_id, user.actor)
        audit.record(conn, user.actor, "commai.onboarding.approve", draft_id, customer_id, {"kind": row["kind"]})
    return row


@router.post("/onboarding/{draft_id}/reject")
def reject_draft(customer_id: str, draft_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = onboarding.reject(conn, customer_id, draft_id, user.actor)
        audit.record(conn, user.actor, "commai.onboarding.reject", draft_id, customer_id)
    return row


# ==== platform assistant and support cases ============================================


class QuestionIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


@router.post("/assistant/ask")
def ask_assistant(customer_id: str, body: QuestionIn, request: Request, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = _voice_change(conn, customer_id, body.question, user)
        if out is None:
            out = assistant.ask(conn, customer_id, body.question, actor=user.actor, settings=_settings(request))
        audit.record(conn, user.actor, "commai.assistant.ask", out["id"] or "", customer_id)
    return out


def _voice_change(conn, customer_id: str, text: str, user) -> dict | None:
    """A phone-system change typed to the assistant ("forward my calls to my
    mobile until 5") becomes the same proposal as /voice/say, with the same
    confirm step (ADR 0033). Questions, and anything the voice parser doesn't
    recognise, go to the diagnostics as before."""
    from .. import entitlements
    from ..voice import selfservice
    from ..voice.common import VoiceError

    if assistant.SECRET_HINT.search(text) or not selfservice.looks_like_change(text):
        return None
    if not entitlements.enabled(conn, customer_id, "voice"):
        return None
    try:
        with conn.transaction():
            vc = selfservice.propose_text(conn, customer_id, user, text)
    except VoiceError as e:
        vc = {"understood": False, "message": str(e)}
    if not vc["understood"] and vc["message"].startswith("I didn't understand"):
        return None
    if vc["understood"]:
        answer = f"I can make this phone change: {vc['summary']} Nothing changes until you confirm it."
    else:
        answer = vc["message"]
    row = conn.execute(
        """INSERT INTO commai_assistant_answers (customer_id, question, answer, confidence, source, asked_by)
           VALUES (%s, %s, %s, 'confirmed', 'voice', %s) RETURNING id, question""",
        (customer_id, text[:1000], answer, user.actor),
    ).fetchone()
    if vc["understood"]:
        audit.record(
            conn, user.actor, "commai.voice.say", vc["id"], customer_id, {"scope": vc["scope"], "via": "assistant"}
        )
    return {
        "id": str(row["id"]),
        "question": row["question"],
        "answer": answer,
        "confidence": "confirmed",
        "findings": [],
        "fixes": [],
        "source": "voice",
        "voice_change": vc,
    }


@router.get("/assistant/history")
def assistant_history(
    customer_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(20, ge=1, le=100)
) -> Any:
    access.check(user, customer_id, "commai:admin")
    after, args = paging.where(cursor)
    with db.tx() as conn:
        rows = conn.execute(
            f"""SELECT id, question, answer, confidence, findings, source, asked_by, created_at
               FROM commai_assistant_answers WHERE customer_id = %s{after}
               ORDER BY created_at DESC, id::text DESC LIMIT %s""",
            (customer_id, *args, limit + 1),
        ).fetchall()
        rows = rows[: limit + (cursor is not None)]
        for r in rows:
            r["fixes"] = conn.execute(
                "SELECT * FROM commai_assistant_fixes WHERE answer_id = %s ORDER BY proposed_at", (r["id"],)
            ).fetchall()
    return paging.result(rows, cursor, limit)


@router.get("/assistant/checks")
def assistant_checks(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        return {
            "checks": diagnostics.names(),
            "fixes": assistant.fix_ids(),
            "findings": diagnostics.run(conn, customer_id),
        }


@router.post("/assistant/fixes/{fix_id}/apply")
def apply_fix(customer_id: str, fix_id: str, user: UserDep) -> dict:
    """An admin approves a proposed fix; it is applied and the check run again."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = assistant.apply_fix(conn, customer_id, fix_id, actor=user.actor)
        audit.record(
            conn,
            user.actor,
            "commai.assistant.fix",
            fix_id,
            customer_id,
            {"fix": out["fix_id"], "status": out["status"], "resolved": out["resolved"]},
        )
    return out


@router.post("/assistant/fixes/{fix_id}/reject")
def reject_fix(customer_id: str, fix_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = assistant.reject_fix(conn, customer_id, fix_id, actor=user.actor)
        audit.record(conn, user.actor, "commai.assistant.fix_reject", fix_id, customer_id)
    return row


class CaseIn(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    question: str = Field(default="", max_length=1000)
    answer_id: str | None = None


@router.post("/support-cases", status_code=201)
def open_case(customer_id: str, body: CaseIn, user: UserDep) -> dict:
    """Open a support case with ExaCarib: configuration, redacted errors,
    diagnostics and correlation ids are attached automatically."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = assistant.open_case(
            conn, customer_id, subject=body.subject, question=body.question, actor=user.actor, answer_id=body.answer_id
        )
        audit.record(conn, user.actor, "commai.support_case.open", row["reference"], customer_id)
    return row


@router.get("/support-cases")
def list_cases(
    customer_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(100, ge=1, le=200)
) -> Any:
    access.check(user, customer_id, "commai:admin")
    after, args = paging.where(cursor)
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT id, reference, subject, status, priority, updated_at, created_by, created_at"
            f" FROM commai_support_cases WHERE customer_id = %s{after} ORDER BY created_at DESC, id::text DESC"
            " LIMIT %s",
            (customer_id, *args, limit + 1),
        ).fetchall()
    return paging.result(rows, cursor, limit)


@router.get("/support-cases/{case_id}")
def get_case(customer_id: str, case_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM commai_support_cases WHERE id::text = %s AND customer_id = %s", (case_id, customer_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Support case not found.")
    return row


# ==== reports and usage limits =========================================================


def _period(start: dt.datetime | None, end: dt.datetime | None) -> tuple[dt.datetime, dt.datetime]:
    try:
        return reports.period(start, end)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/reports/outcomes")
def outcome_report(
    customer_id: str,
    user: UserDep,
    start: dt.datetime | None = Query(None, alias="from"),
    end: dt.datetime | None = Query(None, alias="to"),
) -> dict:
    """Outcomes counted from recorded events (acceptance test 10)."""
    access.check(user, customer_id, "commai:read")
    s, e = _period(start, end)
    with db.tx() as conn:
        out = reports.outcomes(conn, customer_id, s, e)
    return out


@router.get("/reports/usage")
def usage_report(
    customer_id: str,
    user: UserDep,
    start: dt.datetime | None = Query(None, alias="from"),
    end: dt.datetime | None = Query(None, alias="to"),
) -> dict:
    access.check(user, customer_id, "commai:read")
    s, e = _period(start, end)
    with db.tx() as conn:
        return reports.usage_report(conn, customer_id, s, e)


@router.get("/usage-limits")
def list_limits(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return reports.limits(conn, customer_id)


class LimitIn(BaseModel):
    monthly_alert: float | None = Field(default=None, ge=0)
    monthly_hard: float | None = Field(default=None, ge=0)


@router.put("/usage-limits/{meter}")
def set_limit(customer_id: str, meter: str, body: LimitIn, user: UserDep) -> dict:
    """Budgets: an alert level and a hard monthly limit that stops the metered work."""
    access.check(user, customer_id, "commai:admin")
    if len(meter) > 60:
        raise HTTPException(422, "Meter name too long.")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = reports.set_limit(conn, customer_id, meter, body.monthly_alert, body.monthly_hard)
        audit.record(conn, user.actor, "commai.usage_limit.set", meter, customer_id, body.model_dump())
    return row


@router.delete("/usage-limits/{meter}", status_code=204)
def delete_limit(customer_id: str, meter: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        conn.execute("DELETE FROM usage_limits WHERE customer_id = %s AND meter = %s", (customer_id, meter))
        audit.record(conn, user.actor, "commai.usage_limit.delete", meter, customer_id)
