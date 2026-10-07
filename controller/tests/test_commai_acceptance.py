"""The ten Jibsy acceptance tests of the scope document ("Jibsy by ExaCarib
architecture and scope", Acceptance tests), end to end.

Each test drives the product through its public surfaces: the website widget
and provider webhooks for customers, the REST API for staff and API keys, and
the simulated providers (simulated WhatsApp, the simulated calendar and CRM,
the simulated AI model) where a real one would sit. The database is read only
to look at what a simulated provider recorded as sent, never to arrange the
outcome under test.

Real-provider boundary: no test calls a real carrier, Meta, calendar or model.
Each test goes up to the provider interface (the simulated provider records
exactly what would be handed to the real one); the real adapters' signing and
parsing are tested offline in test_commai_channels and
test_commai_webhook_security.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import jobs
from exaconnect_controller.commai.ai import runtime
from exaconnect_controller.commai.ai.model import ModelError, SimulatedModel
from exaconnect_controller.commai.channels import providers

from .commai_helpers import api_key, base, business, run_jobs

SITE = "https://www.examplebank.tt"
CUSTOMER_WA = "+18685550101"
HOURS = (
    "Opening hours",
    "We are open Monday to Friday from 8am to 4pm, and on Saturday from 9am to 1pm. We are closed on public holidays.",
)
FEES = ("Card fees", "A replacement debit card costs 25 dollars and arrives within five working days.")


@pytest.fixture(autouse=True)
def simulated_model():
    runtime.set_model(SimulatedModel())
    yield
    runtime.set_model(None)


# ---- the business, as its staff set it up through the API ----------------------------------------------


class Shop:
    """A business with a team, a routing rule per channel, a widget key and a
    simulated WhatsApp number, all made through the API."""

    def __init__(self, client, name="Example Bank", ai=False, people=("agent", "agent2", "internal")):
        self.c = client
        self.b = business(client, name, people)
        self.u = base(self.b)
        self.h = self.b["agent"]["h"]
        if ai:
            self.ok(self.c.patch(f"{self.u}/settings", json={"mode": "ai_first"}, headers=self.h))
            for title, body in (HOURS, FEES):
                self.ok(
                    self.c.post(
                        f"{self.u}/ai/knowledge",
                        json={"title": title, "body": body, "approved": True, "source_url": "https://example.org/help"},
                        headers=self.h,
                    )
                )
        team = self.ok(
            self.c.post(
                f"{self.u}/teams", json={"name": "Front desk", "members": [self.b["agent"]["id"]]}, headers=self.h
            )
        )
        self.team_id = team["id"]
        self.ok(
            self.c.post(
                f"{self.u}/routing-rules",
                json={"name": "Everything", "match": {}, "team_id": self.team_id},
                headers=self.h,
            )
        )
        self.key = self.ok(
            self.c.post(f"{self.u}/widget-keys", json={"name": "Main site", "allowed_origins": [SITE]}, headers=self.h)
        )
        self.wa = self.ok(
            self.c.post(
                f"{self.u}/channel-accounts",
                json={"channel": "whatsapp", "provider": "simulated", "address": "+18685550100", "name": "WhatsApp"},
                headers=self.h,
            )
        )

    @staticmethod
    def ok(r):
        assert r.status_code in (200, 201, 202), r.text
        return r.json()

    @property
    def id(self):
        return self.b["id"]

    def whatsapp_in(self, body, msg_id, frm=CUSTOMER_WA):
        raw = json.dumps({"messages": [{"id": msg_id, "from": frm, "name": "Ana", "body": body}]}).encode()
        path = "/api/v1" + self.wa["webhook_url"].split("/api/v1", 1)[1]
        sig = providers.Simulated.sign(self.wa["secret"], raw)
        return self.c.post(path, content=raw, headers={"Content-Type": "application/json", "X-Exa-Signature": sig})

    def conversations(self, **params):
        return self.ok(self.c.get(f"{self.u}/conversations", params=params, headers=self.h))["items"]

    def conversation(self, conv_id, headers=None):
        return self.ok(self.c.get(f"{self.u}/conversations/{conv_id}", headers=headers or self.h))

    def reply(self, conv_id, body, who="agent", **extra):
        return self.c.post(
            f"{self.u}/conversations/{conv_id}/messages", json={"body": body, **extra}, headers=self.b[who]["h"]
        )

    def outbox(self):
        return self.ok(self.c.get(f"{self.u}/channels/outbox", headers=self.h))

    def events(self, type_=None) -> list[dict]:
        out, after = [], 0
        while True:
            params = {"after": after, "limit": 500, **({"type": type_} if type_ else {})}
            page = self.ok(self.c.get(f"{self.u}/events", params=params, headers=self.h))
            out += page["items"]
            if not page["next"]:
                return out
            after = page["next"]

    def connect_calendar(self, actions=("find_slots", "book", "cancel")):
        self.ok(self.c.post(f"{self.u}/integrations/sim_calendar/connect", headers=self.h))
        self.ok(
            self.c.put(f"{self.u}/integrations/sim_calendar/actions", json={"actions": list(actions)}, headers=self.h)
        )
        assert self.ok(self.c.post(f"{self.u}/integrations/sim_calendar/test", headers=self.h))["test"]["ok"]
        assert self.ok(self.c.post(f"{self.u}/integrations/sim_calendar/approve", headers=self.h))["status"] == "live"

    def allow_ai_tools(self, tools):
        self.ok(self.c.put(f"{self.u}/ai/roles/customer_agent/tools", json={"tools": tools}, headers=self.h))


class Visitor:
    """A browser on the business's website running the widget."""

    def __init__(self, client, key):
        self.c = client
        self.u = f"/api/v1/commai/widget/{key['public_key']}"
        r = client.post(f"{self.u}/session", json={}, headers={"Origin": SITE})
        assert r.status_code == 200, r.text
        self.token = r.json()["token"]
        self.conv: str | None = None

    def h(self):
        return {"Origin": SITE, "X-Widget-Session": self.token}

    def say(self, body, client_id=None):
        payload = {"body": body, "client_id": client_id or f"c{time.monotonic_ns()}"}
        if self.conv:
            payload["conversation_id"] = self.conv
        r = self.c.post(f"{self.u}/messages", json=payload, headers=self.h())
        assert r.status_code == 201, r.text
        self.conv = r.json()["conversation_id"]
        return r.json()

    def seen(self) -> list[tuple[str, str]]:
        r = self.c.get(f"{self.u}/messages", params={"conversation_id": self.conv}, headers=self.h())
        assert r.status_code == 200, r.text
        return [(m["from"], m["body"]) for m in r.json()["items"]]

    def raw(self) -> str:
        return "".join(
            self.c.get(f"{self.u}{p}", params={"conversation_id": self.conv}, headers=self.h()).text
            for p in ("/messages", "/conversations")
        )


