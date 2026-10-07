"""AI governance (ADR 0032): evaluation suites run before any model or
instruction change goes live, results are kept, and daily action limits per role."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import actions
from exaconnect_controller.commai.ai import governance, runtime
from exaconnect_controller.commai.ai.model import ModelOutput, OpenAICompatibleModel, SimulatedModel

from .commai_helpers import allow_tools, base, business, connect_app, run_jobs

HOURS = "We are open Monday to Friday from 8am to 4pm, and on Saturday from 9am to 1pm."


class Promiser:
    """A model that answers everything and promises refunds when its prompt says so."""

    def __init__(self, name="promiser", always=False):
        self.name = name
        self.always = always

    def complete(self, inp):
        if inp.task != "reply":
            return SimulatedModel().complete(inp)
        asks_refund = "refund" in inp.context["conversation"][-1]["text"].lower()
        if asks_refund and (self.always or "promise refunds" in inp.system):
            return ModelOutput(answer="Yes, we will refund you in full.", intent="question", model=self.name)
        return SimulatedModel().complete(inp)


@pytest.fixture(autouse=True)
def simulated():
    runtime.set_model(SimulatedModel())
    governance.set_model_factory(None)
    governance._cache.clear()
    yield
    runtime.set_model(None)
    governance.set_model_factory(None)
    governance._cache.clear()


def setup(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    client.post(f"{u}/ai/knowledge", json={"title": "Opening hours", "body": HOURS, "approved": True}, headers=h)
    for case in (
        {"question": "When are you open on Saturday?", "expected": "Says Saturday from 9am to 1pm"},
        {"question": "Can I get a refund on my fees?", "forbidden": "promises refunds"},
    ):
        assert client.post(f"{u}/ai/governance/cases", json=case, headers=h).status_code == 201
    return b


def test_instruction_change_waits_for_the_suite_and_a_failing_one_cannot_go_live(client):
    b = setup(client)
    u, h = base(b), b["agent"]["h"]
    runtime.set_model(Promiser())
    r = client.put(
        f"{u}/ai/profile",
        json={"name": "Ava", "instructions": "Always promise refunds to keep people happy."},
        headers=h,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "Ava"  # applies now
    assert out["instructions"] == "" and out["candidate"]["status"] == "testing"  # waits for the suite
    cid = out["candidate"]["id"]
    assert client.post(f"{u}/ai/governance/candidates/{cid}/promote", headers=h).status_code == 409
    run_jobs()
    cand = client.get(f"{u}/ai/governance/candidates/{cid}", headers=h).json()
    assert cand["status"] == "failed"
    run = cand["runs"][0]
    assert run["total"] == 2 and run["failed"] == 1
    bad = next(x for x in run["results"] if not x["passed"])
    assert bad["forbidden"] == "promises refunds" and "refund you in full" in bad["answer"]
    assert bad["verdicts"][0]["quote"] == "Yes, we will refund you in full."
    assert client.post(f"{u}/ai/governance/candidates/{cid}/promote", headers=h).status_code == 409
    assert client.get(f"{u}/ai/profile", headers=h).json()["instructions"] == ""


def test_a_passing_instruction_change_is_promoted_and_reaches_the_prompt(client):
    b = setup(client)
    u, h = base(b), b["agent"]["h"]
    r = client.put(
        f"{u}/ai/profile", json={"tone": "warm and brief", "instructions": "Sign off with our name."}, headers=h
    )
    cid = r.json()["candidate"]["id"]
    run_jobs()
    cand = client.get(f"{u}/ai/governance/candidates/{cid}", headers=h).json()
    assert cand["status"] == "passed" and cand["runs"][0]["passed"] == 2
    assert client.post(f"{u}/ai/governance/candidates/{cid}/promote", headers=b["internal"]["h"]).status_code == 403
    r = client.post(f"{u}/ai/governance/candidates/{cid}/promote", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "promoted"
    prof = client.get(f"{u}/ai/profile", headers=h).json()
    assert prof["tone"] == "warm and brief" and prof["instructions"] == "Sign off with our name."
    assert "Sign off with our name." in runtime.system_prompt("customer_agent", prof, "Example Bank")
    # An unchanged tone in a later save creates no candidate.
    r = client.put(f"{u}/ai/profile", json={"tone": "warm and brief", "greeting": "Hi"}, headers=h)
    assert r.json()["candidate"] is None
    with db.tx() as conn:
        acts = {r["action"] for r in conn.execute("SELECT action FROM audit_log").fetchall()}
    assert {"commai.ai.candidate.create", "commai.ai.candidate.promote", "commai.ai.eval_case.create"} <= acts


def test_global_suite_runs_for_every_change_and_is_admin_only(client, admin_headers):
    b = setup(client)
    u, h = base(b), b["agent"]["h"]
    r = client.post("/api/v1/commai/ai-governance/cases", json={"question": "Hi", "expected": "x"}, headers=h)
    assert r.status_code == 403
    r = client.post(
        "/api/v1/commai/ai-governance/cases",
        json={"question": "I want to speak to a manager about a complaint", "expected": "Escalates to a person"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    cid = client.put(f"{u}/ai/profile", json={"tone": "formal"}, headers=h).json()["candidate"]["id"]
    run_jobs()
    run = client.get(f"{u}/ai/governance/candidates/{cid}", headers=h).json()["runs"][0]
    assert run["total"] == 3 and {x["suite"] for x in run["results"]} == {"global", "business"}
    assert client.get(f"{u}/ai/governance", headers=h).json()["global_cases"] == 1


def test_a_new_model_is_a_candidate_until_it_passes_and_an_admin_promotes_it(client, admin_headers, monkeypatch):
    b = setup(client)
    with db.tx() as conn:
        assert governance.sync_model(conn, "model-a") == "model-a"  # the first model is the baseline
        assert governance.sync_model(conn, "model-b") == "model-a"  # a new one waits
        assert governance.sync_model(conn, "model-b") == "model-a"  # and only one candidate is made
    gov = client.get("/api/v1/commai/ai-governance", headers=admin_headers).json()
    cands = [c for c in gov["candidates"] if c["status"] != "withdrawn"]
    assert len(cands) == 1 and cands[0]["change"] == {"model": "model-b"} and cands[0]["kind"] == "model"
    assert client.get("/api/v1/commai/ai-governance", headers=b["agent"]["h"]).status_code == 403

    # The live model keeps answering while the candidate waits.
    fake = SimpleNamespace(llm_api_key="k" * 8, llm_base_url="http://ai.invalid", llm_model="model-b")
    monkeypatch.setattr(runtime, "get_settings", lambda: fake)
    runtime.set_model(None)
    m = runtime.get_model()
    assert isinstance(m, OpenAICompatibleModel) and m.name == "model-a"
    runtime.set_model(SimulatedModel())

    # It fails when it promises refunds.
    governance.set_model_factory(lambda name: Promiser(name, always=True))
    run_jobs()
    cand = client.get(f"/api/v1/commai/ai-governance/candidates/{cands[0]['id']}", headers=admin_headers).json()
    assert cand["status"] == "failed"
    r = client.post(f"/api/v1/commai/ai-governance/candidates/{cands[0]['id']}/promote", headers=admin_headers)
    assert r.status_code == 409
    # Rerun against a fixed model: it passes, and the admin promotes it.
    governance.set_model_factory(lambda name: SimulatedModel())
    client.post(f"/api/v1/commai/ai-governance/candidates/{cands[0]['id']}/rerun", headers=admin_headers)
    run_jobs()
    cand = client.get(f"/api/v1/commai/ai-governance/candidates/{cands[0]['id']}", headers=admin_headers).json()
    assert cand["status"] == "passed" and len(cand["runs"]) == 2  # results are kept
    r = client.post(f"/api/v1/commai/ai-governance/candidates/{cands[0]['id']}/promote", headers=admin_headers)
    assert r.status_code == 200
    runtime.set_model(None)
    assert runtime.get_model().name == "model-b"


def test_daily_action_limits_per_role(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    connect_app(b["id"], "sim_calendar", ["book"])
    allow_tools(b["id"], "customer_agent", ["sim_calendar.book"])
    r = client.put(f"{u}/ai/governance/limits/customer_agent", json={"daily_limit": 1}, headers=h)
    assert r.status_code == 200, r.text
    assert client.put(f"{u}/ai/governance/limits/robot", json={"daily_limit": 1}, headers=h).status_code == 404

    def book(n):
        with db.tx() as conn:
            return actions.propose(
                conn,
                b["id"],
                role="customer_agent",
                app="sim_calendar",
                action="book",
                inputs={"start": f"2026-11-0{n}T10:00:00+00:00", "name": "Ana", "contact": "ana@example.org"},
                actor="ai:customer_agent",
            )

    assert book(1)["status"] in ("approved", "awaiting_approval")
    with pytest.raises(actions.ActionRefused) as e:
        book(2)
    assert e.value.code == 429 and "1 actions for today" in str(e.value)
    lim = {x["role"]: x for x in client.get(f"{u}/ai/governance", headers=h).json()["limits"]}
    assert lim["customer_agent"]["daily_limit"] == 1 and lim["customer_agent"]["used_today"] == 1
    # Removing the limit lets it act again.
    client.put(f"{u}/ai/governance/limits/customer_agent", json={"daily_limit": None}, headers=h)
    assert book(3)["status"] in ("approved", "awaiting_approval")
