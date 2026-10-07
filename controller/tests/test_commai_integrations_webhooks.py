"""Webhooks as standards (ADR 0034): CloudEvents 1.0 bodies and Standard
Webhooks headers on outbound deliveries; signed inbound webhooks (Standard
Webhooks or ExaCarib v1, CloudEvents structured or binary, or JSON) that
start workflows, accepted once, refused when unsigned, wrongly signed or stale."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

from exaconnect_controller import db
from exaconnect_controller.commai import webhooks
from exaconnect_controller.commai.automation import workflows
from exaconnect_controller.commai.standards import webhooks_std

from .commai_helpers import base, business, run_jobs


def std_headers(whsec: str, msg_id: str, body: bytes, ts: int | None = None) -> dict:
    """What a Standard Webhooks sender does, written from the spec (not our code)."""
    ts = ts or int(time.time())
    key = base64.b64decode(whsec.removeprefix("whsec_"))
    mac = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": f"v1,{mac}"}


def test_outbound_cloudevents_with_standard_webhooks_headers(client, monkeypatch):
    b = business(client, people=("agent",))
    u, h = base(b), b["agent"]["h"]
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    ep = client.post(f"{u}/webhooks", json={"url": "http://hooks.example/in"}, headers=h).json()
    r = client.put(
        f"{u}/webhooks/{ep['id']}/format", json={"format": "cloudevents", "standard_headers": True}, headers=h
    )
    assert r.status_code == 200, r.text
    whsec = r.json()["standard_secret"]
    assert whsec.startswith("whsec_")
    got = []
    monkeypatch.setattr(webhooks, "sender", lambda url, body, headers, timeout_s=10: got.append((body, headers)) or 200)
    client.post(f"{u}/webhooks/{ep['id']}/test", headers=h)
    run_jobs()
    body, headers = got[-1]
    ce = json.loads(body)
    assert ce["specversion"] == "1.0" and ce["type"] == "com.exacarib.commai.webhook.test"
    assert ce["source"] == f"/commai/customers/{b['id']}" and ce["id"] == headers["X-ExaCarib-Event-Id"]
    assert headers["Content-Type"].startswith("application/cloudevents+json")
    # A Standard Webhooks receiver, using the whsec_ secret, accepts it.
    want = std_headers(whsec, headers["webhook-id"], body, int(headers["webhook-timestamp"]))
    assert headers["webhook-signature"] == want["webhook-signature"]
    assert webhooks_std.verify_standard(ep["secret"], headers, body)
    # The original v1 signature is still there.
    assert webhooks.verify(ep["secret"], headers["X-ExaCarib-Timestamp"], body, headers["X-ExaCarib-Signature"])
    # Back to the original format.
    client.put(f"{u}/webhooks/{ep['id']}/format", json={"format": "exacarib"}, headers=h)
    client.post(f"{u}/webhooks/{ep['id']}/test", headers=h)
    run_jobs()
    body, headers = got[-1]
    assert "specversion" not in json.loads(body) and "webhook-signature" not in headers


def test_inbound_hook_verifies_dedupes_and_starts_workflows(client):
    b = business(client, people=("agent",))
    other = business(client, "Other Ltd", people=("agent",))
    u, h = base(b), b["agent"]["h"]
    r = client.post(f"{u}/inbound-hooks", json={"name": "Zapier orders", "event_type": "order.created"}, headers=h)
    assert r.status_code == 201, r.text
    hook = r.json()
    secret, path = hook["secret"], urllib.parse.urlsplit(hook["url"]).path
    assert "secret" not in client.get(f"{u}/inbound-hooks", headers=h).json()[0]

    body = json.dumps({"order": "A-100", "email": "ann@example.com"}).encode()
    ok = client.post(
        path, content=body, headers={"Content-Type": "application/json", **std_headers(secret, "msg_1", body)}
    )
    assert ok.status_code == 202, ok.text
    again = client.post(
        path, content=body, headers={"Content-Type": "application/json", **std_headers(secret, "msg_1", body)}
    )
    assert again.status_code == 200 and again.json()["duplicate"]
    with db.tx() as conn:
        evs = conn.execute(
            "SELECT customer_id, data FROM commai_events WHERE type = 'inbound_webhook.received'"
        ).fetchall()
    assert len(evs) == 1 and str(evs[0]["customer_id"]) == b["id"]
    assert evs[0]["data"]["type"] == "order.created" and evs[0]["data"]["data"]["order"] == "A-100"

    # Wrong secret, stale timestamp, no signature: refused and counted.
    bad = client.post(
        path, content=body, headers=std_headers("whsec_" + base64.b64encode(b"x" * 32).decode(), "msg_2", body)
    )
    assert bad.status_code == 401
    stale = client.post(path, content=body, headers=std_headers(secret, "msg_3", body, int(time.time()) - 900))
    assert stale.status_code == 401
    assert client.post(path, content=body, headers={"Content-Type": "application/json"}).status_code == 401
    hooks = client.get(f"{u}/inbound-hooks", headers=h).json()
    assert hooks[0]["rejected_count"] == 3 and hooks[0]["received_count"] == 1

    # A CloudEvent in binary mode, signed with the ExaCarib v1 signature.
    ts = int(time.time())
    ce_body = json.dumps({"total": 42}).encode()
    ce_headers = {
        "Content-Type": "application/json",
        "ce-specversion": "1.0",
        "ce-id": "evt-9",
        "ce-type": "com.example.invoice.paid",
        "ce-source": "/billing",
        "X-ExaCarib-Timestamp": str(ts),
        "X-ExaCarib-Signature": webhooks.sign(secret, ts, ce_body),
    }
    r = client.post(path, content=ce_body, headers=ce_headers)
    assert r.status_code == 202, r.text
    with db.tx() as conn:
        ev = conn.execute(
            "SELECT data FROM commai_events WHERE type = 'inbound_webhook.received' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    assert ev["data"]["type"] == "com.example.invoice.paid" and ev["data"]["source"] == "/billing"

    # A workflow can start from it (and only data, never code, comes in).
    with db.tx() as conn:
        v = workflows.validate(
            conn,
            b["id"],
            {
                "name": "New order",
                "trigger": {
                    "event": "inbound_webhook.received",
                    "conditions": [{"field": "event.data.hook", "op": "eq", "value": "Zapier orders"}],
                },
                "steps": [{"type": "wait", "duration_s": 60}],
            },
        )
    assert not v["problems"], v["problems"]

    # Switched off: the address no longer answers. Another business can't manage it.
    assert (
        client.patch(f"{u}/inbound-hooks/{hook['id']}", json={"active": False}, headers=other["agent"]["h"]).status_code
        == 403
    )
    client.patch(f"{u}/inbound-hooks/{hook['id']}", json={"active": False}, headers=h)
    assert client.post(path, content=body, headers=std_headers(secret, "msg_9", body)).status_code == 404
    assert client.delete(f"{base(other)}/inbound-hooks/{hook['id']}", headers=other["agent"]["h"]).status_code == 404