def _ai_jobs_only():
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET run_after = now() WHERE status = 'queued'")
    jobs.run_pending(kinds=["ai.respond"])


def _booking_text() -> tuple[str, dt.datetime]:
    when = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return (
        f"Hi, I'd like to book an appointment on {when.date().isoformat()} at 10:00. "
        "My name is Ana Lopez and my email is ana@example.org",
        when,
    )


# ==== 1 ==================================================================================================


def test_acceptance_01_enquiry_reaches_inbox_is_assigned_and_answered_on_its_channel(client):
    """1. A website or WhatsApp enquiry reaches the inbox, is assigned and gets a
    reply on its original channel.

    WhatsApp goes up to the provider boundary: the reply is handed to the
    simulated WhatsApp provider (its outbox), where the real adapter would call
    Twilio or 360dialog."""
    s = Shop(client)
    v = Visitor(client, s.key)
    v.say("Hello, my card was declined")
    assert s.whatsapp_in("Do you open on Saturday?", "wamid.A1").status_code == 200

    mine = {c["channel"]: c for c in s.conversations(view="mine")}
    assert set(mine) == {"web", "whatsapp"}
    for c in mine.values():
        assert c["assignee_id"] == s.b["agent"]["id"] and c["team_id"] == s.team_id

    assert s.reply(mine["web"]["id"], "Let me check that card.").status_code == 201
    assert s.reply(mine["whatsapp"]["id"], "Yes, 9 to 1 on Saturdays.").status_code == 201
    run_jobs()

    # The website reply is in the widget, and only there.
    assert v.seen() == [("you", "Hello, my card was declined"), ("team", "Let me check that card.")]
    # The WhatsApp reply went to the customer's WhatsApp number, and only there.
    assert [(o["channel"], o["to_address"], o["body"]) for o in s.outbox()] == [
        ("whatsapp", CUSTOMER_WA, "Yes, 9 to 1 on Saturdays.")
    ]
    wa = s.conversation(mine["whatsapp"]["id"])
    assert [m["direction"] for m in wa["messages"]] == ["in", "out"] and wa["messages"][1]["status"] == "sent"


