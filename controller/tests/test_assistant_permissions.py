"""Ask must not turn a proposed change into an organisation-role bypass."""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exaconnect_controller.api import ai
from exaconnect_controller.api import settings as settings_api
from exaconnect_controller.api.deps import User, current_user


@pytest.fixture
def assistant(monkeypatch):
    """Exercise real API permission checks without a model service or database."""
    user = User("person", "member@example.invalid", "customer", "org", None, org_role="member")
    state = SimpleNamespace(
        user=user,
        actions=[{"action": "shadow_mode", "on": True}],
        writes=[],
        dry_runs=[],
        prompts=[],
        plan={
            "id": 1,
            "customer_id": "org",
            "status": "draft",
            "actions": [{"action": "shadow_mode", "on": True}],
            "results": [],
        },
    )

    class Result:
        def fetchone(self):
            return {"n": 0}

        def fetchall(self):
            return [copy.deepcopy(state.plan)]

    class Connection:
        def execute(self, *_):
            return Result()

        def transaction(self):
            return nullcontext()

    monkeypatch.setattr(ai.db, "tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(ai.audit, "record", lambda *_: None)
    monkeypatch.setattr(ai.ask_mod, "build_context", lambda *_: {"sites": []})
    monkeypatch.setattr(ai.act, "config", lambda *_: {})

    def chat(_system, prompt, **_):
        state.prompts.append(prompt)
        return json.dumps({"answer": "Here is the network information.", "actions": state.actions})

    def view(_conn, plan, dry_run=True):
        state.dry_runs.append(dry_run)
        return {**copy.deepcopy(plan), "summary": [], "problems": []}

    def create(conn, cid, question, answer, actions, actor, *, dry_run=True):
        state.plan.update(customer_id=cid, actions=actions, status="draft")
        return view(conn, state.plan, dry_run=dry_run)

    def apply(_conn, plan, _actor):
        state.writes.append("apply")
        state.plan["status"] = "applied"

    def undo(_conn, plan, _actor):
        state.writes.append("undo")
        state.plan["status"] = "undone"

    monkeypatch.setattr(ai.ask_mod, "chat", chat)
    monkeypatch.setattr(ai.act, "get", lambda *_args, **_kwargs: copy.deepcopy(state.plan))
    monkeypatch.setattr(ai.act, "view", view)
    monkeypatch.setattr(ai.act, "create", create)
    monkeypatch.setattr(ai.act, "apply", apply)
    monkeypatch.setattr(ai.act, "undo", undo)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        llm_api_key="local-test-value", llm_model="local-test-model", llm_base_url="unused", llm_questions_per_hour=30
    )
    app.dependency_overrides[current_user] = lambda: state.user
    app.include_router(ai.router, prefix="/api/v1")
    app.include_router(settings_api.router, prefix="/api/v1")
    with TestClient(app) as client:
        state.client = client
        yield state


@pytest.mark.parametrize("step,status", [("apply", "draft"), ("undo", "applied")])
def test_member_cannot_change_shadow_mode_through_a_plan(assistant, step, status):
    direct = assistant.client.patch("/api/v1/customers/org/settings", json={"shadow_mode": True})
    assert direct.status_code == 403
    assistant.plan["status"] = status
    response = assistant.client.post(f"/api/v1/ai/plans/1/{step}")
    assert response.status_code == 403
    assert response.json()["detail"] == direct.json()["detail"]
    assert assistant.writes == []


@pytest.mark.parametrize("role,org_role", [("customer", "owner"), ("customer", "admin"), ("admin", None)])
def test_managers_and_staff_can_apply_and_undo_shadow_mode(assistant, role, org_role):
    assistant.user.role = role
    assistant.user.org_role = org_role
    assert assistant.client.post("/api/v1/ai/plans/1/apply").status_code == 200
    assert assistant.client.post("/api/v1/ai/plans/1/undo").status_code == 200
    assert assistant.writes == ["apply", "undo"]


def test_member_keeps_ordinary_network_action_permissions(assistant):
    assistant.plan["actions"] = [{"action": "storm_mode", "on": True, "sites": ["site-a"]}]
    assert assistant.client.post("/api/v1/ai/plans/1/apply").status_code == 200
    assert assistant.client.post("/api/v1/ai/plans/1/undo").status_code == 200
    assert assistant.writes == ["apply", "undo"]


def test_permissions_are_rechecked_after_a_role_change(assistant):
    assistant.user.org_role = "owner"
    assert assistant.client.post("/api/v1/ai/plans/1/apply").status_code == 200
    assistant.user.org_role = "member"
    assert assistant.client.post("/api/v1/ai/plans/1/undo").status_code == 403
    assert assistant.writes == ["apply"]


@pytest.mark.parametrize("org_role", ["member", "viewer"])
def test_read_only_questions_remain_available(assistant, org_role):
    assistant.user.org_role = org_role
    assistant.actions = []
    response = assistant.client.post("/api/v1/ai/ask", json={"question": "Where is voice now?"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Here is the network information."
    assert response.json()["plan"] is None
    assert assistant.writes == []


def test_unauthorised_model_action_is_shown_as_blocked_without_a_dry_run(assistant):
    response = assistant.client.post("/api/v1/ai/ask", json={"question": "Turn on shadow mode"})
    assert response.status_code == 200
    assert "owners and admins" in response.json()["plan"]["problems"][0]
    assert assistant.dry_runs == [False]
    assert '"can_change_shadow_mode":false' in assistant.prompts[0]
    assert assistant.writes == []


@pytest.mark.parametrize("path", ["/api/v1/ai/plans/1", "/api/v1/customers/org/assistant/plans"])
def test_existing_plans_show_current_user_permission_problems(assistant, path):
    response = assistant.client.get(path)
    assert response.status_code == 200
    plan = response.json()[0] if isinstance(response.json(), list) else response.json()
    assert "owners and admins" in plan["problems"][0]
    assert assistant.dry_runs == [False]


def test_a_manager_still_cannot_act_on_another_organisation(assistant):
    assistant.user.org_role = "owner"
    assistant.plan["customer_id"] = "other-org"
    assert assistant.client.post("/api/v1/ai/plans/1/apply").status_code == 403
    assert assistant.writes == []
