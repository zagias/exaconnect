"""Regressions from the rejected, fictional paid-validation advice samples.

No model/provider request or real database is used. Shared API tests exercise
the actual parser/rendering boundary, not only prompt instructions.
"""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exaconnect_controller.ai import act, guidance, hub
from exaconnect_controller.api import ai
from exaconnect_controller.api.deps import User, current_user

BAD_ACCOUNT = (
    "Jibsy diagnostics and approved repairs are accessible via the assistant's Jibsy scope, "
    "but no Jibsy channels or integrations are configured in this snapshot. "
    "All features are purchased via the Connect product subscription."
)
BAD_DESIGN = (
    "Should VoIP be pinned to the lowest-latency link and POS to the most reliable? "
    "Storm Mode uses configured backup capacity (not satellite). LEO is ordered through partners. "
    "Do you need port forwards for POS terminals or payment gateways?"
)
NETWORK = {
    "sites": [
        {"name": f"fictional-{i:02d}", "kind": "site", "paths": [], "last_seen": None, "online": None}
        for i in range(1, 21)
    ],
    "customer": {"shadow_mode": True},
}
RECORDED_DESIGN = json.loads(Path(__file__).with_name("hub_advice_replay.json").read_text())


@pytest.fixture
def account_context(monkeypatch):
    state = SimpleNamespace(products=["connect"], subscriptions=[], offers=[], example=False)
    user = User(
        "person",
        "person@example.invalid",
        "customer",
        "org",
        None,
        org_role="owner",
        products=("connect",),
        apps=("connect",),
        scopes=("connect",),
    )

    class Connection:
        def execute(self, *_):
            return SimpleNamespace(
                fetchone=lambda: {
                    "id": "org",
                    "name": "Fictional retailer",
                    "products": state.products,
                    "example": state.example,
                }
            )

    monkeypatch.setattr(hub.plans, "subscriptions", lambda *_: state.subscriptions)
    monkeypatch.setattr(hub.catalogue, "list_items", lambda *_: state.offers)

    def make(**kwargs):
        return hub.context(Connection(), "org", user, connect_read=True, connect_write=True, **kwargs)

    state.make = make
    state.user = user
    return state


def test_feature_evidence_separates_access_subscription_permissions_and_readiness(account_context):
    out = account_context.make()
    connect = out["account"]["product_access"]["connect"]
    assert connect["member_access"] is True
    assert connect["active_subscription_recorded"] is False
    assert connect["payment_verified"] is False and connect["provider_readiness"] == "not_checked"
    assert out["account"]["subscription_evidence"]["active_recorded"] is False
    features = {feature["id"]: feature for feature in out["verified_features"]}
    assert features["network_health"]["availability"]["permitted_now"] is True
    assert features["jibsy_diagnostics"]["availability"]["permitted_now"] is False
    assert features["jibsy_diagnostics"]["path"] is None
    assert out["tools"]["jibsy_diagnostics_path"] is None
    assert out["catalogue_evidence"]["verified_live_offer_count"] == 0
    assert out["design_constraints"]["storm_mode"]["uses_configured_satellite"] is True
    assert out["design_constraints"]["steering"]["assistant_can_pin_path"] is False


def test_scheduled_subscription_is_not_active_or_payment_evidence(account_context):
    account_context.subscriptions = [{"product": "connect", "plan": "Scheduled", "state": "scheduled"}]
    out = account_context.make()
    assert out["account"]["product_access"]["connect"]["scheduled_subscription_recorded"] is True
    assert out["account"]["subscription_evidence"]["active_recorded"] is False
    assert "No active subscription is recorded" in " ".join(guidance.account_facts(out, [], []))
    account_context.subscriptions[0]["state"] = "active"
    out = account_context.make()
    assert out["account"]["subscription_evidence"]["active_recorded"] is True
    assert "Payment hasn't been checked" in " ".join(guidance.account_facts(out, [], []))