# ==== 2 ==================================================================================================


def test_acceptance_02_notes_unreachable_by_widget_channels_keys_exports_and_customer_ai(client):
    """2. Internal notes are unreachable through the widget, channels, customer
    API keys, exports and the customer AI."""
    secret = f"NOTE-{uuid.uuid4().hex[:8]}"

    class Capturing(SimulatedModel):
        seen: list[str] = []

        def complete(self, inp):
            Capturing.seen.append(inp.system + inp.as_json())
            return super().complete(inp)

    runtime.set_model(Capturing())
    s = Shop(client, ai=True)
    v = Visitor(client, s.key)
    v.say("Hello")
    s.whatsapp_in("Hi there", "wamid.N1")
    run_jobs()
    convs = {c["channel"]: c["id"] for c in s.conversations()}
    for ch, cid in convs.items():
        r = client.post(
            f"{s.u}/conversations/{cid}/notes", json={"body": f"{secret} {ch}"}, headers=s.b["internal"]["h"]
        )
        assert r.status_code == 201, r.text
        assert s.reply(cid, f"Reply on {ch}", take_over=True).status_code == 201
    # The customer writes again; the AI is handed the conversation again after a handback.
    client.post(f"{s.u}/conversations/{convs['web']}/handback", headers=s.h)
    v.say("How much is a replacement debit card?")
    run_jobs()

    # Widget.
    assert secret not in v.raw() and "Reply on web" in v.raw()
    # Channels: everything handed to the WhatsApp provider.
    sent = s.outbox()
    assert sent and secret not in json.dumps(sent)
    # Customer-facing API key: no notes endpoint, nothing in conversations, messages or exports.
    key = api_key(client, s.h, ["commai:read", "commai:write", "commai:admin"])
    for cid in convs.values():
        assert client.get(f"{s.u}/conversations/{cid}/notes", headers=key).status_code == 403
        for path in (f"/conversations/{cid}", f"/conversations/{cid}/messages", f"/conversations/{cid}/export"):
            r = client.get(s.u + path, headers=key)
            assert r.status_code == 200 and secret not in r.text, path
    for path in ("/exports/conversations.csv", "/events?limit=500"):
        r = client.get(s.u + path, headers=key)
        assert r.status_code == 200 and secret not in r.text, path
    contact = s.conversation(convs["web"])["contact_id"]
    r = client.get(f"{s.u}/data/contacts/{contact}/export", headers=key)
    assert r.status_code == 200 and secret not in r.text
    assert client.get(f"{s.u}/data/contacts/{contact}/export?include_notes=true", headers=key).status_code == 403
    assert client.post(f"{s.u}/data/exports", json={"include_notes": True}, headers=key).status_code == 403
    # The customer AI never saw a note.
    assert len(Capturing.seen) >= 2 and not any(secret in x for x in Capturing.seen)
    # Staff with notes rights do see them (the note exists).
    staff = client.get(f"{s.u}/conversations/{convs['web']}/notes", headers=s.b["internal"]["h"]).json()
    assert staff[0]["body"].startswith(secret)


# ==== 3 ==================================================================================================


