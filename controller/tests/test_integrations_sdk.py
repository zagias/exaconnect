"""The Python SDK's integration calls against the real API (ADR 0026)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))

from exaconnect import ExaConnect, ExaConnectError, verify_event  # noqa: E402

from exaconnect_controller.integrations import cloudevents  # noqa: E402

from .integrations_helpers import lab, link  # noqa: E402


def test_sdk_integrations_hooks_notices_and_onramps(client, vault, monkeypatch):
    monkeypatch.delenv("EXACONNECT_API_KEY", raising=False)
    L = lab(client)
    token = L["customer"]["Authorization"].split()[1]
    exa = ExaConnect(client=client, api_key=token)
    cid = L["customer_id"]

    assert any(p["key"] == "pagerduty" for p in exa.integrations.catalogue()["providers"])
    w = exa.integrations.create(
        "webhook", "ops", secrets={"url": "https://hooks.example.org/x"}, event_types=["path.*"]
    )
    assert w["signing_secret"].startswith("whsec_")
    assert exa.integrations.get(w["id"])["name"] == "ops"
    assert exa.integrations.update(w["id"], name="ops 2")["name"] == "ops 2"
    d = exa.integrations.test(w["id"])
    assert d["status"] == "simulated"
    assert [x["id"] for x in exa.integrations.deliveries(w["id"])] == [d["id"]]
    assert "exacarib_link_commit_mbps" in exa.integrations.metrics()
    assert exa.integrations.metrics(openmetrics=True).endswith("# EOF\n")
    exa.integrations.delete(w["id"])
    try:
        exa.integrations.get(w["id"])
        raise AssertionError("deleted")
    except ExaConnectError as e:
        assert e.status == 404

    hook = exa.hooks.subscribe("https://hook.eu1.make.com/abc", ["storm.on"], platform="make")
    assert hook["events"] == ["storm.on"]
    assert exa.hooks.sample("storm.on")[0]["type"].endswith("storm.on")
    exa.hooks.unsubscribe(hook["id"])

    a = link(L, "site-a", "carrier-a")
    assert exa.links.get(a)["path"] == "carrier-a"
    assert {x["path"] for x in exa.links.for_site(L["sites"]["site-a"])} >= {"carrier-a"}
    carrier = ExaConnect(client=client, api_key=L["carrier_a"]["Authorization"].split()[1])
    n = carrier.notices.post("fault", "Fibre cut", [a])
    assert carrier.notices.update(n["id"], status="resolved")["status"] == "resolved"
    assert exa.notices.get(n["id"])["title"] == "Fibre cut" and [x["id"] for x in exa.notices.list()] == [n["id"]]

    o = exa.onramps.create(cid, "aws_dx", 50, site="site-a", aws_account_id="123456789012", region="us-east-1")
    assert exa.onramps.get(cid, o["id"])["simulated"] is True
    assert exa.onramps.refresh(cid, o["id"])["id"] == o["id"]
    assert exa.onramps.delete(cid, o["id"])["status"] == "deleted"
    assert len(exa.onramps.adapters()) == 4


def test_sdk_verifies_connect_webhooks():
    secret = cloudevents.new_secret()
    body = b'{"specversion":"1.0"}'
    headers = cloudevents.signed(secret, "evt-1", {}, body, now=1_700_000_000)
    assert verify_event(secret, headers, body, now=1_700_000_010)
    assert not verify_event(secret, headers, body + b" ", now=1_700_000_010)
    assert not verify_event(secret, headers, body, now=1_700_001_000)
    # The Standard Webhooks reference example.
    ref = {
        "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
        "webhook-timestamp": "1614265330",
        "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
    }
    assert verify_event("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", ref, b'{"test": 2432232314}', now=1614265330)