def test_missing_member_app_is_an_access_issue_not_another_purchase(account_context):
    account_context.products = ["connect", "commai"]
    out = account_context.make(jibsy_diagnostics=True)
    assert out["account"]["product_access"]["commai"]["organisation_access"] is True
    assert out["account"]["product_access"]["commai"]["member_access"] is False
    assert out["tools"]["jibsy_diagnostics_path"] is None
    assert "an owner or admin can give you app access" in " ".join(guidance.account_facts(out, [], []))


def test_offer_is_not_supply_and_admin_access_is_not_tenant_entitlement(account_context):
    account_context.offers = [{"code": "real-offer", "name": "Verified service", "price_text": "Quote required"}]
    out = account_context.make()
    facts = " ".join(guidance.account_facts(out, [], ["real-offer"]))
    assert "doesn't confirm coverage, supply or fulfilment" in facts
    account_context.products = []
    account_context.user.role = "admin"
    out = account_context.make()
    assert out["account"]["product_access"]["connect"]["operator_access"] is True
    assert out["account"]["product_access"]["connect"]["organisation_access"] is False
    assert "admin role lets you review Connect" in " ".join(guidance.account_facts(out, [], []))


def test_stale_agent_or_configured_paths_do_not_establish_operational_readiness():
    context = {"sites": [{"paths": [], "last_seen": "2020-01-01", "online": False}]}
    assert "doesn't prove working connectivity" in guidance.readiness_note(context)[0]
    context["sites"][0]["paths"] = [{"path": "carrier-a"}]
    assert "no agents reporting" in guidance.readiness_note(context)[0]


@pytest.fixture
def shared_grounding(monkeypatch, account_context):
    state = SimpleNamespace(response={}, prompts=[], plans=[], reads=[], context=account_context.make())
    conn = SimpleNamespace(
        execute=lambda *_: SimpleNamespace(
            fetchone=lambda: {
                "n": 0,
                "name": "Fictional retailer",
                "example": state.context["account"].get("example", False),
            }
        )
    )
    monkeypatch.setattr(ai.db, "tx", lambda: nullcontext(conn))
    monkeypatch.setattr(ai.audit, "record", lambda *_: None)
    monkeypatch.setattr(ai, "_hub_context", lambda *_: copy.deepcopy(state.context))
    monkeypatch.setattr(ai.ask_mod, "build_context", lambda *_: state.reads.append("network") or copy.deepcopy(NETWORK))
    monkeypatch.setattr(ai.act, "config", lambda *_: {})

    def chat(system, prompt, **_):
        state.prompts.append((system, prompt))
        return state.response if isinstance(state.response, str) else json.dumps(state.response)

    def create(_conn, _customer, _question, answer, actions, *_args, **_kwargs):
        state.plans.append({"answer": answer, "actions": actions})
        return {
            "id": 17,
            "status": "draft",
            "approval": None,
            "can_execute": False,
            "actions": actions,
            "summary": ["Switch Storm Mode on at fictional-01, fictional-02."],
        }

    monkeypatch.setattr(ai.ask_mod, "chat", chat)
    monkeypatch.setattr(ai.act, "create", create)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        llm_api_key="test", llm_base_url="unused", llm_model="test", llm_questions_per_hour=30
    )
    app.dependency_overrides[current_user] = lambda: account_context.user
    app.include_router(ai.shared, prefix="/api/v1")
    with TestClient(app) as client:
        state.client = client
        yield state


def ask(state, scope="connect"):
    response = state.client.post("/api/v1/assistant/ask", json={"scope": scope, "question": "Review this design."})
    assert response.status_code == 200, response.text
    return response.json()


def test_rejected_account_sample_cannot_claim_purchase_or_jibsy_access(shared_grounding):
    s = shared_grounding
    s.response = {"answer": BAD_ACCOUNT, "actions": []}
    result = ask(s, "account")
    assert "You have Connect access" in result["answer"]
    assert "Jibsy isn't enabled" in result["answer"]
    assert "No active subscription is recorded" in result["answer"]
    assert "haven't checked" in result["answer"]
    assert "are purchased" not in result["answer"] and "none configured" not in result["answer"]
    assert "isn't a verified offer or price" in result["answer"]
    assert result["plan"] is None and s.reads == [] and s.plans == []


