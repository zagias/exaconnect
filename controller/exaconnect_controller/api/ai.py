"""Insights (storm warnings, disaster watch, bill-shock forecasts, carrier anomalies) and
"Ask your network"."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db
from ..ai import act, guidance, hazards, hub, runner, storms
from ..ai import ask as ask_mod
from .deps import VIEWER_ONLY, AdminDep, UserDep, can_manage_org, check_customer, check_product, require_org_manager

router = APIRouter(tags=["ai"])
shared = APIRouter(prefix="/assistant", tags=["assistant"])


def _check_action_permissions(user, customer_id, actions: list[dict]) -> None:
    """Assistant changes need the same rights as their regular API endpoints."""
    check_customer(user, customer_id)
    check_product(user, "connect")
    if user.scopes is not None and "connect" not in user.scopes:
        raise HTTPException(403, "This account cannot apply network changes.")
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
    out["can_execute"] = bool(
        out.get("can_execute") and problem is None and plan.get("approved_by") == user.actor and user.via != "key"
    )
    return out


def _customer(user, requested: str | None) -> str:
    if requested is not None:
        check_customer(user, requested)
    customer_id = user.customer_id if user.role == "customer" else requested
    if not customer_id:
        raise HTTPException(400, "Choose an organisation first.")
    check_customer(user, customer_id)
    return customer_id


def _network_access(user) -> tuple[bool, bool]:
    try:
        check_product(user, "connect")
    except HTTPException:
        return False, False
    read = user.scopes is None or bool({"connect", "connect:read"}.intersection(user.scopes))
    write = read and user.org_role != "viewer" and (user.scopes is None or "connect" in user.scopes)
    return read, write


@dataclass(frozen=True)
class _AssistantLLM:
    api_key: str = field(repr=False)
    base_url: str
    model: str
    provider: str | None
    unavailable_reason: str | None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.unavailable_reason is None


def _assistant_llm(settings) -> _AssistantLLM:
    """Resolve a whole Hub connection, never borrow global credentials for an override."""
    values = tuple(getattr(settings, "assistant_llm_" + name, "") or "" for name in ("api_key", "base_url", "model"))
    if any(values):
        if not all(isinstance(value, str) and value for value in values):
            return _AssistantLLM("", "", "", None, "The assistant's AI connection is incomplete.")
        key, base, model = values
        if (
            base.rstrip("/") != ask_mod.OPENAI_BASE_URL
            or model != ask_mod.OPENAI_ASSISTANT_MODEL
            or any(char.isspace() for char in key)
        ):
            return _AssistantLLM("", "", "", None, "The assistant's AI connection is not supported.")
        return _AssistantLLM(key, ask_mod.OPENAI_BASE_URL, model, "openai", None)
    key, base, model = settings.llm_api_key, settings.llm_base_url, settings.llm_model
    provider = (
        "openai"
        if base.rstrip("/") == ask_mod.OPENAI_BASE_URL
        else "deepinfra"
        if base.rstrip("/") == "https://api.deepinfra.com/v1/openai"
        else "compatible"
    )
    return _AssistantLLM(
        key,
        base,
        model,
        provider if key else None,
        None if key else "The assistant is not connected to an AI service yet.",
    )


def _hub_context(conn, customer_id, user, settings) -> dict:
    from ..commai import access

    read, write = _network_access(user)
    can_check_jibsy = False
    try:
        check_product(user, "commai")
        # Match the original diagnostic endpoint, including custom business
        # permissions for that path; a shared route must not widen them.
        diagnostic_user = replace(user, path=f"/api/v1/commai/customers/{customer_id}/assistant/ask")
        access.check(diagnostic_user, customer_id, "commai:admin")
        access.require_business_admin(diagnostic_user)
        can_check_jibsy = access.seat(conn, diagnostic_user, customer_id) != "internal"
    except HTTPException:
        pass
    out = hub.context(
        conn, customer_id, user, connect_read=read, connect_write=write, jibsy_diagnostics=can_check_jibsy
    )
    connection = _assistant_llm(settings)
    out.update(
        ask_enabled=connection.enabled,
        ask_unavailable_reason=connection.unavailable_reason,
        model=connection.model if connection.enabled else None,
        provider=connection.provider,
    )
    return ask_mod._jsonable(out)


@shared.get("/context")
def assistant_context(request: Request, user: UserDep, customer_id: str | None = None) -> dict:
    customer_id = _customer(user, customer_id)
    with db.tx() as conn:
        try:
            return _hub_context(conn, customer_id, user, request.app.state.settings)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None


@router.get("/ai/status")
def status(request: Request, user: UserDep) -> dict:
    s = request.app.state.settings
    connection = _assistant_llm(s)
    may_ask = user.role != "carrier" and (user.scopes is None or "connect" in user.scopes)
    unavailable = None
    if not may_ask:
        unavailable = "Asking the network assistant is not available with this account's access."
    elif not connection.enabled:
        unavailable = connection.unavailable_reason
    return {
        "ask_enabled": connection.enabled and may_ask,
        "ask_unavailable_reason": unavailable,
        "model": connection.model if connection.enabled else None,
        "provider": connection.provider,
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
    scope: Literal["account", "connect"] = "account"


@router.post("/ai/ask")
async def ask(body: AskIn, user: UserDep, request: Request) -> dict:
    """Answer from the customer's own data; when asked for a change or a fix,
    also propose one as a plan that a person confirms (ADR 0015)."""
    return await _answer(body, user, request, shared_mode=False)


@shared.post("/ask")
async def assistant_ask(body: AskIn, user: UserDep, request: Request) -> dict:
    return await _answer(body, user, request, shared_mode=True)


async def _answer(body: AskIn, user, request: Request, *, shared_mode: bool) -> dict:
    s = request.app.state.settings
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")
    site_request = guidance.site_intent(body.question) if shared_mode else None
    customer_id = _customer(user, body.customer_id)
    network_read, network_write = _network_access(user)
    include_network = not shared_mode or body.scope == "connect"
    if include_network and not network_read:
        raise HTTPException(403, "Network data is not available with this account's product access and scope.")
    # Reuse Jibsy's existing secure-entry boundary before logging, persisting or
    # sending a question/history to an external model service.
    from ..commai.automation.assistant import NO_SECRETS, SECRET_HINT

    if any(SECRET_HINT.search(text) for text in [body.question, *[t.q + "\n" + t.a for t in body.history]]):
        return {
            "answer": NO_SECRETS,
            "plan": None,
            "model": "rules",
            "based_on": {"decisions": 0, "events": 0, "insights": 0, "sites": 0, "sources": []},
        }

    if site_request is not None:
        if not network_read:
            raise HTTPException(403, "Network data is not available with this account's product access and scope.")

        def read_site_context():
            with db.tx() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                org = conn.execute("SELECT name, example FROM customers WHERE id = %s", (customer_id,)).fetchone()
                if org is None:
                    raise HTTPException(404, "Customer not found.")
                context = ask_mod.build_context(conn, customer_id) if site_request == "status" else {}
                context["account"] = {"example": bool(org.get("example")), "organisation": org.get("name")}
                return context

        site_context = await asyncio.to_thread(read_site_context)
        answer = (
            "\n\n".join(
                guidance.example_note(site_context)
                + [
                    "Do you mean getting disconnected sites reporting again, "
                    "or recovering deleted site records or configuration?",
                    "Nothing has been changed or restored.",
                ]
            )
            if site_request == "restore"
            else guidance.site_status_answer(site_context)
        )
        return {
            "answer": answer,
            "plan": None,
            "model": "rules",
            "answer_scope": "connect" if site_request == "status" else body.scope,
            "based_on": {
                "decisions": 0,
                "events": 0,
                "insights": 0,
                "sites": len(site_context.get("sites", [])),
                "sources": ["network_snapshot" if site_request == "status" else "organisation_access"],
                "grounding": "verified_read_only_status",
            },
        }

    connection = _assistant_llm(s)
    if not connection.enabled:
        raise HTTPException(503, connection.unavailable_reason)

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
            context = _hub_context(conn, customer_id, user, s) if shared_mode else {}
            if include_network:
                context.update(ask_mod.build_context(conn, customer_id))
                if shared_mode:
                    context["sources"].append("network_snapshot")
            if shared_mode:
                context["tools"]["connect_changes"] = network_write and include_network
                context["assistant_scope"] = body.scope
                guidance.attach_evidence(context)
            return context, act.config(conn, customer_id) if include_network else {}

    context, config = await asyncio.to_thread(prepare)
    config["assistant_permissions"] = {
        "can_apply_changes": network_write and include_network,
        "can_change_shadow_mode": can_manage_org(user) and network_write and include_network,
    }
    try:
        text = await asyncio.to_thread(
            ask_mod.chat,
            hub.SYSTEM + "\n" + act.ACTION_SYSTEM if shared_mode else act.SYSTEM,
            act.prompt(body.question, {**context, "configuration": config}, [t.model_dump() for t in body.history]),
            api_key=connection.api_key,
            base_url=connection.base_url,
            model=connection.model,
            max_tokens=1400,
            temperature=0.1,
        )
    except ask_mod.AskError as e:
        raise HTTPException(502, str(e)) from None
    answer, actions = act.parse(text)
    # The model cannot turn an advice-only scope into an available tool.
    if shared_mode and (not include_network or not network_write):
        actions = []
    grounding = None
    selection = None
    if shared_mode:
        answer, actions, grounding = guidance.render(text, answer, actions, context, body.scope)
        if grounding == "verified_advisory_selection":
            details = guidance.selection_metadata(text)
            if details["normalised"]:
                selection = details

    def propose():
        with db.tx() as conn:
            problem = _permission_problem(user, customer_id, actions)
            plan = act.create(conn, customer_id, body.question, answer, actions, user.actor, dry_run=problem is None)
            if plan and problem:
                plan["problems"].append(problem)
            return plan

    plan = await asyncio.to_thread(propose) if actions else None
    if shared_mode and plan:
        answer = guidance.proposal_answer(plan, context)
    return {
        "answer": answer,
        "plan": plan,
        "model": connection.model,
        "provider": connection.provider,
        "based_on": {
            "decisions": len(context.get("routing_decisions_last_7_days_newest_first", [])),
            "events": len(context.get("events_last_24h_newest_first", [])),
            "insights": len(context.get("open_insights", [])),
            "sites": len(context.get("sites", [])),
            "sources": context.get("sources", ["tenant_network_snapshot"]),
            **({"grounding": grounding} if grounding else {}),
            **({"advice_selection": selection} if selection else {}),
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


@shared.get("/plans")
def assistant_plans(user: UserDep, customer_id: str | None = None, limit: int = 20) -> list[dict]:
    customer_id = _customer(user, customer_id)
    check_product(user, "connect")
    if not _network_access(user)[0]:
        raise HTTPException(403, "Network data is not available with this account's scope.")
    return list_plans(customer_id, user, limit)


def _plan(conn, plan_id: int, user) -> dict:
    plan = act.get(conn, plan_id, lock=True)
    if plan is None:
        raise HTTPException(404, "Not found.")
    check_customer(user, plan["customer_id"])
    check_product(user, "connect")
    if not _network_access(user)[0]:
        raise HTTPException(403, "Network data is not available with this account's scope.")
    return plan


def _plan_step(plan_id: int, user, step) -> dict:
    with db.tx() as conn:
        plan = _plan(conn, plan_id, user)
        if step in (act.apply, act.undo, act.cancel):
            _check_action_permissions(user, plan["customer_id"], plan["actions"])
            if user.via == "key":
                raise HTTPException(403, "Sign in as a person to change a proposal's configuration.")
        try:
            with conn.transaction():
                step(conn, plan, user.actor)
        except act.ApprovalError as e:
            raise HTTPException(409, str(e)) from None
        except act.PlanError as e:
            failed = str(e)
        else:
            failed = None
        if failed is None:
            return _view_plan(conn, act.get(conn, plan_id), user)
    raise HTTPException(400, failed)


@router.get("/ai/plans/{plan_id}")
@shared.get("/plans/{plan_id}")
def get_plan(plan_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        return _view_plan(conn, _plan(conn, plan_id, user), user)


class ApprovalIn(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecuteIn(ApprovalIn):
    idempotency_key: UUID


def _authorise_step(plan_id, user, body, *, execute: bool) -> dict:
    with db.tx() as conn:
        plan = _plan(conn, plan_id, user)
        _check_action_permissions(user, plan["customer_id"], plan["actions"])
        if user.via == "key":
            raise HTTPException(403, "Sign in as a person to approve or apply a proposal.")
        if body is None:
            raise HTTPException(409, "Review and approve this exact revision before applying its changes.")
        try:
            if execute:
                act.execute(conn, plan, user.actor, body.revision, str(body.idempotency_key))
            else:
                act.approve(conn, plan, user.actor, body.revision)
        except act.ApprovalError as e:
            raise HTTPException(409, str(e)) from None
        except act.PlanError as e:
            raise HTTPException(400, str(e)) from None
        return _view_plan(conn, act.get(conn, plan_id), user)


@shared.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: int, body: ApprovalIn, user: UserDep) -> dict:
    return _authorise_step(plan_id, user, body, execute=False)


@router.post("/ai/plans/{plan_id}/apply")
@shared.post("/plans/{plan_id}/execute")
def apply_plan(plan_id: int, user: UserDep, body: ExecuteIn | None = None) -> dict:
    """Apply the approved revision atomically; a same-key retry returns its result."""
    return _authorise_step(plan_id, user, body, execute=True)


@router.post("/ai/plans/{plan_id}/undo")
@shared.post("/plans/{plan_id}/undo")
def undo_plan(plan_id: int, user: UserDep) -> dict:
    """Reverse applied changes, last first."""
    return _plan_step(plan_id, user, act.undo)


@router.post("/ai/plans/{plan_id}/cancel")
@shared.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: int, user: UserDep) -> dict:
    return _plan_step(plan_id, user, act.cancel)
