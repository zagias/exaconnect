"""Shared advice must not widen data access or bypass reviewed human approval."""

from __future__ import annotations

import copy
import datetime as dt
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exaconnect_controller.ai import act, hub
from exaconnect_controller.api import ai
from exaconnect_controller.api.deps import User, current_user

REVISION = "a" * 64
KEY = "bb16161e-c990-46fd-9acf-c607b9ff42db"


@pytest.fixture
def lifecycle(monkeypatch):
    state = SimpleNamespace(
        plan={
            "id": 1,
            "customer_id": "org",
            "status": "draft",
            "question": "Enable Storm Mode",
            "actions": [{"action": "storm_mode", "on": True, "sites": ["site-a"]}],
            "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30),
        },
        revision=REVISION,
        writes=[],
        audits=[],
        collision=False,
    )
    reviewed = {"actions": copy.deepcopy(state.plan["actions"]), "summary": ["Enable Storm Mode"], "problems": []}

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, sql, params=()):
            if "SET approved_revision" in sql:
                state.plan.update(
                    approved_revision=params[0],
                    approved_actions=params[1].obj,
                    approved_by=params[2],
                    approved_at=dt.datetime.now(dt.UTC),
                )
            elif "SET execution_key" in sql:
                state.plan.update(execution_key=params[0], execution_revision=params[1])
            elif "AND execution_key =" in sql:
                return Result({"exists": True} if state.collision else None)
            return Result()

    def apply(_conn, plan, actor):
        state.writes.append(copy.deepcopy(plan["approved_actions"]))
        state.plan.update(status="applied", applied_by=actor)

    state.conn = Connection()
    monkeypatch.setattr(act, "review", lambda *_args, **_kwargs: copy.deepcopy(reviewed))
    monkeypatch.setattr(act, "revision", lambda *_: state.revision)
    monkeypatch.setattr(act, "apply", apply)
    monkeypatch.setattr(act.audit, "record", lambda _c, _actor, action, *_: state.audits.append(action))
    return state


def test_execution_requires_explicit_approval_and_exact_revision(lifecycle):
    s = lifecycle
    with pytest.raises(act.ApprovalError, match="Approve this exact"):
        act.execute(s.conn, s.plan, "user:a", REVISION, KEY)
    with pytest.raises(act.ApprovalError, match="changed"):
        act.approve(s.conn, s.plan, "user:a", "b" * 64)
    assert s.writes == []
    act.approve(s.conn, s.plan, "user:a", REVISION)
    assert s.writes == [] and s.audits == ["assistant.approve"]
    s.revision = "c" * 64
    with pytest.raises(act.ApprovalError, match="changed"):
        act.execute(s.conn, s.plan, "user:a", REVISION, KEY)
    assert s.writes == []


def test_approval_is_bound_to_person_and_normalised_actions(lifecycle, monkeypatch):
    s = lifecycle
    act.approve(s.conn, s.plan, "user:a", REVISION)
    with pytest.raises(act.ApprovalError, match="Approve this exact"):
        act.execute(s.conn, s.plan, "user:b", REVISION, KEY)
    monkeypatch.setattr(
        act, "review", lambda *_: {"actions": [{"action": "shadow_mode", "on": False}], "summary": [], "problems": []}
    )
    with pytest.raises(act.ApprovalError, match="changed"):
        act.execute(s.conn, s.plan, "user:a", REVISION, KEY)
    assert s.writes == []