def test_acceptance_03_human_takeover_stops_ai_and_keeps_context(client):
    """3. A human taking over stops AI replies and keeps the context."""
    s = Shop(client, ai=True)
    v = Visitor(client, s.key)
    v.say("When are you open on Saturday?")
    run_jobs()
    assert v.seen()[-1][0] == "assistant" and "9am to 1pm" in v.seen()[-1][1]
    conv = v.conv

    v.say("And how much is a replacement debit card?")  # the AI's job is queued...
    r = client.post(f"{s.u}/conversations/{conv}/takeover", headers=s.h)  # ...and a person takes over first
    assert r.status_code == 200 and r.json()["handler"] == "human"
    run_jobs()
    v.say("Hello?")
    run_jobs()
    assert [w for w, _ in v.seen()] == ["you", "assistant", "you", "you"]  # no AI reply after the takeover

    detail = s.conversation(conv)
    assert detail["handler"] == "human" and detail["handler_user_id"] == s.b["agent"]["id"]
    assert [m["body"] for m in detail["messages"]][:3] == [
        "When are you open on Saturday?",
        v.seen()[1][1],
        "And how much is a replacement debit card?",
    ]
    runs = s.ok(client.get(f"{s.u}/ai/conversations/{conv}/runs", headers=s.h))
    assert len(runs) == 1  # the AI ran once, before the takeover
    # The person carries on in the same conversation, with the whole history.
    assert s.reply(conv, "A replacement card is 25 dollars.").status_code == 201
    assert v.seen()[-1] == ("team", "A replacement card is 25 dollars.")


# ==== 4 ==================================================================================================


def test_acceptance_04_booking_confirmed_only_after_the_calendar_reports_success(client):
    """4. A booking is confirmed to the customer only after the calendar reports
    success.

    The calendar is the simulated calendar app (the boundary where Google or
    Microsoft would be called)."""
    s = Shop(client, ai=True)
    s.connect_calendar()
    s.allow_ai_tools(["sim_calendar.book"])
    text, when = _booking_text()
    v = Visitor(client, s.key)
    v.say(text)

    _ai_jobs_only()  # the AI has answered; the calendar has not run yet
    assert not any("confirmed" in body.lower() for _, body in v.seen())
    runs = s.ok(client.get(f"{s.u}/actions", params={"conversation_id": v.conv}, headers=s.h))
    assert [r["status"] for r in runs] == ["approved"] and runs[0]["inputs"]["start"] == when.isoformat()

    run_jobs()  # the calendar books, then (and only then) the customer is told
    confirmations = [body for who, body in v.seen() if "confirmed" in body.lower()]
    assert len(confirmations) == 1
    run = s.ok(client.get(f"{s.u}/actions/{runs[0]['id']}", headers=s.h))
    assert run["status"] == "succeeded"
    assert len(s.events("booking.confirmed")) == 1

    # A calendar that fails never confirms; a person gets the conversation.
    s.ok(
        client.put(
            f"{s.u}/integrations/sim_calendar/settings",
            json={"settings": {"simulate_failure": "permission"}},
            headers=s.h,
        )
    )
    v2 = Visitor(client, s.key)
    v2.say(text.replace("Ana Lopez", "Bo Smith").replace("ana@", "bo@"))
    run_jobs()
    assert not any("confirmed" in body.lower() for _, body in v2.seen())
    assert "member of our team" in v2.seen()[-1][1]
    assert s.conversation(v2.conv)["handler"] == "none"
    assert len(s.events("booking.confirmed")) == 1


# ==== 5 ==================================================================================================


