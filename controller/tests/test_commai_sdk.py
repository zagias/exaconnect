"""The Python SDK's CommAI part against the real API (ADR 0016)."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))

from exaconnect import ExaConnect, ExaConnectError, verify_webhook  # noqa: E402

from exaconnect_controller.commai import webhooks  # noqa: E402

from .commai_helpers import PASSWORD, business  # noqa: E402


def test_sdk_commai(client, monkeypatch):
    b = business(client, people=("agent",))
    monkeypatch.delenv("EXACONNECT_API_KEY", raising=False)
    me = ExaConnect(client=client, email=b["agent"]["email"], password=PASSWORD)
    key = me.api_keys.create("app", scopes=["commai:read", "commai:write"])
    exa = ExaConnect(client=client, api_key=key["token"])
    inbox = exa.commai(b["id"])
    conv = inbox.conversations.start("+18685550199", "Do you open on Saturday?", name="Ravi", idempotency_key="s1")
    again = inbox.conversations.start("+18685550199", "Do you open on Saturday?", name="Ravi", idempotency_key="s1")
    assert again["id"] == conv["id"]
    inbox.conversations.reply(conv["id"], "Yes, 9 to 1.", idempotency_key="r1")
    inbox.conversations.reply(conv["id"], "Yes, 9 to 1.", idempotency_key="r1")
    msgs = inbox.conversations.get(conv["id"])["messages"]
    assert [m["body"] for m in msgs] == ["Do you open on Saturday?", "Yes, 9 to 1."]
    with pytest.raises(ExaConnectError) as e:
        inbox.conversations.notes(conv["id"])
    assert e.value.status == 403
    assert inbox.conversations.list(view="open")["items"][0]["contact_name"] == "Ravi"

    ts = str(int(time.time()))
    sig = webhooks.sign("whsec_x", int(ts), b'{"a":1}')
    assert verify_webhook("whsec_x", ts, b'{"a":1}', sig)
    assert not verify_webhook("whsec_x", ts, b'{"a":2}', sig)


def test_sdk_commai_coverage(client, monkeypatch):
    """The SDK reaches the rest of the CommAI API (ADR 0032)."""
    b = business(client, people=("agent",))
    monkeypatch.delenv("EXACONNECT_API_KEY", raising=False)
    me = ExaConnect(client=client, email=b["agent"]["email"], password=PASSWORD)
    c = me.commai(b["id"])

    team = c.routing.create_team("Loans", members=[b["agent"]["id"]], skills=["mortgages"])
    c.routing.set_member(b["agent"]["id"], skills=["mortgages"], languages=["en", "es"])
    rule = c.routing.create_rule("Mortgages", match={"keywords": ["mortgage"]}, skills=["mortgages"])
    assert rule["skills"] == ["mortgages"] and len(c.routing.rules()) == 1
    assert c.routing.members()[0]["skills"] == ["mortgages"]

    conv = c.conversations.start("+18685550199", "A question about my mortgage", name="Ravi")
    assert conv["team_id"] == team["id"] and conv["assignee_id"] == b["agent"]["id"]
    upd = c.conversations.update(conv["id"], priority="high", tags=["vip"], intent="mortgage")
    assert upd["priority"] == "high" and upd["tags"] == ["vip"] and upd["intent"] == "mortgage"
    f = c.conversations.upload(conv["id"], "terms.pdf", "application/pdf", b"%PDF-1.4\n" + b"0" * 40)
    sent = c.conversations.reply(conv["id"], "Here are the terms.", attachments=[f["id"]])
    assert sent["attachments"][0]["name"] == "terms.pdf"
    page = c.conversations.messages(conv["id"], limit=1)
    assert len(page["items"]) == 1 and page["next"]
    assert c.conversations.typing(conv["id"])["typing"][0]["who_kind"] == "user"
    assert c.conversations.presence(conv["id"])["viewing"]

    contact = conv["contact_id"]
    ident = c.contacts.add_identity(contact, "email", "ravi@example.org")
    assert c.contacts.verify_identity(contact, ident["id"], "Signed in on our app")["verified"] is True
    assert {i["channel"] for i in c.contacts.identities(contact)} == {"api", "email"}
    c.contacts.remove_identity(contact, ident["id"])
    assert [x["id"] for x in c.contacts.history(contact)["conversations"]] == [conv["id"]]
    assert c.contacts.update(contact, language="es")["language"] == "es"

    assert c.set_service_targets(remind_percent=70)["remind_percent"] == 70
    assert c.ai.profile()["name"] and c.ai.knowledge() == []
    assert c.workflows.list() == [] and isinstance(c.integrations.list(), list)
    assert isinstance(c.reports.outcomes(), dict)
    assert float(c.reports.set_limit("message_out:sms", monthly_hard=100)["monthly_hard"]) == 100
    assert any(lim["meter"] == "message_out:sms" for lim in c.reports.limits())

    # Errors carry the code and the request id.
    with pytest.raises(ExaConnectError) as e:
        c.conversations.get("00000000-0000-0000-0000-000000000000")
    assert e.value.code == "not_found" and e.value.request_id
