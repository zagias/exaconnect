"""Jibsy AI agents (ADR 0019): the customer AI agent, knowledge with sources,
safe actions, handover and failure, memory, languages, the copilot and
browser calls. Acceptance tests 2 (customer AI), 3, 4 and 8."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import inbox, jobs
from exaconnect_controller.commai.ai import knowledge, runtime
from exaconnect_controller.commai.ai.model import ModelError, ModelInput, ModelOutput, SimulatedModel

from .commai_helpers import allow_tools, api_key, base, business, connect_app, run_jobs

HOURS = (
    "Opening hours\n\nWe are open Monday to Friday from 8am to 4pm, and on Saturday from 9am to 1pm. "
    "We are closed on public holidays."
)
FEES = "Card fees\n\nA replacement debit card costs 25 dollars and arrives within five working days."


class Capturing:
    """The simulated model, keeping every input it was given."""

    name = "capturing"

    def __init__(self, inner=None):
        self.inner = inner or SimulatedModel()
        self.inputs: list[ModelInput] = []

    def complete(self, inp: ModelInput) -> ModelOutput:
        self.inputs.append(inp)
        return self.inner.complete(inp)

    def seen(self) -> str:
        return "\n".join(i.system + i.as_json() for i in self.inputs)


class Failing:
    name = "failing"

    def __init__(self, exc: Exception):
        self.exc = exc

    def complete(self, inp: ModelInput) -> ModelOutput:
        raise self.exc


@pytest.fixture(autouse=True)
def simulated():
    runtime.set_model(SimulatedModel())
    yield
    runtime.set_model(None)


def ai_business(client, sources=((HOURS, True), (FEES, True))) -> dict:
    b = business(client)
    r = client.patch(f"{base(b)}/settings", json={"mode": "ai_first"}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    for text, approved in sources:
        title, body = text.split("\n\n", 1)
        r = client.post(
            f"{base(b)}/ai/knowledge",
            json={"title": title, "body": body, "approved": approved, "source_url": "https://example.org/help"},
            headers=b["agent"]["h"],
        )
        assert r.status_code == 201, r.text
    return b


def inbound(b, body, address="visitor-1", *, verified=False, channel="web", name=""):
    with db.tx() as conn:
        return inbox.receive(conn, b["id"], channel, address, body, verified=verified, name=name)


def msgs(b, conv_id) -> list[dict]:
    with db.tx() as conn:
        return inbox.messages(conn, b["id"], conv_id)


def conv_row(b, conv_id) -> dict:
    with db.tx() as conn:
        return inbox.get(conn, b["id"], conv_id)


def ai_runs(conv_id) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM ai_runs WHERE conversation_id = %s ORDER BY created_at", (conv_id,)
        ).fetchall()


# ---- answers from knowledge ---------------------------------------------------------


def test_answers_from_approved_knowledge_and_shows_staff_its_sources(client):
    b = ai_business(client)
    out = inbound(b, "When are you open on Saturday?")
    conv = out["conversation"]
    assert conv["handler"] == "ai"
    run_jobs()
    m = msgs(b, conv["id"])
    assert [x["author_kind"] for x in m] == ["contact", "ai"]
    assert "Saturday from 9am to 1pm" in m[1]["body"]
    runs = client.get(f"{base(b)}/ai/conversations/{conv['id']}/runs", headers=b["agent"]["h"]).json()
    assert runs[0]["outcome"] == "replied" and str(runs[0]["reply_message_id"]) == str(m[1]["id"])
    assert runs[0]["sources"][0]["title"] == "Opening hours"
    assert runs[0]["sources"][0]["source_url"] == "https://example.org/help"
    # Usage is counted once per answered message.
    with db.tx() as conn:
        rows = conn.execute("SELECT * FROM usage_records WHERE customer_id = %s AND meter = 'ai_reply'", (b["id"],))
        rows = rows.fetchall()
    assert len(rows) == 1 and rows[0]["ref"] == str(out["message"]["id"])
    # A retried job never replies twice.
    with db.tx() as conn:
        jobs.enqueue(
            conn,
            "ai.respond",
            {"conversation_id": str(conv["id"]), "message_id": str(out["message"]["id"])},
            customer_id=b["id"],
        )
    run_jobs()
    assert len(msgs(b, conv["id"])) == 2


def test_unapproved_or_missing_knowledge_escalates_and_records_a_gap(client):
    b = ai_business(client, sources=((HOURS, False),))
    conv = inbound(b, "When are you open on Saturday? My email is ana@example.org")["conversation"]
    run_jobs()
    c = conv_row(b, conv["id"])
    assert c["handler"] == "none"
    m = msgs(b, conv["id"])
    assert m[-1]["author_kind"] == "system" and "member of our team" in m[-1]["body"]
    detail = client.get(f"{base(b)}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()
    packet = detail["handovers"][0]["packet"]
    assert "knowledge" in packet["why"] and packet["collected"]["email"] == "ana@example.org"
    assert packet["history"][0]["from"] == "customer" and packet["tried"]
    gaps = client.get(f"{base(b)}/ai/gaps", headers=b["agent"]["h"]).json()
    assert gaps[0]["reason"] == "missing" and gaps[0]["times"] == 1

    # The same question again counts against the same gap.
    conv2 = inbound(b, "When are you open on Saturday?  my email is ana@example.org", address="visitor-2")
    run_jobs()
    gaps = client.get(f"{base(b)}/ai/gaps", headers=b["agent"]["h"]).json()
    assert len(gaps) == 1 and gaps[0]["times"] == 2
    assert conv2["conversation"]["id"] != conv["id"]
    r = client.post(f"{base(b)}/ai/gaps/{gaps[0]['id']}/resolve", headers=b["agent"]["h"])
    assert r.status_code == 200 and client.get(f"{base(b)}/ai/gaps", headers=b["agent"]["h"]).json() == []


def test_contradictory_sources_escalate(client):
    other = "Weekend hours\n\nOn Saturday we are open from 10am to 2pm."
    b = ai_business(client, sources=((HOURS, True), (other, True)))
    conv = inbound(b, "What are your Saturday opening hours?")["conversation"]
    run_jobs()
    assert conv_row(b, conv["id"])["handler"] == "none"
    gaps = client.get(f"{base(b)}/ai/gaps", headers=b["agent"]["h"]).json()
    assert gaps[0]["reason"] == "contradictory" and "Weekend hours" in gaps[0]["detail"]
    assert not any(x["author_kind"] == "ai" for x in msgs(b, conv["id"]))


def test_asking_for_a_person_hands_over_without_calling_the_model(client):
    b = ai_business(client)
    cap = Capturing()
    runtime.set_model(cap)
    conv = inbound(b, "I want to speak to a person please")["conversation"]
    run_jobs()
    assert cap.inputs == [] and conv_row(b, conv["id"])["handler"] == "none"


# ---- acceptance test 2: notes never reach the customer AI ---------------------------


def test_private_notes_never_reach_the_customer_ai(client):
    b = ai_business(client)
    cap = Capturing()
    runtime.set_model(cap)
    first = inbound(b, "Hello")
    run_jobs()
    conv_id = first["conversation"]["id"]
    with db.tx() as conn:
        inbox.add_note(conn, b["id"], conv_id, author="agent", body="SECRET-NOTE-4471 customer is on a watch list")
    inbound(b, "How much is a replacement debit card?")
    run_jobs()
    assert len(cap.inputs) == 2
    assert "SECRET-NOTE-4471" not in cap.seen()
    for m in msgs(b, conv_id):
        assert "SECRET-NOTE-4471" not in m["body"]
    assert "25 dollars" in msgs(b, conv_id)[-1]["body"]

    # The copilot reads what staff may read: notes for a person who can read them...
    cap.inputs.clear()
    r = client.post(f"{base(b)}/ai/conversations/{conv_id}/copilot/summary", json={}, headers=b["internal"]["h"])
    assert r.status_code == 200, r.text
    assert "SECRET-NOTE-4471" in cap.seen()
    # ...but not for a key without commai:notes.
    cap.inputs.clear()
    key = api_key(client, b["agent"]["h"], ["commai:read"])
    r = client.post(f"{base(b)}/ai/conversations/{conv_id}/copilot/summary", json={}, headers=key)
    assert r.status_code == 200, r.text
    assert "SECRET-NOTE-4471" not in cap.seen()


# ---- acceptance test 3: a person taking over stops the AI ----------------------------


def test_takeover_before_the_job_runs_stops_the_ai(client):
    b = ai_business(client)
    out = inbound(b, "When are you open on Saturday?")
    conv_id = out["conversation"]["id"]
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM jobs WHERE kind = 'ai.respond' AND status = 'queued'").fetchone()
    r = client.post(f"{base(b)}/conversations/{conv_id}/takeover", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    run_jobs()
    m = msgs(b, conv_id)
    assert [x["author_kind"] for x in m] == ["contact"]  # context kept, nothing from the AI
    assert ai_runs(conv_id) == []
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM usage_records WHERE customer_id = %s", (b["id"],)).fetchone() is None
    c = conv_row(b, conv_id)
    assert c["handler"] == "human" and str(c["handler_user_id"]) == b["agent"]["id"]


def test_takeover_while_the_model_is_thinking_writes_nothing(client):
    b = ai_business(client)
    out = inbound(b, "When are you open on Saturday?")
    conv_id = out["conversation"]["id"]

    class TakesOverMidway(Capturing):
        def complete(self, inp):
            with db.tx() as other:  # a person takes over from another connection
                inbox.take_over(other, b["id"], conv_id, b["agent"]["id"], "user:agent")
            return super().complete(inp)

    runtime.set_model(TakesOverMidway())
    run_jobs()
    assert [x["author_kind"] for x in msgs(b, conv_id)] == ["contact"]
    assert ai_runs(conv_id) == []
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM usage_records WHERE customer_id = %s", (b["id"],)).fetchone() is None
        job = conn.execute("SELECT status FROM jobs WHERE kind = 'ai.respond'").fetchone()
    assert job["status"] == "done"
    assert conv_row(b, conv_id)["handler"] == "human"


# ---- acceptance test 4: bookings are confirmed only by the calendar -------------------


def _booking_text() -> tuple[str, dt.datetime]:
    when = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    text = (
        f"Hi, I'd like to book an appointment on {when.date().isoformat()} at 10:00. "
        "My name is Ana Lopez and my email is ana@example.org"
    )
    return text, when


def test_ai_booking_is_confirmed_only_after_the_calendar_succeeds(client):
    b = ai_business(client)
    connect_app(b["id"], "sim_calendar", ["book", "find_slots"])
    allow_tools(b["id"], "customer_agent", ["sim_calendar.book"])
    text, when = _booking_text()
    conv_id = inbound(b, text)["conversation"]["id"]

    with db.tx() as conn:
        conn.execute("UPDATE jobs SET run_after = now() WHERE status = 'queued'")
    jobs.run_pending(kinds=["ai.respond"])  # the AI only, not the calendar yet
    m = msgs(b, conv_id)
    assert m[-1]["author_kind"] == "ai"
    assert "confirmed" not in m[-1]["body"].lower() and "as soon as it is booked" in m[-1]["body"]
    with db.tx() as conn:
        run = conn.execute("SELECT * FROM action_runs WHERE conversation_id = %s", (conv_id,)).fetchone()
        assert conn.execute("SELECT 1 FROM sim_records WHERE customer_id = %s", (b["id"],)).fetchone() is None
    assert run["role"] == "customer_agent" and run["status"] == "approved"
    assert run["inputs"]["start"] == when.isoformat() and run["inputs"]["contact"] == "ana@example.org"

    run_jobs()  # the calendar books, then the customer hears it
    m = msgs(b, conv_id)
    assert m[-1]["author_kind"] == "system" and "confirmed" in m[-1]["body"]
    with db.tx() as conn:
        booking = conn.execute("SELECT * FROM sim_records WHERE customer_id = %s", (b["id"],)).fetchone()
        run = conn.execute("SELECT * FROM action_runs WHERE id = %s", (run["id"],)).fetchone()
    assert run["status"] == "succeeded" and str(booking["id"]) in m[-1]["body"]
    runs = ai_runs(conv_id)
    assert runs[0]["outcome"] == "proposed" and runs[0]["tool_calls"][0]["tool"] == "sim_calendar.book"


def test_a_failing_calendar_never_confirms_and_hands_over(client):
    b = ai_business(client)
    connect_app(b["id"], "sim_calendar", ["book"], settings={"simulate_failure": "permission"})
    allow_tools(b["id"], "customer_agent", ["sim_calendar.book"])
    text, _ = _booking_text()
    conv_id = inbound(b, text)["conversation"]["id"]
    run_jobs()
    m = msgs(b, conv_id)
    assert not any("confirmed" in x["body"].lower() for x in m)
    assert m[-1]["author_kind"] == "system" and "member of our team" in m[-1]["body"]
    c = conv_row(b, conv_id)
    assert c["handler"] == "none"
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM sim_records WHERE customer_id = %s", (b["id"],)).fetchone() is None
        h = conn.execute("SELECT * FROM handovers WHERE conversation_id = %s", (conv_id,)).fetchone()
    assert "book failed" in h["reason"]


def test_booking_asks_for_missing_details_first(client):
    b = ai_business(client)
    connect_app(b["id"], "sim_calendar", ["book"])
    allow_tools(b["id"], "customer_agent", ["sim_calendar.book"])
    conv_id = inbound(b, "Can I book an appointment please?")["conversation"]["id"]
    run_jobs()
    assert "day and time" in msgs(b, conv_id)[-1]["body"]
    _, when = _booking_text()
    inbound(b, f"{when.date().isoformat()} at 10am. I'm Ana, ana@example.org")
    run_jobs()
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM sim_records WHERE customer_id = %s", (b["id"],)).fetchone()
    assert "confirmed" in msgs(b, conv_id)[-1]["body"]


def test_tools_not_listed_for_the_role_are_refused_by_the_service(client):
    """The model asks to book anyway: the action service refuses it."""
    b = ai_business(client)
    connect_app(b["id"], "sim_calendar", ["book"])  # switched on, but not listed for the customer agent
    _, when = _booking_text()

    class Rogue:
        name = "rogue"

        def complete(self, inp):
            return ModelOutput(
                answer="Booking now.",
                intent="booking",
                tool_calls=[
                    {
                        "tool": "sim_calendar.book",
                        "inputs": {"start": when.isoformat(), "name": "Ana", "contact": "a@example.org"},
                    }
                ],
            )

    runtime.set_model(Rogue())
    conv_id = inbound(b, "book me in")["conversation"]["id"]
    run_jobs()
    with db.tx() as conn:
        run = conn.execute("SELECT * FROM action_runs WHERE conversation_id = %s", (conv_id,)).fetchone()
        assert conn.execute("SELECT 1 FROM sim_records WHERE customer_id = %s", (b["id"],)).fetchone() is None
    assert run["status"] == "rejected" and "not allowed" in run["error"]
    assert conv_row(b, conv_id)["handler"] == "none"
    assert not any(x["author_kind"] == "ai" for x in msgs(b, conv_id))


# ---- acceptance test 8: when the AI fails, a person gets the conversation -------------


@pytest.mark.parametrize("exc", [ModelError("The AI service answered 503."), RuntimeError("boom")])
def test_model_failure_goes_to_a_person_with_a_holding_reply(client, exc):
    b = ai_business(client)
    team = client.post(
        f"{base(b)}/teams", json={"name": "Front desk", "members": [b["agent"]["id"]]}, headers=b["agent"]["h"]
    ).json()
    client.post(
        f"{base(b)}/routing-rules", json={"name": "all", "match": {}, "team_id": team["id"]}, headers=b["agent"]["h"]
    )
    runtime.set_model(Failing(exc))
    out = inbound(b, "When are you open on Saturday?")
    conv_id = out["conversation"]["id"]
    run_jobs()
    c = conv_row(b, conv_id)
    assert c["handler"] == "none" and str(c["assignee_id"]) == b["agent"]["id"]
    m = msgs(b, conv_id)
    assert [x["author_kind"] for x in m] == ["contact", "system"]
    assert m[0]["body"] == "When are you open on Saturday?"
    assert "member of our team" in m[1]["body"]
    with db.tx() as conn:
        h = conn.execute("SELECT * FROM handovers WHERE conversation_id = %s", (conv_id,)).fetchone()
        ev = conn.execute("SELECT * FROM commai_events WHERE type = 'ai.failed'").fetchone()
        job = conn.execute("SELECT status FROM jobs WHERE kind = 'ai.respond'").fetchone()
    assert "could not answer" in h["reason"] and h["packet"]["history"][0]["text"] == m[0]["body"]
    assert ev is not None and job["status"] == "done"
    assert ai_runs(conv_id)[0]["outcome"] == "failed"


def test_hard_usage_limit_hands_over(client):
    b = ai_business(client)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO usage_limits (customer_id, meter, monthly_hard) VALUES (%s, 'ai_reply', 0)", (b["id"],)
        )
    conv_id = inbound(b, "When are you open on Saturday?")["conversation"]["id"]
    run_jobs()
    assert conv_row(b, conv_id)["handler"] == "none"
    assert "limit" in ai_runs(conv_id)[0]["reason"]
    assert msgs(b, conv_id)[-1]["author_kind"] == "system"


# ---- memory and identity --------------------------------------------------------------


def test_memory_is_used_only_for_verified_identities(client):
    b = ai_business(client)
    cap = Capturing()
    runtime.set_model(cap)
    # Verified: the business's own sign-in vouched for them.
    v = inbound(b, "Hello", address="signed-in:ana", verified=True, name="Ana")
    run_jobs()
    contact_id = str(v["conversation"]["contact_id"])
    r = client.patch(f"{base(b)}/contacts/{contact_id}", json={"email": "ana@example.org"}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    r = client.post(
        f"{base(b)}/ai/contacts/{contact_id}/memory",
        json={"fact": "Banks at the Port of Spain branch"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    inbound(b, "I prefer email to phone calls. When are you open on Saturday?", address="signed-in:ana", verified=True)
    run_jobs()
    ctx = cap.inputs[-1].context
    assert ctx["verified"] and "Banks at the Port of Spain branch" in ctx["memory"]
    assert ctx["contact"]["email"] == "ana@example.org"
    facts = client.get(f"{base(b)}/ai/contacts/{contact_id}/memory", headers=b["agent"]["h"]).json()
    assert any(f["fact"].startswith("Prefers email") and f["source"] == "ai" for f in facts["facts"])

    # Same contact reached through an unverified identity: no records, no memory.
    with db.tx() as conn:
        inbox.find_or_create_identity(conn, b["id"], "web", "anon-ana", contact_id=contact_id)
    cap.inputs.clear()
    inbound(b, "I prefer text messages. When are you open on Saturday?", address="anon-ana")
    run_jobs()
    seen = cap.seen()
    assert "Port of Spain branch" not in seen and "ana@example.org" not in seen
    assert cap.inputs[-1].context["verified"] is False
    facts = client.get(f"{base(b)}/ai/contacts/{contact_id}/memory", headers=b["agent"]["h"]).json()["facts"]
    assert not any("text messages" in f["fact"] for f in facts)

    # Staff can delete what the AI remembers.
    fid = facts[0]["id"]
    assert client.delete(f"{base(b)}/ai/contacts/{contact_id}/memory/{fid}", headers=b["agent"]["h"]).status_code == 204
    assert client.delete(f"{base(b)}/ai/contacts/{contact_id}/memory", headers=b["agent"]["h"]).status_code == 204
    assert client.get(f"{base(b)}/ai/contacts/{contact_id}/memory", headers=b["agent"]["h"]).json()["facts"] == []


# ---- languages ------------------------------------------------------------------------


def test_reply_in_the_customers_language_keeps_the_original(client):
    b = ai_business(client)

    class Translating:
        name = "translating"

        def complete(self, inp):
            hit = inp.context["knowledge"]["hits"][0]
            return ModelOutput(
                answer="Abrimos los sábados de 9 a 13 h.",
                answer_original="We open on Saturdays from 9am to 1pm.",
                language="es",
                intent="question",
                sources=[hit["chunk_id"]],
            )

    runtime.set_model(Translating())
    conv_id = inbound(b, "Hola, ¿cuándo abren los sábados? Saturday opening hours")["conversation"]["id"]
    run_jobs()
    m = msgs(b, conv_id)[-1]
    assert m["body"].startswith("Abrimos") and m["original_body"].startswith("We open")
    assert m["original_language"] == "en"
    assert conv_row(b, conv_id)["language"] == "es"


def test_without_a_model_the_reply_says_honestly_it_is_in_english(client):
    b = ai_business(client)
    conv_id = inbound(b, "Hola, los opening hours?")["conversation"]["id"]
    run_jobs()
    m = msgs(b, conv_id)[-1]
    assert m["author_kind"] == "ai" and m["body"].startswith("Disculpe, por ahora solo puedo responder en inglés")
    assert "8am to 4pm" in m["body"]


# ---- copilot --------------------------------------------------------------------------


def test_copilot_helps_but_never_sends(client):
    b = ai_business(client)
    b_mode = client.patch(f"{base(b)}/settings", json={"mode": "human_first"}, headers=b["agent"]["h"])
    assert b_mode.status_code == 200
    conv_id = inbound(b, "How much is a replacement debit card? I'd also like to book a visit.")["conversation"]["id"]
    before = len(msgs(b, conv_id))
    u = f"{base(b)}/ai/conversations/{conv_id}/copilot"
    h = b["agent"]["h"]
    s = client.post(f"{u}/summary", json={}, headers=h).json()
    assert "replacement debit card" in s["summary"]
    d = client.post(f"{u}/draft", json={}, headers=h).json()
    assert "25 dollars" in d["draft"] and d["sources"][0]["title"] == "Card fees"
    t = client.post(f"{u}/translate", json={"text": "Hola, quiero una cita para mañana"}, headers=h).json()
    assert t["translated"] is False and t["text"] == t["original"] and t["language"] == "es"
    miss = client.post(f"{u}/missing", json={}, headers=h).json()
    assert "The day and time they want to book" in miss["items"]
    nxt = client.post(f"{u}/next_steps", json={}, headers=h).json()
    assert "Reply to the customer's latest message" in nxt["items"]
    assert client.post(f"{u}/nonsense", json={}, headers=h).status_code == 422
    assert len(msgs(b, conv_id)) == before  # nothing went out
    with db.tx() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM usage_records WHERE customer_id = %s AND meter = 'copilot'", (b["id"],)
        ).fetchone()["n"]
        audits = conn.execute("SELECT count(*) AS n FROM audit_log WHERE action LIKE 'commai.ai.copilot.%%'").fetchone()
    assert n == 5 and audits["n"] == 5


# ---- settings APIs --------------------------------------------------------------------


def test_role_tools_and_profile_api(client):
    b = ai_business(client)
    u = base(b)
    h = b["agent"]["h"]
    roles = client.get(f"{u}/ai/roles", headers=h).json()
    assert {r["role"] for r in roles["roles"]} == {"customer_agent", "copilot", "platform_assistant"}
    assert any(t["tool"] == "sim_calendar.book" for t in roles["catalogue"])
    r = client.put(f"{u}/ai/roles/customer_agent/tools", json={"tools": ["sim_calendar.book", "sim_crm.*"]}, headers=h)
    assert r.status_code == 200 and r.json()["tools"] == ["sim_calendar.book", "sim_crm.*"]
    assert client.put(f"{u}/ai/roles/customer_agent/tools", json={"tools": ["nope.x"]}, headers=h).status_code == 422
    r = client.put(f"{u}/ai/roles/copilot/tools", json={"tools": ["sim_calendar.book"]}, headers=h)
    assert r.status_code == 422  # the copilot never creates anything
    assert client.put(f"{u}/ai/roles/x/tools", json={"tools": []}, headers=h).status_code == 404
    r = client.put(f"{u}/ai/roles/customer_agent/tools", json={"tools": []}, headers=b["internal"]["h"])
    assert r.status_code == 403

    connect_app(b["id"], "sim_calendar", ["book"])
    roles = client.get(f"{u}/ai/roles", headers=h).json()
    agent = next(r for r in roles["roles"] if r["role"] == "customer_agent")
    assert agent["usable"] == ["sim_calendar.book"]

    r = client.put(
        f"{u}/ai/profile",
        json={
            "name": "Ava",
            "greeting": "Hi, I'm Ava from Example Bank.",
            "escalation": {"keywords": ["lawyer"], "max_ai_replies": 3},
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Ava" and r.json()["escalation"]["keywords"] == ["lawyer"]
    assert r.json()["tone"] == runtime.DEFAULT_PROFILE["tone"]
    conv_id = inbound(b, "When are you open on Saturday?")["conversation"]["id"]
    run_jobs()
    m = msgs(b, conv_id)[-1]
    assert m["body"].startswith("Hi, I'm Ava from Example Bank.") and m["author"] == "Ava"
    status = client.get(f"{u}/ai/status", headers=h).json()
    assert status["model"] == "simulated" and status["live"] is False and status["ai_available"] is True
    with db.tx() as conn:
        acts = {r["action"] for r in conn.execute("SELECT action FROM audit_log").fetchall()}
    assert {"commai.ai.role_tools.set", "commai.ai.profile.update", "commai.ai.knowledge.create"} <= acts

    r = client.put(f"{u}/ai/profile", json={"enabled": False}, headers=h)
    conv2 = inbound(b, "When are you open on Saturday?", address="visitor-9")["conversation"]["id"]
    run_jobs()
    assert conv_row(b, conv2)["handler"] == "none"


def test_knowledge_api_approval_and_search(client):
    b = business(client)
    u = base(b)
    h = b["agent"]["h"]
    long_body = "\n\n".join(f"Paragraph {i}. " + ("Our branches offer many services. " * 20) for i in range(4))
    r = client.post(f"{u}/ai/knowledge", json={"title": "Services", "body": long_body}, headers=h)
    assert r.status_code == 201 and r.json()["approved"] is False and r.json()["chunks"] > 1
    sid = r.json()["id"]
    assert client.get(f"{u}/ai/knowledge/search?q=branch services", headers=h).json()["hits"] == []
    assert client.post(f"{u}/ai/knowledge/{sid}/approve", json={"approved": True}, headers=h).json()["approved"]
    assert client.get(f"{u}/ai/knowledge/search?q=branch services", headers=h).json()["hits"]
    r = client.patch(f"{u}/ai/knowledge/{sid}", json={"body": "Mortgages are reviewed within ten days."}, headers=h)
    assert r.status_code == 200 and r.json()["approved"] is False  # edited: approve again
    assert client.get(f"{u}/ai/knowledge/{sid}", headers=h).json()["chunks"][0]["text"].startswith("Mortgages")
    r = client.post(f"{u}/ai/knowledge", json={"title": "x", "body": "y", "source_url": "ftp://x"}, headers=h)
    assert r.status_code == 422
    assert (
        client.post(f"{u}/ai/knowledge", json={"title": "x", "body": "y"}, headers=b["internal"]["h"]).status_code
        == 403
    )
    assert client.delete(f"{u}/ai/knowledge/{sid}", headers=h).status_code == 204
    assert client.get(f"{u}/ai/knowledge", headers=h).json() == []
    # Another business can't see or touch it.
    other = business(client, "Other Bank")
    assert client.get(f"{u}/ai/knowledge", headers=other["agent"]["h"]).status_code == 403


def test_add_source_function_for_onboarding(client):
    b = business(client)
    with db.tx() as conn:
        src = knowledge.add_source(
            conn,
            b["id"],
            title="Parking",
            body="Free parking behind the branch.",
            source_url="https://example.org/parking",
            approved=True,
            created_by="onboarding",
        )
        found = knowledge.lookup(conn, b["id"], "Is there parking?")
    assert src["chunks"] == 1 and src["approved"] and src["approved_by"] == "onboarding"
    assert found["hits"][0]["title"] == "Parking"


# ---- voice stage 1: browser calls -------------------------------------------------------


def test_browser_call_with_the_ai_agent(client):
    b = ai_business(client)
    client.patch(f"{base(b)}/settings", json={"mode": "human_first"}, headers=b["agent"]["h"])
    u = f"{base(b)}/ai/calls"
    h = b["agent"]["h"]
    r = client.post(u, json={"caller": "Test caller"}, headers=h)
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["handler"] == "ai" and "AI assistant" in call["greeting"]
    cid = call["conversation_id"]
    r = client.post(f"{u}/{cid}/turns", json={"text": "When are you open on Saturday?"}, headers=h)
    assert r.status_code == 200, r.text
    assert "9am to 1pm" in r.json()["reply"] and r.json()["handed_over"] is False
    r = client.post(f"{u}/{cid}/turns", json={"text": "Can I talk to a person?"}, headers=h)
    assert r.json()["handed_over"] is True and "member of our team" in r.json()["reply"]
    rec = client.get(f"{u}/{cid}", headers=h).json()
    assert [t["from"] for t in rec["transcript"]] == ["ai", "customer", "ai", "customer", "business"]
    assert rec["turns"] == 2 and rec["handler"] == "none"
    detail = client.get(f"{base(b)}/conversations/{cid}", headers=h).json()
    assert detail["channel"] == "voice"
    ended = client.post(f"{u}/{cid}/end", headers=h).json()
    assert ended["ended_at"] is not None
    assert client.post(f"{u}/{cid}/turns", json={"text": "hello?"}, headers=h).status_code == 409
    with db.tx() as conn:
        q = conn.execute(
            "SELECT quantity FROM usage_records WHERE customer_id = %s AND meter = 'ai_voice_minute'", (b["id"],)
        ).fetchone()
    assert float(q["quantity"]) == 1
    sp = client.get(f"{base(b)}/ai/speech", headers=h).json()
    assert sp["browser"] is True and sp["server"] is False and "not live" in sp["note"]
    # The job queued for each caller turn finds the turn answered and does nothing.
    n = len(rec["transcript"])
    run_jobs()
    assert len(client.get(f"{u}/{cid}", headers=h).json()["transcript"]) == n


def test_model_contract_parsing():
    from exaconnect_controller.commai.ai.model import _parse_json, output_from

    raw = (
        '<think>hmm</think>```json\n{"answer": "Hi", "sources": [3, "4", "x"], "tool_calls": [{"tool": "a.b"}],'
        ' "escalate": false}\n```'
    )
    out = output_from(_parse_json(raw), "m")
    assert out.answer == "Hi" and out.sources == [3, 4] and out.tool_calls == [{"tool": "a.b", "inputs": {}}]
    with pytest.raises(ModelError):
        _parse_json("no json here")
    assert json.loads(ModelInput("customer_agent", "reply", "s", {"a": 1}).as_json())["context"] == {"a": 1}