def test_execution_retries_are_idempotent_even_after_expiry(lifecycle):
    s = lifecycle
    act.approve(s.conn, s.plan, "user:a", REVISION)
    act.execute(s.conn, s.plan, "user:a", REVISION, KEY)
    s.plan["expires_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    act.execute(s.conn, s.plan, "user:a", REVISION, KEY)
    assert len(s.writes) == 1
    assert s.audits == ["assistant.approve", "assistant.execute"]
    with pytest.raises(act.ApprovalError, match="already been applied"):
        act.execute(s.conn, s.plan, "user:a", REVISION, "other-key")


@pytest.mark.parametrize("step", ["approve", "execute"])
def test_expired_and_legacy_missing_expiry_fail_closed(lifecycle, step):
    s = lifecycle
    for expiry in (None, dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)):
        s.plan["expires_at"] = expiry
        with pytest.raises(act.ApprovalError, match="no longer available"):
            if step == "approve":
                act.approve(s.conn, s.plan, "user:a", REVISION)
            else:
                act.execute(s.conn, s.plan, "user:a", REVISION, KEY)
    assert s.writes == []


def test_key_cannot_be_reused_for_another_plan(lifecycle):
    s = lifecycle
    act.approve(s.conn, s.plan, "user:a", REVISION)
    s.collision = True
    with pytest.raises(act.ApprovalError, match="another proposal"):
        act.execute(s.conn, s.plan, "user:a", REVISION, KEY)
    assert s.writes == []


def test_revision_binds_the_current_configuration(monkeypatch):
    state = {"rules": [{"id": 1, "destination": "old"}]}
    monkeypatch.setattr(act, "config", lambda *_: copy.deepcopy(state))
    monkeypatch.setattr(act, "_sites", lambda *_: [])

    class Connection:
        def execute(self, *_):
            return SimpleNamespace(fetchone=lambda: {"shadow_mode": False}, fetchall=lambda: [])

    plan = {"id": 1, "customer_id": "org", "actions": [{"action": "delete_firewall_rule", "id": 1}]}
    reviewed = {"actions": plan["actions"], "summary": ["Delete rule 1"], "problems": []}
    before = act.revision(Connection(), plan, reviewed)
    state["rules"][0]["destination"] = "edited after review"
    assert act.revision(Connection(), plan, reviewed) != before


@pytest.mark.parametrize(
    "action",
    [
        {"action": "storm_mode", "on": "yes"},
        {"action": "shadow_mode", "on": None},
        {"action": "storm_mode", "on": True, "endpoint": "/arbitrary"},
    ],
)
def test_action_schema_rejects_ambiguous_or_model_selected_fields(action):
    with pytest.raises(act.PlanError):
        act._check(None, "org", action)


def test_oversized_proposal_stays_blocked_after_persistence(monkeypatch):
    stored = {}

    class Connection:
        def execute(self, _sql, params):
            stored["actions"] = params[3].obj
            return SimpleNamespace(fetchone=lambda: {"id": 1})

    monkeypatch.setattr(act, "_check", lambda _c, _cid, a: (a, "A proposed change"))
    monkeypatch.setattr(act, "get", lambda *_: stored)
    monkeypatch.setattr(act, "view", lambda conn, plan, **_: act.review(conn, "org", plan["actions"], dry_run=False))
    monkeypatch.setattr(act.audit, "record", lambda *_: None)
    out = act.create(
        Connection(), "org", "Change these", "Proposed", [{"action": "storm_mode"}] * 9, "user:a", dry_run=False
    )
    assert out["problems"] == ["At most 8 changes at once."]


@pytest.fixture
def shared_api(monkeypatch):
    user = User(
        "person",
        "person@example.invalid",
        "customer",
        "org",
        None,
        org_role="member",
        products=("commai",),
        apps=("commai",),
        scopes=("commai:read",),
    )
    state = SimpleNamespace(user=user, model_calls=[], data_reads=[], plans=[], actions=[])

    class Connection:
        def execute(self, *_):
            return SimpleNamespace(fetchone=lambda: {"n": 0})

    monkeypatch.setattr(ai.db, "tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(ai.audit, "record", lambda *_: None)
    monkeypatch.setattr(
        ai,
        "_hub_context",
        lambda *_: {
            "account": {"customer_id": "org", "products": ["commai"]},
            "sources": ["active_product_catalogue"],
            "tools": {"connect_changes": False},
            "verified_features": [],
            "catalogue": [],
        },
    )
    monkeypatch.setattr(ai.ask_mod, "build_context", lambda *_: state.data_reads.append("network") or {})
    monkeypatch.setattr(ai.act, "config", lambda *_: state.data_reads.append("configuration") or {})
    monkeypatch.setattr(ai.act, "create", lambda *_args, **_kwargs: state.plans.append("created"))

    def chat(_system, prompt, **_):
        state.model_calls.append(prompt)
        return json.dumps({"answer": "Here is the verified product advice.", "actions": state.actions})

    monkeypatch.setattr(ai.ask_mod, "chat", chat)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        llm_api_key="test", llm_base_url="unused", llm_model="test", llm_questions_per_hour=30
    )
    app.dependency_overrides[current_user] = lambda: state.user
    app.include_router(ai.shared, prefix="/api/v1")
    with TestClient(app) as client:
        state.client = client
        yield state


