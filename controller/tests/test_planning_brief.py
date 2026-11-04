"""Offline brief validation; no database fixture, provider or credentials."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from exaconnect_controller.ai.brief import PlanningBrief
from exaconnect_controller.api import ai


def brief(**changes):
    return {
        "format": "xakore-planning-brief",
        "version": 1,
        "offer": "Connect",
        "facts": [{"label": "Sites", "value": "Twenty shops"}],
        "gaps": ["Independent backup connection?"],
        **changes,
    }


def test_imported_requirements_remain_separate_from_account_evidence():
    value = PlanningBrief.model_validate(brief())
    context = value.context()
    assert context["verified_account_facts"] is False
    assert context["source"] == "visitor_reviewed_website_notes"
    assert "customer_id" not in context and "permissions" not in context
    assert context["facts"] == [{"label": "Sites", "value": "Twenty shops"}]


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"offer": "Invented enterprise plan"},
        {"facts": [], "gaps": []},
        {"facts": [{"label": "Sites", "value": "x"}] * 25},
        {"facts": [{"label": "x" * 81, "value": "x"}]},
        {"facts": [{"label": "Sites", "value": "x" * 501}]},
        {"facts": [{"label": "Sites", "value": 20}]},
        {"facts": [{"label": "Sites", "value": "x", "verified": True}]},
        {"facts": [{"label": "Sites", "value": "bad\x00text"}]},
        {"facts": [{"label": "Sites", "value": "\ud800"}]},
        {"gaps": ["x"] * 7},
        {"gaps": ["x" * 201]},
        {"customer_id": "foreign"},
        {"actions": [{"action": "storm_mode", "on": True}]},
    ],
)
def test_invalid_and_authority_bearing_briefs_are_rejected(changes):
    with pytest.raises(ValidationError):
        PlanningBrief.model_validate(brief(**changes))


def test_24_notes_fit_but_multibyte_oversize_is_rejected():
    assert len(PlanningBrief.model_validate(brief(facts=[{"label": "Note", "value": "x" * 500}] * 24)).facts) == 24
    with pytest.raises(ValidationError, match="too large"):
        PlanningBrief.model_validate(brief(facts=[{"label": "Note", "value": "界" * 500}] * 24))


@pytest.mark.parametrize(
    "note",
    [
        {"label": "Password", "value": "hunter-two"},
        {"label": "Account", "value": "4111 1111 1111 1111"},
        {"label": "Private key", "value": "private_key=never-share"},
    ],
)
def test_sensitive_import_stops_before_database_or_provider(monkeypatch, note):
    monkeypatch.setattr(ai, "_customer", lambda *_: "fictional-organisation")
    monkeypatch.setattr(ai, "_network_access", lambda *_: (True, True))

    def forbidden(*_, **__):
        pytest.fail("Sensitive notes reached a database or provider")

    monkeypatch.setattr(ai.db, "tx", forbidden)
    monkeypatch.setattr(ai.ask_mod, "chat", forbidden)
    body = ai.AskIn(question="Review my planning notes", brief=brief(facts=[note]))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace())))
    out = asyncio.run(ai._answer(body, SimpleNamespace(role="customer"), request, shared_mode=True))
    assert out["model"] == "rules" and out["plan"] is None


def test_existing_questions_need_no_brief():
    assert ai.AskIn(question="Check my sites").brief is None


def test_questions_only_brief_is_supported_without_inventing_facts():
    value = PlanningBrief.model_validate(brief(facts=[], gaps=["Which outcome matters most?"]))
    assert value.context()["facts"] == []
    assert value.context()["verified_account_facts"] is False