def test_shared_prompt_has_one_selector_contract_and_preserves_typed_action_catalogue(shared_grounding):
    s = shared_grounding
    s.response = {"answer": "", "actions": [], "advice": {"kind": "account", "account_topics": ["access"]}}
    assert ask(s, "account")["based_on"]["grounding"] == "verified_advisory_selection"
    system = s.prompts[-1][0]
    assert system.startswith(hub.SYSTEM)
    assert system.endswith(act.ACTION_SYSTEM)
    assert '"advice":{"kind":"account"' in system
    assert '"advice":{"kind":"design"' in system
    assert '"actions": [ ... ]' not in system
    assert "Lead with the answer, then the evidence" not in system
    assert "with the answer citing" not in system
    assert act.SYSTEM.endswith(act.ACTION_SYSTEM)
    for action in act.ACTION_FIELDS:
        assert '"action":"' + action + '"' in act.ACTION_SYSTEM


@pytest.mark.parametrize("kind", [None, "analysis", "proposal", "design"])
def test_rejected_design_sample_and_kind_escape_emit_only_verified_facts(shared_grounding, kind):
    s = shared_grounding
    s.response = {"answer": BAD_DESIGN, "actions": []}
    if kind:
        s.response["advice"] = {"kind": kind}
    result = ask(s)
    assert "not satellite" not in result["answer"]
    assert "LEO is ordered" not in result["answer"]
    assert "Do you need port forwards" not in result["answer"]
    assert "can't pin" in result["answer"]
    assert "configured satellite paths warm" in result["answer"]
    assert "required outbound connections" in result["answer"]
    assert "Treat this as a proposed design" in result["answer"]
    assert result["plan"] is None


@pytest.mark.parametrize("kind", ["proposal", "analysis", None])
def test_exact_reviewed_proposal_survives_without_unverified_capability_prose(shared_grounding, kind):
    s = shared_grounding
    actions = [{"action": "storm_mode", "on": True, "sites": ["fictional-01", "fictional-02"]}]
    s.response = {"answer": BAD_DESIGN, "actions": actions}
    if kind:
        s.response["advice"] = {"kind": kind}
    result = ask(s)
    assert result["plan"]["id"] == 17 and result["plan"]["actions"] == actions
    assert result["plan"]["approval"] is None and result["plan"]["can_execute"] is False
    assert "Switch Storm Mode on at fictional-01, fictional-02." in result["answer"]
    assert "Nothing has been applied" in result["answer"]
    assert "not satellite" not in result["answer"] and "LEO is ordered" not in result["answer"]
    assert "not satellite" not in s.plans[0]["answer"]


def test_model_adapts_followups_without_a_fixed_questionnaire(shared_grounding):
    s = shared_grounding
    s.response = {
        "answer": BAD_DESIGN,
        "actions": [],
        "advice": {"kind": "design", "controls": ["pos_sessions"], "questions": ["voice_failover", "diversity"]},
    }
    result = ask(s)
    assert "payment sessions" in result["answer"]
    assert result["answer"].index("hosted or branch-based") < result["answer"].index("last-mile routes")
    assert "backup power" not in result["answer"]
    s.response["advice"]["questions"] = []
    result = ask(s)
    assert "?" not in result["answer"]


