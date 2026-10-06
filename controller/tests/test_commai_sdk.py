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
