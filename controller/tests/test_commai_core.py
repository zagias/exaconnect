"""CommAI foundation: inbox, notes isolation, one handler at a time, durable
jobs, idempotency, webhooks and safe actions (ADR 0016)."""

import datetime as dt
import json

from exaconnect_controller import db
from exaconnect_controller.commai import actions, inbox, jobs, webhooks

from .commai_helpers import allow_tools, api_key, base, business, connect_app, run_jobs


def _inbound(b, body="Hello, I need help with my card", address="+18685550101", external_id=None):
    with db.tx() as conn:
        out = inbox.receive(conn, b["id"], "web", address, body, external_id=external_id, name="Ana")
    return out


def test_enquiry_reaches_inbox_routed_and_replied(client):
    b = business(client)
    u = base(b)
    team = client.post(f"{u}/teams", json={"name": "Cards", "members": [b["agent"]["id"]]}, headers=b["agent"]["h"])
    assert team.status_code == 201, team.text
    r = client.post(
        f"{u}/routing-rules",
        json={
            "name": "Card problems",
            "match": {"keywords": ["card"]},
            "team_id": team.json()["id"],
            "priority": "high",
        },
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    out = _inbound(b)
    conv = out["conversation"]
    assert str(conv["team_id"]) == team.json()["id"] and str(conv["assignee_id"]) == b["agent"]["id"]
    assert conv["priority"] == "high" and conv["first_reply_due"] is not None

    listed = client.get(f"{u}/conversations?view=mine", headers=b["agent"]["h"]).json()
    assert [c["id"] for c in listed["items"]] == [str(conv["id"])]
    assert listed["items"][0]["contact_name"] == "Ana"

    r = client.post(
        f"{u}/conversations/{conv['id']}/messages", json={"body": "Hi Ana, I can help."}, headers=b["agent"]["h"]
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "sent"  # website chat: the widget collects it
    detail = client.get(f"{u}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()
    assert [m["direction"] for m in detail["messages"]] == ["in", "out"]
    assert detail["state"] == "awaiting_customer" and detail["handler"] == "human"
    assert detail["first_reply_at"] is not None
    kinds = [entry["kind"] for entry in detail["log"]]
    assert "assign" in kinds and "handler" in kinds and "state" in kinds

    # The customer writes again: same conversation, open again.
    again = _inbound(b, "Thanks, it's the debit card")
    assert again["conversation"]["id"] == conv["id"] and again["conversation"]["state"] == "open"


def test_notes_never_reach_customer_facing_paths(client):
    b = business(client)
    u = base(b)
    conv = _inbound(b)["conversation"]
    r = client.post(
        f"{u}/conversations/{conv['id']}/notes",
        json={"body": "SECRET: customer is on a watch list"},
        headers=b["internal"]["h"],
    )
    assert r.status_code == 201, r.text

    # Messages, the conversation, the export and the event log never carry the note.
    for path in (
        f"/conversations/{conv['id']}",
        f"/conversations/{conv['id']}/messages",
        f"/conversations/{conv['id']}/export",
        "/events",
    ):
        body = client.get(u + path, headers=b["agent"]["h"]).text
        assert "SECRET" not in body, path

    # A customer-facing key (no commai:notes) can't read notes.
    key = api_key(client, b["agent"]["h"], ["commai:read", "commai:write"])
    assert client.get(f"{u}/conversations/{conv['id']}/notes", headers=key).status_code == 403
    assert client.post(f"{u}/conversations/{conv['id']}/notes", json={"body": "x"}, headers=key).status_code == 403
    assert "SECRET" not in client.get(f"{u}/conversations/{conv['id']}", headers=key).text
    # ...nor the network API.
    assert client.get("/api/v1/overview", headers=key).status_code == 403
    # A staff key with commai:notes can.
    staff = api_key(client, b["agent"]["h"], ["commai:read", "commai:notes"], "staff")
    notes = client.get(f"{u}/conversations/{conv['id']}/notes", headers=staff).json()
    assert notes[0]["body"].startswith("SECRET")


def test_internal_seat_writes_notes_but_cannot_reply(client):
    b = business(client)
    u = base(b)
    conv = _inbound(b)["conversation"]
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "hi"}, headers=b["internal"]["h"])
    assert r.status_code == 403
    assert client.post(f"{u}/conversations/{conv['id']}/takeover", headers=b["internal"]["h"]).status_code == 403
    assert (
        client.post(
            f"{u}/conversations/{conv['id']}/notes", json={"body": "ok"}, headers=b["internal"]["h"]
        ).status_code
        == 201
    )


def test_tenant_isolation(client):
    a = business(client, "Bank A", ("agent",))
    other = business(client, "Shop B", ("agent",))
    conv = _inbound(a)["conversation"]
    with db.tx() as conn:
        inbox.add_note(conn, a["id"], conv["id"], author="x", body="private")
    key = api_key(client, other["agent"]["h"], None)
    for path in ("/conversations", f"/conversations/{conv['id']}", f"/conversations/{conv['id']}/notes"):
        assert client.get(base(a) + path, headers=key).status_code == 403
        assert client.get(base(a) + path, headers=other["agent"]["h"]).status_code == 403
    # Asking for A's conversation under B's own id finds nothing.
    assert client.get(f"{base(other)}/conversations/{conv['id']}", headers=key).status_code == 404
    assert client.get(f"{base(other)}/conversations/{conv['id']}/notes", headers=key).status_code == 404


def test_one_handler_at_a_time(client):
    b = business(client)
    u = base(b)
    conv = _inbound(b)["conversation"]
    with db.tx() as conn:
        conn.execute("UPDATE conversations SET handler = 'ai' WHERE id = %s", (conv["id"],))
        inbox.send(conn, b["id"], conv["id"], "Hello from the AI", author_kind="ai", author="ai")
    # A person takes over: the AI is stopped at once and the context stays.
    r = client.post(f"{u}/conversations/{conv['id']}/takeover", headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["handler"] == "human"
    with db.tx() as conn:
        try:
            inbox.send(conn, b["id"], conv["id"], "AI again", author_kind="ai", author="ai")
            raise AssertionError("the AI replied after a takeover")
        except inbox.NotHandler:
            pass
    # A second person sees who handles it before replying.
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "me too"}, headers=b["agent2"]["h"])
    assert r.status_code == 409 and b["agent"]["email"] in r.json()["detail"]
    r = client.post(
        f"{u}/conversations/{conv['id']}/messages", json={"body": "me too", "take_over": True}, headers=b["agent2"]["h"]
    )
    assert r.status_code == 201
    msgs = client.get(f"{u}/conversations/{conv['id']}/messages", headers=b["agent"]["h"]).json()
    assert [m["body"] for m in msgs] == ["Hello, I need help with my card", "Hello from the AI", "me too"]


