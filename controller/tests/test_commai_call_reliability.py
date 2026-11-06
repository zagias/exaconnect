"""Voice failure boundaries: unfinished telephone AI, disconnects and limits.

The database cases run against real PostgreSQL in CI; they do not assert
that a text-only controller exchange proves a working telephone call.
"""

import asyncio
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import inbox, visitor_calls
from exaconnect_controller.commai.ai import agent, runtime, voice
from exaconnect_controller.commai.ai.model import ModelOutput
from exaconnect_controller.commai.voice import livekit
from exaconnect_controller.commai.voice.common import VoiceError

from .test_commai_ai import ai_business
from .test_commai_channels import Visitor, _widget_key


def test_phone_credentials_do_not_enable_an_unfinished_worker(monkeypatch):
    for name in livekit.SETTINGS:
        monkeypatch.setenv(name, "configured-for-test")
    monkeypatch.setenv("EXA_LIVEKIT_URL", "ws://livekit:7880")
    status = livekit.status()
    assert status["configured"] is True
    assert status["phone_ready"] is False
    assert "handover" in status["phone_reason"]


@pytest.mark.parametrize("header", [None, "secret-for-test", "Basic secret-for-test", "Bearer wrong"])
def test_phone_worker_requires_bearer_authentication(monkeypatch, header):
    monkeypatch.setenv("EXA_LIVEKIT_AGENT_SECRET", "secret-for-test")
    with pytest.raises(VoiceError) as exc:
        livekit.check_agent(header)
    assert exc.value.code == 401


def test_unconfigured_worker_refuses_before_registration_or_call_side_effects(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "deploy/livekit/agent/agent.py"
    monkeypatch.syspath_prepend(str(path.parent))
    for name in ("STT_URL", "STT_KEY", "STT_MODEL", "TTS_URL", "TTS_KEY", "TTS_MODEL", "TTS_VOICE"):
        monkeypatch.delenv(f"EXA_PHONE_{name}", raising=False)
    spec = importlib.util.spec_from_file_location("telephone_worker_under_test", path)
    worker = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, worker)
    spec.loader.exec_module(worker)
    # No LiveKit SDK or connection is required to refuse an unready worker.
    # Passing an object without connect also proves it cannot join a room.
    with pytest.raises(RuntimeError, match="STT and TTS"):
        asyncio.run(worker.entrypoint(object()))
    with pytest.raises(RuntimeError, match="STT and TTS"):
        worker.main()


def test_concurrent_call_end_records_one_duration_and_one_end_event(client):
    b = ai_business(client)
    with db.tx() as conn:
        call = voice.start_call(conn, b["id"], caller="Concurrent disconnect test")
    conversation = call["conversation_id"]
    barrier = Barrier(2)

    def end(actor):
        barrier.wait(timeout=10)
        with db.tx() as conn:
            return voice.end_call(conn, b["id"], conversation, actor)["ended_at"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(end, ["browser:end", "browser:disconnect"]))
    assert first == second
    with db.tx() as conn:
        assert voice.end_call(conn, b["id"], conversation, "retry")["ended_at"] == first
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM usage_records WHERE customer_id = %s AND meter = 'ai_voice_minute'",
                (b["id"],),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commai_events WHERE customer_id = %s AND type = 'ai.call_ended'",
                (b["id"],),
            ).fetchone()["n"]
            == 1
        )
        with pytest.raises(inbox.InboxError, match="This call has ended"):
            voice.caller_turn(conn, b["id"], conversation, "late transcript")


def test_visitor_time_limit_commits_termination_even_when_returning_409(client):
    b = ai_business(client)
    key = _widget_key(client, b, settings={"ai_calls": True})
    visitor = Visitor(client, key)
    url = f"{visitor.u}/calls"
    started = client.post(url, json={}, headers=visitor.h())
    assert started.status_code == 201, started.text
    conversation = started.json()["conversation_id"]
    with db.tx() as conn:
        conn.execute(
            "UPDATE ai_calls SET started_at = now() - interval '16 minutes' WHERE conversation_id = %s",
            (conversation,),
        )
    refused = client.post(f"{url}/{conversation}/turns", json={"text": "Still there?"}, headers=visitor.h())
    assert refused.status_code == 409 and "15 minutes" in refused.json()["detail"]
    with db.tx() as conn:
        call = voice.get_call(conn, b["id"], conversation)
        assert call["ended_at"] is not None and call["turns"] == 0
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM usage_records WHERE customer_id = %s AND meter = 'ai_voice_minute'",
                (b["id"],),
            ).fetchone()["n"]
            == 1
        )
    assert client.post(f"{url}/{conversation}/end", json={}, headers=visitor.h()).status_code == 200
    assert (
        client.post(f"{url}/{conversation}/turns", json={"text": "late retry"}, headers=visitor.h()).status_code == 409
    )

    # A disappeared browser sends neither another turn nor an End request.
    # Its scheduled task must still close the call without billing downtime.
    abandoned = client.post(url, json={}, headers=visitor.h()).json()["conversation_id"]
    with db.tx() as conn:
        conn.execute(
            "UPDATE ai_calls SET started_at = now() - interval '2 days' WHERE conversation_id = %s", (abandoned,)
        )
        task = conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key = %s", (f"visitor-call-expire:{abandoned}",)
        ).fetchone()
        visitor_calls._expire(conn, task)
        visitor_calls._expire(conn, task)
        ended = voice.get_call(conn, b["id"], abandoned)
        assert (ended["ended_at"] - ended["started_at"]).total_seconds() == 15 * 60
        records = conn.execute(
            "SELECT quantity FROM usage_records WHERE customer_id = %s AND meter = 'ai_voice_minute'", (b["id"],)
        ).fetchall()
        assert len(records) == 2 and all(float(row["quantity"]) == 15 for row in records)


@pytest.mark.parametrize("model_fails", [False, True])
def test_disconnect_during_model_prevents_reply_actions_and_later_queued_runs(client, monkeypatch, model_fails):
    b = ai_business(client)
    with db.tx() as conn:
        call = voice.start_call(conn, b["id"])
        conversation = call["conversation_id"]
        message = voice.caller_turn(conn, b["id"], conversation, "When are you open?")
    entered, release = Event(), Event()
    attempts = []

    def slow_model(*args, **kwargs):
        attempts.append(True)
        entered.set()
        assert release.wait(timeout=10)
        if model_fails:
            raise RuntimeError("provider lost")
        return ModelOutput(answer="Late answer", tool_calls=[{"tool": "unknown.create", "inputs": {}}])

    monkeypatch.setattr(runtime, "complete", slow_model)

    def answer():
        with db.tx() as conn:
            return agent.respond(conn, b["id"], conversation, message["id"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(answer)
        try:
            assert entered.wait(timeout=10)
            with db.tx() as conn:
                voice.end_call(conn, b["id"], conversation, "caller disconnected")
        finally:
            release.set()
        assert pending.result(timeout=10)["outcome"] == "stopped"
    with db.tx() as conn:
        assert agent.respond(conn, b["id"], conversation, message["id"])["outcome"] == "stopped"
        assert conn.execute("SELECT 1 FROM ai_runs WHERE conversation_id = %s", (conversation,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM action_runs WHERE conversation_id = %s", (conversation,)).fetchone() is None
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM messages WHERE conversation_id = %s AND direction = 'out'", (conversation,)
            ).fetchone()["n"]
            == 1
        )
    assert len(attempts) == 1
