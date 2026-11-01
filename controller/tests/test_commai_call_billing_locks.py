"""Real PostgreSQL lock ordering for browser-call publication, billing and erasure.

Only the deterministic model is injected. Usage, rate-card initialization,
conversation/call locks, memory FK writes and erasure use their real services.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from psycopg.types.json import Jsonb

from exaconnect_controller import db
from exaconnect_controller.commai import bill, inbox, usage, visitor_calls
from exaconnect_controller.commai.ai import agent, attachments, copilot, memory, runtime, voice
from exaconnect_controller.commai.ai.model import ModelOutput
from exaconnect_controller.commai.enterprise import governance

from .test_commai_ai import ai_business


class TestModel:
    __test__ = False
    name = "locking-fixture"

    def __init__(self, entered=None, release=None, facts=None, failure=False):
        self.entered, self.release, self.facts, self.failure = entered, release, facts or [], failure

    def complete(self, inp):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=8), "model fixture was never released"
        if self.failure:
            raise RuntimeError("Deterministic model failure")
        return ModelOutput(answer="A deterministic reply.", intent="greeting", facts=self.facts)


def _case(client, *, has_card=False, verified=False):
    business = ai_business(client)
    cid = business["id"]
    with db.tx() as conn:
        call = voice.start_call(conn, cid, caller="Lock regression")
        conversation = call["conversation_id"]
        message = voice.caller_turn(conn, cid, conversation, "When are you open on Saturday?")
        conv = inbox.get(conn, cid, conversation)
        if verified:
            conn.execute("UPDATE contact_identities SET verified = true WHERE id = %s", (conv["identity_id"],))
        if has_card:
            bill.ensure_card(conn, cid)
        else:
            conn.execute("DELETE FROM commai_rate_cards WHERE customer_id = %s", (cid,))
        conn.execute(
            "INSERT INTO commai_budgets (customer_id, scope, monthly_hard, updated_by) "
            "VALUES (%s, 'total', 100, 'test')",
            (cid,),
        )
    return business, conversation, message["id"], conv["contact_id"]


def _limits(conn, *, short=False):
    # A short blocker timeout makes the old inversion fail in the ending
    # transaction instead of allowing the agent's failure handler to mask it.
    conn.execute("SET LOCAL lock_timeout = '250ms'" if short else "SET LOCAL lock_timeout = '6s'")
    conn.execute("SET LOCAL statement_timeout = '10s'")


def _attach_voice_note(cid, conversation, message, monkeypatch):
    monkeypatch.setattr(attachments, "_speech", attachments.SimulatedSpeech())
    data = b"OggS" + b"\x00" * 30 + b"SIMTEXT:When are you open on Saturday?"
    with db.tx() as conn:
        fid = conn.execute(
            """INSERT INTO channel_files (customer_id, conversation_id, owner, name, content_type, size, data)
               VALUES (%s, %s, 'locking-fixture', 'note.ogg', 'audio/ogg', %s, %s) RETURNING id""",
            (cid, conversation, len(data), data),
        ).fetchone()["id"]
        conn.execute(
            "UPDATE messages SET attachments = %s WHERE id = %s",
            (Jsonb([{"id": str(fid), "name": "note.ogg", "type": "audio/ogg"}]), message),
        )
    return fid


def _answer(cid, conversation, message, pid, started):
    with db.tx() as conn:
        _limits(conn)
        pid.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        started.set()
        return agent.respond(conn, cid, conversation, message)


def _blocked(pid, expected_blocker=None):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with db.tx() as conn:
            blockers = conn.execute("SELECT pg_blocking_pids(%s) AS blockers", (pid,)).fetchone()["blockers"]
        if blockers and (expected_blocker is None or expected_blocker in blockers):
            return
        time.sleep(0.02)
    raise AssertionError(f"backend {pid} did not reach the intended real SQL lock")


def _assert_stopped(cid, conversation):
    with db.tx() as conn:
        assert voice.get_call(conn, cid, conversation)["ended_at"] is not None
        rows = conn.execute("SELECT meter, quantity FROM usage_records WHERE customer_id = %s", (cid,)).fetchall()
        assert [(r["meter"], float(r["quantity"])) for r in rows] == [("ai_voice_minute", 1.0)]
        assert (
            conn.execute("SELECT count(*) AS n FROM commai_rate_cards WHERE customer_id = %s", (cid,)).fetchone()["n"]
            == 1
        )
        assert conn.execute("SELECT 1 FROM ai_runs WHERE conversation_id = %s", (conversation,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM action_runs WHERE conversation_id = %s", (conversation,)).fetchone() is None
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM messages WHERE conversation_id = %s AND direction = 'out'", (conversation,)
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commai_events WHERE customer_id = %s AND type = 'ai.call_ended'", (cid,)
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute("SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'ai.failed'", (cid,)).fetchone()
            is None
        )


@pytest.mark.parametrize("has_card", [False, True])
def test_end_first_never_waits_for_billing_owned_by_a_reply_waiting_for_the_call(client, monkeypatch, has_card):
    b, conversation, message, _ = _case(client, has_card=has_card)
    entered = Event()
    monkeypatch.setattr(runtime, "get_model", lambda: TestModel(entered=entered))
    pid, started = [], Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.tx() as ending:
            _limits(ending, short=True)
            owner = ending.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            ending.execute("SELECT id FROM ai_calls WHERE conversation_id = %s FOR UPDATE", (conversation,))
            pending = pool.submit(_answer, b["id"], conversation, message, pid, started)
            assert started.wait(timeout=5) and entered.wait(timeout=5)
            _blocked(pid[0], owner)
            # The real callback waits on the call before it can own a card lock,
            # including the first INSERT of version 1 and the hard-budget path.
            assert voice.end_call(ending, b["id"], conversation, "end-first")["ended_at"] is not None
        assert pending.result(timeout=8)["outcome"] == "stopped"
    _assert_stopped(b["id"], conversation)


@pytest.mark.parametrize("has_card", [False, True])
def test_budget_preflight_does_not_hold_billing_or_call_locks_during_model_io(client, monkeypatch, has_card):
    b, conversation, message, _ = _case(client, has_card=has_card)
    entered, release, started = Event(), Event(), Event()
    monkeypatch.setattr(runtime, "get_model", lambda: TestModel(entered, release))
    pid = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_answer, b["id"], conversation, message, pid, started)
        try:
            assert started.wait(timeout=5) and entered.wait(timeout=5)
            with db.tx() as conn:
                assert (
                    conn.execute(
                        "SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND granted", (pid[0],)
                    ).fetchone()
                    is None
                )
                assert conn.execute(
                    "SELECT count(*) AS n FROM commai_rate_cards WHERE customer_id = %s", (b["id"],)
                ).fetchone()["n"] == int(has_card)
                _limits(conn, short=True)
                voice.end_call(conn, b["id"], conversation, "disconnect-during-model")
        finally:
            release.set()
        assert pending.result(timeout=8)["outcome"] == "stopped"
    _assert_stopped(b["id"], conversation)


@pytest.mark.parametrize("has_card", [False, True])
def test_reply_first_and_concurrent_end_both_commit_without_a_failure_handover(client, monkeypatch, has_card):
    b, conversation, message, _ = _case(client, has_card=has_card)
    metered, release, started, end_started = Event(), Event(), Event(), Event()
    monkeypatch.setattr(runtime, "get_model", lambda: TestModel())
    real_complete = runtime.complete

    def metered_complete(*args, **kwargs):
        result = real_complete(*args, **kwargs)  # Actual callback, usage INSERT, and card advisory lock.
        metered.set()
        assert release.wait(timeout=8)
        return result

    monkeypatch.setattr(runtime, "complete", metered_complete)
    reply_pid, end_pid = [], []

    def end():
        with db.tx() as conn:
            _limits(conn)
            end_pid.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            end_started.set()
            return voice.end_call(conn, b["id"], conversation, "reply-first")

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(_answer, b["id"], conversation, message, reply_pid, started)
        try:
            assert started.wait(timeout=5) and metered.wait(timeout=5)
            ending = pool.submit(end)
            assert end_started.wait(timeout=5)
            _blocked(end_pid[0], reply_pid[0])
        finally:
            release.set()
        assert answer.result(timeout=8)["outcome"] == "replied"
        assert ending.result(timeout=8)["ended_at"] is not None
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM usage_records WHERE customer_id = %s AND meter = 'ai_reply'", (b["id"],)
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM usage_records WHERE customer_id = %s AND meter = 'ai_voice_minute'",
                (b["id"],),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'ai.failed'", (b["id"],)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("mode", ["anonymise", "delete"])
@pytest.mark.parametrize("stage", ["model", "failure", "escalation"])
def test_erase_first_stops_captured_reply_memory_and_usage_before_publication(client, monkeypatch, mode, stage):
    b, conversation, message, contact = _case(client, verified=True)
    fid = _attach_voice_note(b["id"], conversation, message, monkeypatch)
    if stage == "escalation":
        with db.tx() as conn:
            conn.execute("UPDATE messages SET body = 'Can I talk to a person?' WHERE id = %s", (message,))
    entered, started, pid = Event(), Event(), []
    monkeypatch.setattr(
        runtime, "get_model", lambda: TestModel(entered=entered, facts=["Prefers email"], failure=stage == "failure")
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.tx() as erasing:
            _limits(erasing, short=True)
            owner = erasing.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            erasing.execute("SELECT id FROM contacts WHERE id = %s FOR UPDATE", (contact,))
            answer = pool.submit(_answer, b["id"], conversation, message, pid, started)
            assert started.wait(timeout=5)
            if stage != "escalation":
                assert entered.wait(timeout=5)
            _blocked(pid[0], owner)
            assert erasing.execute("SELECT 1 FROM attachment_readings WHERE file_id = %s", (fid,)).fetchone() is None
            assert not governance.subject_erase(erasing, b["id"], contact, mode, "privacy-test")["refused"]
        assert answer.result(timeout=8)["outcome"] == "stopped"
        if stage == "escalation":
            assert not entered.is_set()
    with db.tx() as conn:
        for table in ("usage_records", "contact_memory", "attachment_readings", "ai_runs", "action_runs"):
            assert conn.execute(f"SELECT 1 FROM {table} WHERE customer_id = %s", (b["id"],)).fetchone() is None
        assert (
            conn.execute(
                "SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'ai.failed'", (b["id"],)
            ).fetchone()
            is None
        )
        if mode == "anonymise":
            assert (
                conn.execute(
                    "SELECT 1 FROM messages WHERE conversation_id = %s AND body <> ''", (conversation,)
                ).fetchone()
                is None
            )
            # An existing anonymised call must still terminate/account once;
            # publication and further caller transcripts remain forbidden.
            assert voice.end_call(conn, b["id"], conversation, "cleanup")["ended_at"] is not None
            voice.end_call(conn, b["id"], conversation, "cleanup-retry")
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM usage_records WHERE customer_id = %s AND meter = 'ai_voice_minute'",
                    (b["id"],),
                ).fetchone()["n"]
                == 1
            )
            with pytest.raises(inbox.NotHandler):
                voice.caller_turn(conn, b["id"], conversation, "late transcript")
        else:
            with pytest.raises(inbox.InboxError) as error:
                voice.end_call(conn, b["id"], conversation, "late-end")
            assert error.value.code == 404
            job = {"customer_id": b["id"], "payload": {"conversation_id": conversation}}
            assert visitor_calls._expire(conn, job) is None


@pytest.mark.parametrize("mode", ["anonymise", "delete"])
def test_publication_first_keeps_real_memory_fk_ahead_of_erasure_and_late_runs_stop(client, monkeypatch, mode):
    b, conversation, message, contact = _case(client, verified=True)
    fid = _attach_voice_note(b["id"], conversation, message, monkeypatch)
    written, release, started, erase_started = Event(), Event(), Event(), Event()
    monkeypatch.setattr(runtime, "get_model", lambda: TestModel(facts=["Prefers email"]))
    real_remember = memory.remember

    def remember(*args, **kwargs):
        result = real_remember(*args, **kwargs)  # Actual contact_memory INSERT and its real contact FK.
        assert args[0].execute("SELECT 1 FROM attachment_readings WHERE file_id = %s", (fid,)).fetchone()
        written.set()
        assert release.wait(timeout=8)
        return result

    monkeypatch.setattr(memory, "remember", remember)
    reply_pid, erase_pid = [], []

    def erase():
        with db.tx() as conn:
            _limits(conn)
            erase_pid.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            erase_started.set()
            return governance.subject_erase(conn, b["id"], contact, mode, "privacy-test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(_answer, b["id"], conversation, message, reply_pid, started)
        try:
            assert started.wait(timeout=5) and written.wait(timeout=5)
            erasing = pool.submit(erase)
            assert erase_started.wait(timeout=5)
            _blocked(erase_pid[0], reply_pid[0])
        finally:
            release.set()
        assert answer.result(timeout=8)["outcome"] == "replied"
        assert not erasing.result(timeout=8)["refused"]
    with db.tx() as conn:
        before = conn.execute("SELECT count(*) AS n FROM usage_records WHERE customer_id = %s", (b["id"],)).fetchone()[
            "n"
        ]
        assert conn.execute("SELECT 1 FROM contact_memory WHERE customer_id = %s", (b["id"],)).fetchone() is None
        assert conn.execute("SELECT 1 FROM attachment_readings WHERE customer_id = %s", (b["id"],)).fetchone() is None
        assert (
            conn.execute("SELECT 1 FROM messages WHERE conversation_id = %s AND body <> ''", (conversation,)).fetchone()
            is None
        )
        assert agent.respond(conn, b["id"], conversation, message)["outcome"] == "stopped"
        assert (
            conn.execute("SELECT count(*) AS n FROM usage_records WHERE customer_id = %s", (b["id"],)).fetchone()["n"]
            == before
        )


def test_budget_quote_and_currency_refusal_do_not_initialize_a_card(client):
    b = ai_business(client)
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_rate_cards WHERE customer_id = %s", (b["id"],))
        conn.execute(
            "INSERT INTO commai_budgets (customer_id, scope, monthly_hard, updated_by) VALUES (%s, 'total', 0, 'test')",
            (b["id"],),
        )
        assert "USD" in usage.blocked_by(conn, b["id"], "ai_reply")
        assert conn.execute("SELECT 1 FROM commai_rate_cards WHERE customer_id = %s", (b["id"],)).fetchone() is None
        pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        assert (
            conn.execute(
                "SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND granted", (pid,)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("has_card", [False, True])
def test_copilot_waits_for_publication_before_taking_real_billing_locks(client, monkeypatch, has_card):
    b, conversation, message, _ = _case(client, has_card=has_card)
    claimed, release, started, copilot_started = Event(), Event(), Event(), Event()
    monkeypatch.setattr(runtime, "get_model", lambda: TestModel())
    real_claim = agent._claim

    def claim(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        claimed.set()
        assert release.wait(timeout=8)
        return result

    monkeypatch.setattr(agent, "_claim", claim)
    reply_pid, copilot_pid = [], []

    def suggest():
        with db.tx() as conn:
            _limits(conn)
            copilot_pid.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            copilot_started.set()
            return copilot.run(conn, b["id"], conversation, "summary", actor="test", include_notes=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(_answer, b["id"], conversation, message, reply_pid, started)
        try:
            assert started.wait(timeout=5) and claimed.wait(timeout=5)
            suggestion = pool.submit(suggest)
            assert copilot_started.wait(timeout=5)
            _blocked(copilot_pid[0], reply_pid[0])
        finally:
            release.set()
        assert answer.result(timeout=8)["outcome"] == "replied"
        assert suggestion.result(timeout=8)["summary"]
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'ai.failed'", (b["id"],)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("has_card", [False, True])
def test_first_card_and_concurrent_approved_versions_remain_serialized(client, has_card):
    b = ai_business(client)
    with db.tx() as conn:
        if has_card:
            bill.ensure_card(conn, b["id"])
        else:
            conn.execute("DELETE FROM commai_rate_cards WHERE customer_id = %s", (b["id"],))
    barrier = Barrier(2)

    def approve(price):
        with db.tx() as conn:
            _limits(conn)
            barrier.wait(timeout=5)
            return bill.new_card(conn, b["id"], {"ai_reply": price}, "rate-admin")["version"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, ["0.03", "0.04"])) == [2, 3]
    with db.tx() as conn:
        cards = conn.execute(
            "SELECT version, example FROM commai_rate_cards WHERE customer_id = %s ORDER BY version", (b["id"],)
        ).fetchall()
        assert [(r["version"], r["example"]) for r in cards] == [(1, True), (2, False), (3, False)]


def test_runtime_claims_after_model_output_and_before_any_usage(monkeypatch):
    order = []

    class Model:
        def complete(self, inp):
            order.append("model")
            return ModelOutput(answer="Test", tokens_in=3)

    monkeypatch.setattr(runtime, "get_model", lambda: Model())
    monkeypatch.setattr(bill, "ensure_card", lambda *args: order.append("billing"))
    monkeypatch.setattr(usage, "allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(usage, "record", lambda conn, cid, meter, *args, **kwargs: order.append(meter))
    runtime.complete(
        None,
        "customer",
        "customer_agent",
        "reply",
        {},
        business="Fixture",
        prof=runtime.DEFAULT_PROFILE,
        before_usage=lambda: order.append("claim"),
    )
    assert order == ["model", "claim", "billing", "ai_reply", "ai_tokens"]


def test_parallel_completions_recheck_the_recorded_budget_after_claiming_publication(client, monkeypatch):
    b, first, first_message, _ = _case(client, has_card=True)
    with db.tx() as conn:
        conn.execute("UPDATE commai_budgets SET monthly_hard = 0.03 WHERE customer_id = %s", (b["id"],))
        second = voice.start_call(conn, b["id"])["conversation_id"]
        second_message = voice.caller_turn(conn, b["id"], second, "When are you open?")["id"]
    metered, release, first_started, second_started = Event(), Event(), Event(), Event()
    monkeypatch.setattr(runtime, "get_model", lambda: TestModel())
    real_complete = runtime.complete

    def complete(conn, cid, role, task, context, **kwargs):
        result = real_complete(conn, cid, role, task, context, **kwargs)
        if kwargs.get("ref") == str(first_message):
            metered.set()
            assert release.wait(timeout=8)
        return result

    monkeypatch.setattr(runtime, "complete", complete)
    first_pid, second_pid = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_answer, b["id"], first, first_message, first_pid, first_started)
        try:
            assert first_started.wait(timeout=5) and metered.wait(timeout=5)
            other = pool.submit(_answer, b["id"], second, second_message, second_pid, second_started)
            assert second_started.wait(timeout=5)
            _blocked(second_pid[0], first_pid[0])
        finally:
            release.set()
        assert a.result(timeout=8)["outcome"] == "replied"
        assert other.result(timeout=8)["outcome"] == "escalated"
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM usage_records WHERE customer_id = %s AND meter = 'ai_reply'", (b["id"],)
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute("SELECT outcome FROM ai_runs WHERE conversation_id = %s", (second,)).fetchone()["outcome"]
            == "escalated"
        )
        assert (
            conn.execute(
                "SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'ai.failed'", (b["id"],)
            ).fetchone()
            is None
        )