def test_repeated_inbound_and_idempotent_replies(client):
    b = business(client)
    u = base(b)
    first = _inbound(b, external_id="wamid.1")
    dup = _inbound(b, external_id="wamid.1")
    assert dup["duplicate"] and dup["message"]["id"] == first["message"]["id"]
    cid = first["conversation"]["id"]
    h = {**b["agent"]["h"], "Idempotency-Key": "reply-1"}
    r1 = client.post(f"{u}/conversations/{cid}/messages", json={"body": "Once"}, headers=h)
    r2 = client.post(f"{u}/conversations/{cid}/messages", json={"body": "Once"}, headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"] and r2.headers.get("idempotent-replayed") == "true"
    r3 = client.post(f"{u}/conversations/{cid}/messages", json={"body": "Different"}, headers=h)
    assert r3.status_code == 422
    msgs = client.get(f"{u}/conversations/{cid}/messages", headers=b["agent"]["h"]).json()
    assert [m["body"] for m in msgs].count("Once") == 1 and len(msgs) == 2


def test_job_dedupe_and_retry(client):
    calls = []

    @jobs.handler("test.flaky")
    def flaky(conn, job):
        calls.append(job["attempts"])
        if job["attempts"] < 2:
            raise RuntimeError("provider down")

    with db.tx() as conn:
        assert jobs.enqueue(conn, "test.flaky", dedupe_key="k1") is not None
        assert jobs.enqueue(conn, "test.flaky", dedupe_key="k1") is None
    run_jobs()
    run_jobs()
    assert calls == [1, 2]
    with db.tx() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE dedupe_key = 'k1'").fetchone()["status"] == "done"
        # A worker that crashed mid-job leaves a lease that expires; the job runs again.
        jobs.enqueue(conn, "test.flaky", dedupe_key="k2")
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = 1, locked_until = now() - interval '1 s'"
            " WHERE dedupe_key = 'k2'"
        )
    run_jobs()
    with db.tx() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE dedupe_key = 'k2'").fetchone()["status"] == "done"