def test_analysis_selects_real_evidence_not_model_rationale(shared_grounding, monkeypatch):
    s = shared_grounding
    network = {
        **NETWORK,
        "routing_decisions_last_7_days_newest_first": [
            {
                "time": "2026-11-02 00:00:00Z",
                "site": "fictional-01",
                "class_name": "voice",
                "shadow": True,
                "reason": "Loss forecast 1.3% against a 1% SLA.",
            }
        ],
    }
    monkeypatch.setattr(ai.ask_mod, "build_context", lambda *_: copy.deepcopy(network))
    s.response = {
        "answer": BAD_DESIGN,
        "actions": [],
        "advice": {"kind": "analysis", "evidence_ids": ["decision:0"], "questions": ["failure_validation"]},
    }
    result = ask(s)
    assert "Shadow decision" in result["answer"] and "Loss forecast 1.3%" in result["answer"]
    assert "not satellite" not in result["answer"] and "LEO is ordered" not in result["answer"]
    s.response["advice"]["evidence_ids"] = ["decision:999"]
    assert "Loss forecast 1.3%" not in ask(s)["answer"]


def test_unknown_controls_offers_and_custom_prose_fail_closed(shared_grounding):
    s = shared_grounding
    for field, values in (
        ("controls", ["pin_path"]),
        ("offer_codes", ["buy-leo"]),
        ("questions", ["open-pos-inbound"]),
        ("rationale", "Invented feature"),
    ):
        s.response = {"answer": BAD_DESIGN, "actions": [], "advice": {"kind": "design", field: values}}
        assert "unverified_advice_replaced" in ask(s)["based_on"]["grounding"]


def test_example_label_is_server_rendered_for_account_design_and_proposal(shared_grounding):
    s = shared_grounding
    s.context["account"]["example"] = True
    for scope, response in (
        ("account", {"advice": {"kind": "account"}}),
        ("connect", {"advice": {"kind": "design", "controls": ["commissioning"]}}),
        ("connect", {"actions": [{"action": "storm_mode", "on": True, "sites": ["fictional-01"]}]}),
    ):
        s.response = {"answer": "", "actions": [], **response}
        assert ask(s, scope)["answer"].startswith("Example data.")


def test_replies_are_brief_and_account_topics_avoid_repeated_boilerplate(shared_grounding):
    s = shared_grounding
    s.response = {
        "answer": BAD_ACCOUNT,
        "actions": [],
        "advice": {"kind": "account", "account_topics": ["access"], "feature_ids": ["network_health", "network_plans"]},
    }
    result = ask(s, "account")
    assert len(result["answer"].split()) <= 140
    assert "/account/apps" not in result["answer"] and "/quotes" not in result["answer"]
    assert "Apps & plans" in result["answer"] and "Quotes" in result["answer"]
    assert "subscription" not in result["answer"].lower() and "haven't checked" not in result["answer"]
    s.response["advice"] = {
        "kind": "design",
        "controls": ["application_sla", "storm_mode", "pos_security"],
        "observations": ["inventory", "satellite"],
    }
    assert len(ask(s)["answer"].split()) <= 140


