"""A missing or failed AI service must never become simulated customer work."""

from dataclasses import replace

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import inbox, jobs
from exaconnect_controller.commai.ai import agent, governance, runtime, voice
from exaconnect_controller.commai.ai.model import ModelError, OpenAICompatibleModel, SimulatedModel

from .commai_helpers import base
from .test_commai_ai import ai_business
from .test_commai_channels import Visitor, _widget_key


@pytest.mark.parametrize("flag", [None, "0", "true", "yes"])
def test_registered_job_without_service_or_explicit_test_mode_is_unavailable(monkeypatch, settings, flag):
    monkeypatch.setattr(runtime, "_override", None)
    monkeypatch.setattr(runtime, "get_settings", lambda: replace(settings, llm_api_key=""))
    monkeypatch.setitem(jobs._handlers, "ai.respond", object())
    if flag is None:
        monkeypatch.delenv("EXA_COMMAI_AI_TEST_MODE", raising=False)
    else:
        monkeypatch.setenv("EXA_COMMAI_AI_TEST_MODE", flag)
    assert not runtime.model_ready() and not inbox.ai_available()
    status = runtime.model_status()
    assert not status["available"] and not status["test_mode"]
    with pytest.raises(ModelError, match="not connected"):
        runtime.get_model()
    # Must refuse before any identity, conversation, usage or provider call.
    with pytest.raises(inbox.InboxError, match="unavailable") as error:
        voice.start_call(None, "business")
    assert error.value.code == 503


def test_simulation_requires_explicit_test_mode_or_injected_test_model(monkeypatch, settings):
    monkeypatch.setattr(runtime, "_override", None)
    monkeypatch.setattr(runtime, "get_settings", lambda: replace(settings, llm_api_key=""))
    monkeypatch.setenv("EXA_COMMAI_AI_TEST_MODE", "1")
    assert isinstance(runtime.get_model(), SimulatedModel)
    assert runtime.model_status()["test_mode"] and runtime.model_status()["available"]
    monkeypatch.delenv("EXA_COMMAI_AI_TEST_MODE")
    runtime.set_model(SimulatedModel())
    assert runtime.model_ready()  # explicit test injection still works


@pytest.mark.parametrize("failure", ["key_removed", "provider_timeout"])
def test_active_call_hands_to_team_instead_of_simulating_after_service_loss(client, monkeypatch, failure):
    monkeypatch.setattr(runtime, "_override", None)
    b = ai_business(client)
    key = _widget_key(client, b, settings={"ai_calls": True})
    visitor = Visitor(client, key)
    url = f"{base(b)}/ai/calls"
    started = client.post(url, json={"caller": "Readiness regression"}, headers=b["agent"]["h"])
    assert started.status_code == 201, started.text
    conversation = started.json()["conversation_id"]
    monkeypatch.delenv("EXA_COMMAI_AI_TEST_MODE", raising=False)
    service = replace(client.app.state.settings, llm_api_key="test-key" if failure == "provider_timeout" else "")
    monkeypatch.setattr(runtime, "get_settings", lambda: service)
    monkeypatch.setattr(governance, "live_model", lambda default: default)

    def unavailable(*args, **kwargs):
        raise ModelError("The configured AI service timed out.")

    monkeypatch.setattr(OpenAICompatibleModel, "complete", unavailable)
    if failure == "key_removed":
        status = client.get(f"{base(b)}/ai/status", headers=b["agent"]["h"])
        assert status.status_code == 200 and not status.json()["ai_available"]
        assert not client.get(f"{visitor.u}/config", headers={"Origin": visitor.origin}).json()["ai_calls"]
        assert client.post(url, json={}, headers=b["agent"]["h"]).status_code == 503
    with db.tx() as conn:
        message = voice.caller_turn(conn, b["id"], conversation, "When are you open on Saturday?")
    with db.tx() as conn:
        result = agent.respond(conn, b["id"], conversation, message["id"])
    assert result["outcome"] == "failed"
    with db.tx() as conn:
        assert inbox.get(conn, b["id"], conversation)["handler"] != "ai"
        assert conn.execute("SELECT 1 FROM action_runs WHERE conversation_id = %s", (conversation,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM sim_records WHERE customer_id = %s", (b["id"],)).fetchone() is None
        assert (
            conn.execute("SELECT count(*) AS n FROM ai_calls WHERE customer_id = %s", (b["id"],)).fetchone()["n"] == 1
        )
    assert client.post(f"{url}/{conversation}/end", headers=b["agent"]["h"]).status_code == 200