def test_webhooks_signed_retried_and_deduplicated(client, monkeypatch):
    b = business(client)
    u = base(b)
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    r = client.post(
        f"{u}/webhooks", json={"url": "http://hooks.example/in", "events": ["message.*"]}, headers=b["agent"]["h"]
    )
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert "secret" not in client.get(f"{u}/webhooks", headers=b["agent"]["h"]).json()[0]

    got, answers = [], [500, 200]

    def fake(url, body, headers, timeout_s=10):
        got.append((body, headers))
        return answers.pop(0) if answers else 200

    monkeypatch.setattr(webhooks, "sender", fake)
    _inbound(b, external_id="m1")
    run_jobs()  # first attempt answers 500
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET run_after = now()")
    run_jobs()  # retried, delivered
    received = [g for g in got if json.loads(g[0])["type"] == "message.received"]
    assert len(received) == 2
    body, headers = received[-1]
    assert webhooks.verify(secret, headers["X-ExaCarib-Timestamp"], body, headers["X-ExaCarib-Signature"])
    assert not webhooks.verify("wrong", headers["X-ExaCarib-Timestamp"], body, headers["X-ExaCarib-Signature"])
    assert received[0][1]["X-ExaCarib-Event-Id"] == received[1][1]["X-ExaCarib-Event-Id"]
    assert not [g for g in got if json.loads(g[0])["type"] == "conversation.created"]  # not subscribed
    deliveries = client.get(f"{u}/webhooks/{r.json()['id']}/deliveries", headers=b["agent"]["h"]).json()
    assert {d["status"] for d in deliveries} == {"delivered"}
    # The same event is never fanned out twice.
    with db.tx() as conn:
        eid = conn.execute("SELECT event_id FROM webhook_deliveries LIMIT 1").fetchone()["event_id"]
        jobs.enqueue(conn, "webhook.fanout", {"event_id": str(eid)})
    before = len(got)
    run_jobs()
    assert len(got) == before


def test_private_webhook_addresses_refused(client):
    b = business(client, people=("agent",))
    r = client.post(f"{base(b)}/webhooks", json={"url": "https://127.0.0.1/x"}, headers=b["agent"]["h"])
    assert r.status_code == 422
    r = client.post(f"{base(b)}/webhooks", json={"url": "http://example.com/x"}, headers=b["agent"]["h"])
    assert r.status_code == 422


def _tomorrow_10() -> str:
    d = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(10), tzinfo=dt.UTC).isoformat()


