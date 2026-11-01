"""Insights (storm warnings, disaster watch, bill-shock forecasts, carrier anomalies) and
"Ask your network"."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db
from ..ai import act, hazards, runner, storms
from ..ai import ask as ask_mod
from .deps import VIEWER_ONLY, AdminDep, UserDep, can_manage_org, check_customer, require_org_manager

router = APIRouter(tags=["ai"])


def _check_action_permissions(user, customer_id, actions: list[dict]) -> None:
    """Assistant changes need the same rights as their regular API endpoints."""
    check_customer(user, customer_id)
    if actions and user.org_role == "viewer":
        raise HTTPException(403, VIEWER_ONLY)
    # Changing shadow mode is an organisation setting, unlike traffic rules,
    # SLAs and Storm Mode, which any non-viewer member may manage.
    if any(a.get("action") == "shadow_mode" for a in actions):
        require_org_manager(user, customer_id)


def _permission_problem(user, customer_id, actions: list[dict]) -> str | None:
    try:
        _check_action_permissions(user, customer_id, actions)
    except HTTPException as e:
        return str(e.detail)
    return None


def _view_plan(conn, plan: dict, user, dry_run: bool = True) -> dict:
    problem = _permission_problem(user, plan["customer_id"], plan["actions"])
    out = act.view(conn, plan, dry_run=dry_run and problem is None)
    if problem and plan["status"] in ("draft", "applied"):
        out["problems"].append(problem)
    return out


@router.get("/ai/status")
def status(request: Request, user: UserDep) -> dict:
    s = request.app.state.settings
    may_ask = user.role != "carrier" and (user.scopes is None or "connect" in user.scopes)
    unavailable = None
    if not may_ask:
        unavailable = "Asking the network assistant is not available with this account's access."
    elif not s.llm_api_key:
        unavailable = "The network assistant is not connected to an AI service yet."
    return {
        "ask_enabled": bool(s.llm_api_key) and may_ask,
        "ask_unavailable_reason": unavailable,
        "model": s.llm_model if s.llm_api_key else None,
        "storm_watch": bool(s.nhc_url),
        "hazard_watch": runner.hazard_watch_on(s),
    }


@router.get("/insights")
def list_insights(
    user: UserDep,
    customer_id: str | None = None,
    kind: Literal["storm_warning", "hazard", "bill_shock", "anomaly"] | None = None,
    include_resolved: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Open insights, newest first. Carrier users see anomalies on their own links only."""
    params = {
        "c": customer_id,
        "k": kind,
        "r": include_resolved,
        "carrier": None,
        "l": max(1, min(limit, 500)),
    }
    if user.role == "customer":
        params["c"] = user.customer_id
    elif user.role == "carrier":
        if not user.carrier_id:
            raise HTTPException(403, "Not available for this account.")
        params.update(c=None, k="anomaly", carrier=user.carrier_id)
    with db.tx() as conn:
        # A link can change carrier before the next anomaly pass. Hide its
        # previous insight from both carriers until the data is refreshed.
        return conn.execute(
            """SELECT i.id, i.customer_id, i.kind, i.severity, i.title, i.detail, i.data, i.example,
                      i.first_seen, i.last_seen, i.resolved_at, i.acknowledged_by, i.acknowledged_at,
                      s.name AS site, l.path, ca.name AS carrier
               FROM insights i LEFT JOIN sites s ON s.id = i.site_id LEFT JOIN links l ON l.id = i.link_id
               LEFT JOIN carriers ca ON ca.id = i.carrier_id
               WHERE (%(c)s::uuid IS NULL OR i.customer_id = %(c)s)
                 AND (%(k)s::text IS NULL OR i.kind = %(k)s)
                 AND (%(carrier)s::uuid IS NULL OR
                      (i.carrier_id = %(carrier)s AND l.carrier_id = %(carrier)s))
                 AND (%(r)s OR i.resolved_at IS NULL)
               ORDER BY i.resolved_at IS NOT NULL,
                        CASE i.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, i.last_seen DESC
               LIMIT %(l)s""",
            params,
        ).fetchall()