def test_recorded_valid_six_control_response_keeps_grounded_priority_instead_of_fallback(shared_grounding):
    s = shared_grounding
    s.response = RECORDED_DESIGN["content"]
    response = s.client.post(
        "/api/v1/assistant/ask", json={"scope": "connect", "question": RECORDED_DESIGN["question"]}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["based_on"]["grounding"] == "verified_advisory_selection"
    assert result["based_on"]["advice_selection"] == {
        "normalised": True,
        "changes": [{"field": "controls", "supplied": 6, "selected": 3}],
    }
    answer = result["answer"]
    for control in ("diversity", "power", "application_sla"):
        assert guidance.DESIGN[control]["advice"] in answer
    for control in ("pos_security", "storm_mode", "satellite_costs"):
        assert guidance.DESIGN[control]["advice"] not in answer
    questions = ["pos_sessions", "voice_failover", "commissioning", "failure_validation"]
    assert [answer.index(guidance.DESIGN[key]["question"]) for key in questions] == sorted(
        answer.index(guidance.DESIGN[key]["question"]) for key in questions
    )
    assert "0 configured paths" in answer and "0 configured satellite paths" in answer
    assert result["plan"] is None and s.plans == []


@pytest.mark.parametrize("name", guidance.SELECTION_LIMITS)
def test_every_selector_validates_unknown_tail_before_bounding(name):
    allowed = {f"id-{index}" for index in range(guidance.SELECTION_LIMITS[name] + 2)}
    values = sorted(allowed) + ["unknown-id"]
    with pytest.raises(ValueError, match="unverified selection"):
        guidance._selection({name: values}, name, allowed)


def test_duplicate_valid_choices_are_bounded_without_reordering():
    advice = {"controls": ["power", "power", "diversity", "pos_sessions", "storm_mode"]}
    assert guidance._selection(advice, "controls", guidance.DESIGN) == ["power", "diversity", "pos_sessions"]
    assert guidance.selection_metadata(json.dumps({"advice": advice})) == {
        "normalised": True,
        "changes": [{"field": "controls", "supplied": 5, "selected": 3}],
    }


@pytest.mark.parametrize("bad", ["power", [None], [{"endpoint": "/arbitrary"}], ["power"] * 65])
def test_malformed_or_oversized_choices_keep_safe_fallback(shared_grounding, bad):
    s = shared_grounding
    s.response = {"answer": BAD_DESIGN, "actions": [], "advice": {"kind": "design", "controls": bad}}
    result = ask(s)
    assert result["based_on"]["grounding"] == "unverified_advice_replaced_with_grounded_facts"
    assert "advice_selection" not in result["based_on"]
    assert "not satellite" not in result["answer"] and result["plan"] is None


def test_unknown_control_after_valid_display_limit_rejects_whole_advice(shared_grounding):
    s = shared_grounding
    s.response = json.loads(RECORDED_DESIGN["content"])
    s.response["advice"]["controls"].append("pin_path")
    result = ask(s)
    assert result["based_on"]["grounding"] == "unverified_advice_replaced_with_grounded_facts"
    assert "advice_selection" not in result["based_on"]
    assert result["plan"] is None and s.plans == []


def test_bounded_account_features_do_not_grant_jibsy_or_claim_a_purchase(shared_grounding):
    s = shared_grounding
    s.response = {
        "answer": BAD_ACCOUNT,
        "actions": [],
        "advice": {
            "kind": "account",
            "feature_ids": ["jibsy_diagnostics", "network_health", "network_plans", "network_orders"],
            "account_topics": ["access", "subscriptions"],
        },
    }
    result = ask(s, "account")
    assert result["based_on"]["grounding"] == "verified_advisory_selection"
    assert "isn't available with your access and permissions" in result["answer"]
    assert "Jibsy isn't enabled" in result["answer"] and "No active subscription is recorded" in result["answer"]
    assert result["based_on"]["advice_selection"]["changes"] == [{"field": "feature_ids", "supplied": 4, "selected": 2}]
    assert s.reads == [] and s.plans == []


def test_advice_with_valid_extras_cannot_smuggle_a_configuration_action(shared_grounding):
    s = shared_grounding
    s.response = json.loads(RECORDED_DESIGN["content"])
    s.response["actions"] = [{"action": "storm_mode", "on": True, "sites": ["fictional-01"]}]
    result = ask(s)
    assert result["based_on"]["grounding"] == "verified_advisory_selection"
    assert result["plan"] is None and s.plans == []


def test_bounded_choices_do_not_bypass_account_scope_or_customer_access(shared_grounding):
    s = shared_grounding
    s.response = RECORDED_DESIGN["content"]
    assert ask(s, "account")["based_on"]["grounding"] == "unverified_advice_replaced_with_grounded_facts"
    s.prompts.clear()
    response = s.client.post(
        "/api/v1/assistant/ask",
        json={"scope": "connect", "question": "Review this design.", "customer_id": "another-customer"},
    )
    assert response.status_code == 403 and s.prompts == []


@pytest.mark.parametrize("scope", ["account", "connect"])
@pytest.mark.parametrize("question", ["check the status of my sites?", "Are my sites online?"])
def test_site_status_reads_without_model_or_actions(shared_grounding, monkeypatch, scope, question):
    s = shared_grounding
    s.context["account"]["example"] = True
    monkeypatch.setattr(ai.act, "config", lambda *_: pytest.fail("Status must not read action configuration"))
    s.client.app.state.settings.llm_api_key = ""
    response = s.client.post("/api/v1/assistant/ask", json={"question": question, "scope": scope})
    assert response.status_code == 200
    result = response.json()
    assert result["model"] == "rules" and result["plan"] is None and result["answer_scope"] == "connect"
    assert "scope" not in result  # Older portal tabs spread replies into their originating turn.
    assert result["answer"].startswith("Example data.")
    assert "20 sites" in result["answer"] and "20 with no report recorded" in result["answer"]
    assert "0 of 0 configured paths" in result["answer"] and "Connectivity is unverified" in result["answer"]
    assert "15 more sites" in result["answer"] and "fictional-06:" not in result["answer"]
    assert result["based_on"]["sources"] == ["network_snapshot"]
    assert s.reads == ["network"] and s.prompts == [] and s.plans == []


def test_restore_clarifies_without_model_snapshot_or_plan(shared_grounding):
    s = shared_grounding
    s.client.app.state.settings.llm_api_key = ""
    response = s.client.post(
        "/api/v1/assistant/ask", json={"question": "how do i restore my sites?", "scope": "account"}
    )
    assert response.status_code == 200
    result = response.json()
    assert "getting disconnected sites reporting again" in result["answer"]
    assert "recovering deleted site records or configuration" in result["answer"]
    assert result["plan"] is None and result["model"] == "rules"
    assert s.reads == [] and s.prompts == [] and s.plans == []


@pytest.mark.parametrize("other_tenant", [False, True])
def test_site_status_keeps_access_and_tenant_gates(shared_grounding, account_context, other_tenant):
    s = shared_grounding
    body = {"question": "Check the status of my sites", "scope": "account"}
    if other_tenant:
        body["customer_id"] = "other-org"
    else:
        account_context.user.products = ("commai",)
        account_context.user.apps = ("commai",)
        account_context.user.scopes = ("commai:read",)
    assert s.client.post("/api/v1/assistant/ask", json=body).status_code == 403
    assert s.reads == [] and s.prompts == [] and s.plans == []


@pytest.mark.parametrize(
    "question",
    [
        "Delete my sites",
        "Reset all site configuration",
        "What does our plan include?",
        "Where is voice now?",
        "Why did voice move?",
        "Why were my sites offline yesterday?",
        "What is the voice service status at my sites?",
        "Show site health last week",
        "Restore voice service at my sites",
        "Set my site status to offline",
        "How do I keep my sites online?",
        "Can my sites stay online if a carrier fails?",
        "Will our sites be online tomorrow?",
    ],
)
def test_site_shortcut_does_not_capture_changes_or_historical_diagnosis(question):
    assert guidance.site_intent(question) is None


def test_site_status_distinguishes_reports_from_connectivity():
    answer = guidance.site_status_answer(
        {
            "now_utc": "2026-11-03T12:00:00+00:00",
            "sites": [
                {"name": "Reporting", "last_seen": "2026-11-03T12:00:00Z", "online": True, "paths": []},
                {
                    "name": "Stale",
                    "last_seen": "2020-01-01T00:00:00Z",
                    "online": False,
                    "paths": [{"path": "carrier-a"}],
                },
                {"name": "Never", "last_seen": None, "online": None, "paths": []},
            ],
        }
    )
    assert "1 reporting within 30 seconds" in answer and "1 with older reports" in answer
    assert "1 with no report recorded" in answer and "0 of 1 configured paths" in answer
    assert "Never: no agent report recorded" in answer and "Connectivity is unverified" in answer
    assert "site down" not in answer.lower() and "healthy" not in answer.lower()
    assert "03 Nov 2026 at 12:00 UTC" in answer