def test_booking_confirmed_only_after_calendar_success_and_never_twice(client):
    b = business(client)
    conv = _inbound(b, "Can I book an appointment tomorrow at 10?")["conversation"]
    connect_app(b["id"], "sim_calendar", ["find_slots", "book"])
    allow_tools(b["id"], "customer_agent", ["sim_calendar.book"])
    inputs = {"start": _tomorrow_10(), "name": "Ana", "contact": "+18685550101"}
    with db.tx() as conn:
        conn.execute("UPDATE conversations SET handler = 'ai' WHERE id = %s", (conv["id"],))
        run = actions.propose(
            conn,
            b["id"],
            role="customer_agent",
            app="sim_calendar",
            action="book",
            inputs=inputs,
            actor="ai",
            conversation_id=conv["id"],
            on_success={"reply": "You're booked for {start}."},
        )
        again = actions.propose(
            conn,
            b["id"],
            role="customer_agent",
            app="sim_calendar",
            action="book",
            inputs=inputs,
            actor="ai",
            conversation_id=conv["id"],
        )
        assert again["id"] == run["id"]
        # Nothing has told the customer yet: the calendar has not answered.
        assert not [m for m in inbox.messages(conn, b["id"], conv["id"]) if "booked" in m["body"]]
    run_jobs()
    with db.tx() as conn:
        assert (
            conn.execute("SELECT status FROM action_runs WHERE id = %s", (run["id"],)).fetchone()["status"]
            == "succeeded"
        )
        assert conn.execute("SELECT count(*) AS n FROM sim_records").fetchone()["n"] == 1
        confirm = [m for m in inbox.messages(conn, b["id"], conv["id"]) if "booked" in m["body"]]
        assert len(confirm) == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM commai_events WHERE type = 'booking.confirmed'").fetchone()["n"]
            == 1
        )
        # A retried job with the same key books nothing new.
        jobs.enqueue(conn, "action.execute", {"run_id": str(run["id"])})
        conn.execute("UPDATE action_runs SET status = 'approved' WHERE id = %s", (run["id"],))
    run_jobs()
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM sim_records").fetchone()["n"] == 1


def test_unapproved_and_unlisted_actions_are_blocked(client):
    b = business(client)
    u = base(b)
    connect_app(b["id"], "sim_calendar", ["book", "cancel"])
    allow_tools(b["id"], "customer_agent", ["sim_calendar.cancel"])
    with db.tx() as conn:
        # Not in the role's list: refused by the service.
        try:
            actions.propose(
                conn,
                b["id"],
                role="customer_agent",
                app="sim_calendar",
                action="book",
                inputs={"start": _tomorrow_10(), "name": "A", "contact": "a@b.c"},
                actor="ai",
            )
            raise AssertionError("an unlisted tool ran")
        except actions.ActionRefused as e:
            assert e.code == 403
        # Sensitive: waits for a person.
        run = actions.propose(
            conn,
            b["id"],
            role="customer_agent",
            app="sim_calendar",
            action="cancel",
            inputs={"booking_id": "x"},
            actor="ai",
        )
        assert run["status"] == "awaiting_approval"
        try:
            actions.approve(conn, b["id"], run["id"], approver="ai")
            raise AssertionError("the AI approved its own action")
        except actions.ActionRefused:
            pass
    assert run_jobs() == 0  # nothing executes before approval
    r = client.post(f"{u}/actions/{run['id']}/approve", headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "approved"
    run_jobs()
    with db.tx() as conn:
        status = conn.execute("SELECT status, error FROM action_runs WHERE id = %s", (run["id"],)).fetchone()
    assert status["status"] == "failed" and "No such booking" in status["error"]


def test_failed_integration_hands_over_with_holding_reply(client):
    b = business(client)
    conv = _inbound(b, "Book me in please")["conversation"]
    connect_app(b["id"], "sim_calendar", ["book"], settings={"simulate_failure": "expired_signin"})
    allow_tools(b["id"], "customer_agent", ["sim_calendar.book"])
    with db.tx() as conn:
        conn.execute("UPDATE conversations SET handler = 'ai' WHERE id = %s", (conv["id"],))
        actions.propose(
            conn,
            b["id"],
            role="customer_agent",
            app="sim_calendar",
            action="book",
            inputs={"start": _tomorrow_10(), "name": "Ana", "contact": "x"},
            actor="ai",
            conversation_id=conv["id"],
            on_success={"reply": "Booked"},
        )
    run_jobs()
    detail = client.get(f"{base(b)}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()
    assert detail["handler"] == "none" and detail["handovers"]
    assert detail["handovers"][0]["packet"]["cause"] == "expired_signin"
    bodies = [m["body"] for m in detail["messages"]]
    assert actions.HOLDING_REPLY in bodies and "Booked" not in bodies
    with db.tx() as conn:
        c = conn.execute("SELECT status, last_error FROM integration_connections").fetchone()
    assert c["status"] == "broken" and "expired" in c["last_error"]