def test_jibsy_only_account_gets_advice_without_network_reads_or_model_tools(shared_api):
    s = shared_api
    s.actions = [{"action": "shadow_mode", "on": False}]
    r = s.client.post("/api/v1/assistant/ask", json={"question": "How can I add Connect?", "scope": "account"})
    assert r.status_code == 200, r.text
    assert r.json()["plan"] is None and r.json()["based_on"]["sources"] == ["active_product_catalogue"]
    assert s.data_reads == [] and s.plans == []
    assert '"can_apply_changes":false' in s.model_calls[0]


def test_network_scope_and_other_tenant_fail_before_reads_or_model_call(shared_api):
    s = shared_api
    for body in (
        {"question": "Show network", "scope": "connect"},
        {"question": "Show account", "customer_id": "other-org"},
    ):
        assert s.client.post("/api/v1/assistant/ask", json=body).status_code == 403
    assert s.model_calls == [] and s.data_reads == []
    assert s.client.get("/api/v1/assistant/plans").status_code == 403


@pytest.mark.parametrize("history", [False, True])
def test_pasted_credentials_never_reach_model_audit_or_proposal(shared_api, monkeypatch, history):
    s = shared_api
    audit_calls = []
    monkeypatch.setattr(ai.audit, "record", lambda *_: audit_calls.append("recorded"))
    text = "api_key: fictional-value-for-this-test"
    body = {"question": "Can you check my setup?", "history": []}
    if history:
        body["history"] = [{"q": text, "a": "Please use secure entry"}]
    else:
        body["question"] = text
    r = s.client.post("/api/v1/assistant/ask", json=body)
    assert r.status_code == 200 and "not stored" in r.json()["answer"]
    assert s.model_calls == [] and audit_calls == [] and s.plans == [] and s.data_reads == []


def test_account_context_projects_only_safe_live_offers(monkeypatch):
    class Connection:
        def execute(self, *_):
            return SimpleNamespace(fetchone=lambda: {"id": "org", "name": "This organisation", "products": ["commai"]})

    monkeypatch.setattr(
        hub.plans,
        "subscriptions",
        lambda *_: [
            {"product": "commai", "plan": "Jibsy", "state": "active", "private_rate": "never copy"},
            {"product": "connect", "plan": "Example", "state": "active", "plan_example": True},
        ],
    )
    monkeypatch.setattr(
        hub.catalogue,
        "list_items",
        lambda *_: [
            {
                "code": "live",
                "name": "Verified offer",
                "product": "connect",
                "description": "A real offer",
                "price_text": "Quote required",
                "internal_margin": "never copy",
            },
            {"code": "sample", "example": True},
            {"code": "sample-price", "price": {"example": True}},
        ],
    )
    user = User("p", "p@example.invalid", "customer", "org", None, org_role="member", apps=("commai",))
    out = hub.context(Connection(), "org", user, connect_read=False, connect_write=False)
    assert [x["code"] for x in out["catalogue"]] == ["live"]
    assert [x["plan"] for x in out["account"]["subscriptions"]] == ["Jibsy"]
    assert "never copy" not in json.dumps(out)
    assert out["upgrade_guidance"]["can_request"] is False