@router.post("/insights/{insight_id}/acknowledge")
def acknowledge(insight_id: int, user: UserDep) -> dict:
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE insights SET acknowledged_by = %s, acknowledged_at = now()
               WHERE id = %s AND (%s::uuid IS NULL OR customer_id = %s) RETURNING id, customer_id, title""",
            (
                user.actor,
                insight_id,
                user.customer_id if user.role == "customer" else None,
                user.customer_id if user.role == "customer" else None,
            ),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Insight not found.")
        audit.record(conn, user.actor, "insight.acknowledge", row["title"], row["customer_id"])
    return {"id": row["id"]}


@router.post("/ai/storm-watch/example")
def storm_example(user: AdminDep, request: Request, on: bool = True) -> dict:
    """Raise (or clear) warnings from a made-up hurricane near Jamaica and a
    made-up earthquake off Trinidad, labelled as example data, to show the
    hurricane and disaster watches without a real event. The earthquake is
    reported by three feeds and still raises one insight."""
    with db.tx() as conn:
        n = storms.run_once(conn, storms.example_feed() if on else {"activeStorms": []}, example=True)
        h = hazards.run_once(
            conn, hazards.example_reports() if on else [], example=True, nhc_on=bool(request.app.state.settings.nhc_url)
        )
        audit.record(conn, user.actor, "storm_watch.example", "on" if on else "off")
    return {"open_warnings": n, "open_hazards": h}


class Turn(BaseModel):
    q: str = Field(max_length=500)
    a: str = Field(default="", max_length=4000)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    customer_id: str | None = None
    # The last few turns, so "yes, do that" follows on from the answer before it.
    history: list[Turn] = Field(default_factory=list, max_length=act.MAX_HISTORY)


@router.post("/ai/ask")
async def ask(body: AskIn, user: UserDep, request: Request) -> dict:
    """Answer from the customer's own data; when asked for a change or a fix,
    also propose one as a plan that a person confirms (ADR 0015)."""
    s = request.app.state.settings
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")
    if not s.llm_api_key:
        raise HTTPException(503, "Ask your network is switched off: no AI service key is configured.")
    customer_id = user.customer_id if user.role == "customer" else body.customer_id
    if not customer_id:
        raise HTTPException(400, "Choose a customer first.")

    def prepare():
        with db.tx() as conn:
            if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
                raise HTTPException(404, "Customer not found.")
            used = conn.execute(
                "SELECT count(*) AS n FROM audit_log WHERE actor = %s AND action = 'ai.ask'"
                " AND at > now() - interval '1 hour'",
                (user.actor,),
            ).fetchone()["n"]
            if used >= s.llm_questions_per_hour:
                raise HTTPException(429, "That's the limit of questions for this hour. Try again later.")
            audit.record(conn, user.actor, "ai.ask", body.question[:200], customer_id)
            return ask_mod.build_context(conn, customer_id), act.config(conn, customer_id)

    context, config = await asyncio.to_thread(prepare)
    config["assistant_permissions"] = {
        "can_apply_changes": user.org_role != "viewer",
        "can_change_shadow_mode": can_manage_org(user),
    }
    try:
        text = await asyncio.to_thread(
            ask_mod.chat,
            act.SYSTEM,
            act.prompt(body.question, {**context, "configuration": config}, [t.model_dump() for t in body.history]),
            api_key=s.llm_api_key,
            base_url=s.llm_base_url,
            model=s.llm_model,
            max_tokens=1400,
            temperature=0.1,
        )
    except ask_mod.AskError as e:
        raise HTTPException(502, str(e)) from None
    answer, actions = act.parse(text)

    def propose():
        with db.tx() as conn:
            problem = _permission_problem(user, customer_id, actions)
            plan = act.create(conn, customer_id, body.question, answer, actions, user.actor, dry_run=problem is None)
            if plan and problem:
                plan["problems"].append(problem)
            return plan

    plan = await asyncio.to_thread(propose) if actions else None
    return {
        "answer": answer,
        "plan": plan,
        "model": s.llm_model,
        "based_on": {
            "decisions": len(context.get("routing_decisions_last_7_days_newest_first", [])),
            "events": len(context.get("events_last_24h_newest_first", [])),
            "insights": len(context.get("open_insights", [])),
            "sites": len(context.get("sites", [])),
        },
    }


@router.get("/customers/{customer_id}/assistant/plans")
def list_plans(customer_id: str, user: UserDep, limit: int = 20) -> list[dict]:
    """Changes the assistant proposed, newest first, with what became of them."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM assistant_plans WHERE customer_id = %s ORDER BY id DESC LIMIT %s",
            (customer_id, max(1, min(limit, 100))),
        ).fetchall()
        return [_view_plan(conn, r, user, dry_run=False) for r in rows]


def _plan(conn, plan_id: int, user) -> dict:
    plan = act.get(conn, plan_id, lock=True)
    if plan is None:
        raise HTTPException(404, "Not found.")
    check_customer(user, plan["customer_id"])
    return plan


def _plan_step(plan_id: int, user, step) -> dict:
    with db.tx() as conn:
        plan = _plan(conn, plan_id, user)
        if step in (act.apply, act.undo):
            _check_action_permissions(user, plan["customer_id"], plan["actions"])
        try:
            with conn.transaction():
                step(conn, plan, user.actor)
        except act.PlanError as e:
            failed = str(e)
        else:
            failed = None
        if failed is None:
            return _view_plan(conn, act.get(conn, plan_id), user)
    raise HTTPException(400, failed)


@router.get("/ai/plans/{plan_id}")
def get_plan(plan_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        return _view_plan(conn, _plan(conn, plan_id, user), user)


@router.post("/ai/plans/{plan_id}/apply")
def apply_plan(plan_id: int, user: UserDep) -> dict:
    """Apply every proposed change, or none of them."""
    return _plan_step(plan_id, user, act.apply)


@router.post("/ai/plans/{plan_id}/undo")
def undo_plan(plan_id: int, user: UserDep) -> dict:
    """Reverse applied changes, last first."""
    return _plan_step(plan_id, user, act.undo)


@router.post("/ai/plans/{plan_id}/cancel")
def cancel_plan(plan_id: int, user: UserDep) -> dict:
    return _plan_step(plan_id, user, act.cancel)