def test_acceptance_05_repeated_events_and_retries_create_no_duplicates(client):
    """5. Repeated events and retries create no duplicate messages or bookings."""
    s = Shop(client)
    for _ in range(3):  # the provider retries its webhook
        assert s.whatsapp_in("Same message", "wamid.R1").status_code == 200
    v = Visitor(client, s.key)
    first = v.say("Retry me", client_id="client-msg-0001")
    again = v.say("Retry me", client_id="client-msg-0001")  # the browser retries
    assert again["duplicate"] and again["message"]["id"] == first["message"]["id"]

    wa = next(c for c in s.conversations() if c["channel"] == "whatsapp")
    h = {**s.h, "Idempotency-Key": "reply-1"}
    for _ in range(2):  # the agent's app retries the reply
        r = client.post(f"{s.u}/conversations/{wa['id']}/messages", json={"body": "Once"}, headers=h)
        assert r.status_code == 201
    run_jobs()
    with db.tx() as conn:  # the worker crashed after sending and the send job runs again
        conn.execute("UPDATE jobs SET status = 'queued', run_after = now() WHERE kind = 'message.send'")
    run_jobs()

    # A booking requested twice with the same Idempotency-Key, and its job retried.
    s.connect_calendar()
    start = (dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
    hb = {**s.h, "Idempotency-Key": "book-1"}
    body = {
        "app": "sim_calendar",
        "action": "book",
        "inputs": {"start": start.isoformat(), "name": "Ana", "contact": "a@x.org"},
    }
    ids = {client.post(f"{s.u}/actions", json=body, headers=hb).json()["id"] for _ in range(2)}
    assert len(ids) == 1
    run_jobs()
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'queued', run_after = now() WHERE kind = 'action.execute'")
    run_jobs()

    assert len(s.ok(client.get(f"{s.u}/conversations/{wa['id']}/messages", headers=s.h))) == 2
    assert [m["body"] for m in s.ok(client.get(f"{s.u}/conversations/{v.conv}/messages", headers=s.h))] == ["Retry me"]
    assert [o["body"] for o in s.outbox()] == ["Once"]
    assert len(s.events("message.received")) == 2
    assert s.ok(client.get(f"{s.u}/actions/{ids.pop()}", headers=s.h))["status"] == "succeeded"
    with db.tx() as conn:  # what the simulated calendar holds
        assert (
            conn.execute("SELECT count(*) AS n FROM sim_records WHERE customer_id = %s", (s.id,)).fetchone()["n"] == 1
        )


# ==== 6 ==================================================================================================


def test_acceptance_06_broken_integration_shows_status_evidence_and_safe_recovery(client):
    """6. A broken integration shows a clear status, the evidence and a safe way
    to recover."""
    s = Shop(client)
    s.connect_calendar()
    start = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    def book(hour):
        inputs = {"start": start.replace(hour=hour).isoformat(), "name": "Ana", "contact": "a@x.org"}
        r = s.ok(
            client.post(f"{s.u}/actions", json={"app": "sim_calendar", "action": "book", "inputs": inputs}, headers=s.h)
        )
        run_jobs()
        return s.ok(client.get(f"{s.u}/actions/{r['id']}", headers=s.h))

    assert book(9)["status"] == "succeeded"
    assert s.ok(client.get(f"{s.u}/integrations/sim_calendar/health", headers=s.h))["level"] == "healthy"

    s.ok(
        client.put(
            f"{s.u}/integrations/sim_calendar/settings",
            json={"settings": {"simulate_failure": "expired_signin"}},
            headers=s.h,
        )
    )
    failed = book(10)
    assert failed["status"] == "failed"
    health = s.ok(client.get(f"{s.u}/integrations/sim_calendar/health", headers=s.h))
    assert health["status"] == "broken" and health["cause"] == "expired_signin"  # a clear status
    assert health["evidence"][0]["run_id"] == failed["id"] and health["evidence"][0]["error"]  # the evidence
    assert [st["id"] for st in health["repair"]["steps"]] == ["sign_in", "recheck"]  # a safe way to recover
    listed = {i["app"]: i["connection"] for i in s.ok(client.get(f"{s.u}/integrations", headers=s.h))}
    assert listed["sim_calendar"]["status"] == "broken"

    # Rechecking while it is still broken changes nothing; signing in never asks for a password here.
    r = s.ok(client.post(f"{s.u}/integrations/sim_calendar/repair", json={"step": "recheck"}, headers=s.h))
    assert not r["ok"] and r["health"]["status"] == "broken"
    r = s.ok(client.post(f"{s.u}/integrations/sim_calendar/repair", json={"step": "sign_in"}, headers=s.h))
    assert r["needs_person"]
    # The sign-in is fixed (in the simulated app); recheck restores it; bookings work again.
    s.ok(
        client.put(
            f"{s.u}/integrations/sim_calendar/settings", json={"settings": {"simulate_failure": ""}}, headers=s.h
        )
    )
    r = s.ok(client.post(f"{s.u}/integrations/sim_calendar/repair", json={"step": "recheck"}, headers=s.h))
    assert r["ok"] and r["health"]["status"] == "live"
    assert book(11)["status"] == "succeeded"


# ==== 7 ==================================================================================================


def test_acceptance_07_one_tenants_api_key_cannot_read_anothers_data_or_notes(client):
    """7. One tenant's API key cannot read another tenant's data or private notes.
    (Every route is swept in test_commai_tenant_sweep.)"""
    a = Shop(client, "Bank A")
    z = Shop(client, "Shop Z", people=("agent", "internal"))
    va = Visitor(client, a.key)
    va.say("A's private enquiry")
    conv = va.conv
    client.post(f"{a.u}/conversations/{conv}/notes", json={"body": "A's private note"}, headers=a.b["internal"]["h"])
    contact = a.conversation(conv)["contact_id"]
    z_key = api_key(client, z.h, None, "Z everything")  # every scope, notes included
    for path in (
        "/conversations",
        f"/conversations/{conv}",
        f"/conversations/{conv}/messages",
        f"/conversations/{conv}/notes",
        f"/conversations/{conv}/export",
        "/contacts",
        f"/contacts/{contact}",
        "/events",
        "/reports/outcomes",
        "/exports/conversations.csv",
        f"/data/contacts/{contact}/export",
    ):
        r = client.get(a.u + path, headers=z_key)
        assert r.status_code == 403 and "private" not in r.text, path
    # A's ids asked for under Z's own business find nothing.
    for path in (f"/conversations/{conv}", f"/conversations/{conv}/notes", f"/contacts/{contact}"):
        assert client.get(z.u + path, headers=z_key).status_code == 404, path
    assert client.get(f"{z.u}/conversations", headers=z_key).json()["items"] == []
    # A's own customer-facing key reads A's conversation but never the note.
    a_key = api_key(client, a.h, ["commai:read"])
    assert client.get(f"{a.u}/conversations/{conv}/notes", headers=a_key).status_code == 403
    r = client.get(f"{a.u}/conversations/{conv}", headers=a_key)
    assert r.status_code == 200 and "A's private enquiry" in r.text and "private note" not in r.text


# ==== 8 ==================================================================================================


@pytest.mark.parametrize("failure", [ModelError("The AI service answered 503."), RuntimeError("boom")])
def test_acceptance_08_when_the_ai_fails_a_person_gets_the_conversation(client, failure):
    """8. When the AI fails, the conversation goes to a person and is not lost."""

    class Failing:
        name = "failing"

        def complete(self, inp):
            raise failure

    s = Shop(client, ai=True)
    runtime.set_model(Failing())
    v = Visitor(client, s.key)
    v.say("When are you open on Saturday?")
    run_jobs()

    seen = v.seen()
    assert seen[0] == ("you", "When are you open on Saturday?")
    assert seen[-1][0] == "system" and "member of our team" in seen[-1][1]  # the customer is told
    mine = s.conversations(view="mine")
    assert [c["id"] for c in mine] == [v.conv]  # a person has it, in their list
    detail = s.conversation(v.conv)
    assert detail["handler"] == "none" and detail["assignee_id"] == s.b["agent"]["id"]
    assert detail["handovers"] and detail["handovers"][0]["packet"]["history"][0]["text"] == seen[0][1]
    assert len(s.events("ai.failed")) == 1
    # The person answers in the same conversation.
    assert s.reply(v.conv, "We open 9 to 1 on Saturday.").status_code == 201
    assert v.seen()[-1] == ("team", "We open 9 to 1 on Saturday.")


# ==== 9 ==================================================================================================


def test_acceptance_09_whatsapp_outside_the_window_and_unapproved_sensitive_actions_are_blocked(client):
    """9. A free-form WhatsApp send outside the 24-hour window, and any
    unapproved sensitive action, is blocked."""
    s = Shop(client)
    s.whatsapp_in("Hi", "wamid.W1")
    conv = next(c for c in s.conversations() if c["channel"] == "whatsapp")["id"]
    with db.tx() as conn:  # the clock moves on 25 hours
        conn.execute("UPDATE conversations SET last_inbound_at = now() - interval '25 hours' WHERE id = %s", (conv,))
    r = s.reply(conv, "Following up")
    assert r.status_code == 422 and "24-hour" in r.json()["detail"]
    key = api_key(client, s.h, ["commai:read", "commai:write"])
    r = client.post(f"{s.u}/conversations/{conv}/messages", json={"body": "From our app"}, headers=key)
    assert r.status_code == 422
    t = s.ok(
        client.post(
            f"{s.u}/whatsapp-templates",
            json={"name": "follow_up", "category": "utility", "body": "Hello {{1}}, following up."},
            headers=s.h,
        )
    )
    send = {"template_id": t["id"], "params": ["Ana"], "take_over": True}
    assert (
        client.post(f"{s.u}/conversations/{conv}/template", json=send, headers=s.h).status_code == 422
    )  # not approved
    s.ok(client.post(f"{s.u}/whatsapp-templates/{t['id']}/review", json={"status": "approved"}, headers=s.h))
    assert client.post(f"{s.u}/conversations/{conv}/template", json=send, headers=s.h).status_code == 201
    run_jobs()
    assert [o["body"] for o in s.outbox()] == ["Hello Ana, following up."]  # only the approved template went out

    # A sensitive action (cancelling a booking) waits for a second person.
    s.connect_calendar()
    run = s.ok(
        client.post(
            f"{s.u}/actions",
            json={"app": "sim_calendar", "action": "cancel", "inputs": {"booking_id": "b-1"}},
            headers=s.h,
        )
    )
    assert run["status"] == "awaiting_approval"
    run_jobs()
    assert s.ok(client.get(f"{s.u}/actions/{run['id']}", headers=s.h))["status"] == "awaiting_approval"
    own = client.post(f"{s.u}/actions/{run['id']}/approve", headers=s.h)
    assert own.status_code == 403, own.text  # the proposer cannot approve their own action
    assert s.ok(client.get(f"{s.u}/actions/{run['id']}", headers=s.h))["status"] == "awaiting_approval"
    r = client.post(f"{s.u}/actions/{run['id']}/approve", headers=s.b["agent2"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "approved"


# ==== 10 =================================================================================================


def test_acceptance_10_usage_and_outcome_reports_match_recorded_events(client):
    """10. Usage and outcome reports match the recorded events exactly."""
    s = Shop(client, ai=True)
    s.connect_calendar()
    s.allow_ai_tools(["sim_calendar.book"])
    visitors = [Visitor(client, s.key) for _ in range(3)]
    visitors[0].say("When are you open on Saturday?")
    visitors[1].say(_booking_text()[0])
    visitors[2].say("I want to speak to a person please")
    for i in range(2):
        s.whatsapp_in(f"WhatsApp question {i}", f"wamid.T{i}", frm=f"+1868555020{i}")
    run_jobs()
    for c in s.conversations():
        if c["channel"] == "whatsapp":
            assert s.reply(c["id"], "Thanks, we're on it.", take_over=True).status_code == 201
    run_jobs()
    resolved = visitors[0].conv
    s.ok(client.post(f"{s.u}/conversations/{resolved}/state", json={"state": "resolved"}, headers=s.h))
    visitors[0].say("One more thing")  # reopened

    rep = s.ok(client.get(f"{s.u}/reports/outcomes", headers=s.h))

    def n(type_, **match):
        return len([e for e in s.events(type_) if all(e["data"].get(k) == v for k, v in match.items())])

    assert rep["conversations"]["value"] == n("conversation.created") == 5
    assert rep["messages_received"]["value"] == n("message.received") == 6
    assert rep["resolved"]["value"] == n("conversation.state_changed", to="resolved") == 1
    assert rep["reopened"]["value"] == n("conversation.state_changed", to="reopened") == 1
    assert rep["confirmed_bookings"]["value"] == n("booking.confirmed") == 1
    assert rep["integration_failures"]["value"] == n("action.failed") == 0

    use = s.ok(client.get(f"{s.u}/reports/usage", headers=s.h))
    meters = {m["meter"]: m["quantity"] for m in use["meters"]}
    # WhatsApp: one unit per message handed to the (simulated) provider: two
    # replies and the AI's two holding replies.
    assert meters["message_out:whatsapp"] == len(s.outbox()) == 4
    # AI: one unit per AI run that called the model ("speak to a person" hands
    # over without calling it).
    runs = [
        r for c in s.conversations() for r in s.ok(client.get(f"{s.u}/ai/conversations/{c['id']}/runs", headers=s.h))
    ]
    assert len(runs) == 5
    assert meters["ai_reply"] == len([r for r in runs if r["model"]]) == 4
    assert set(meters) == {"ai_reply", "message_out:whatsapp"}
