# ruff: noqa: F401, F811  (pytest fixtures imported from the helpers)
"""Helpdesk connectors (ADR 0029): Zendesk, Freshdesk and ServiceNow against a
fake HTTP layer (live mode) and their simulated stand-ins."""

import datetime as dt
import json
import urllib.parse

from exaconnect_controller.commai.connectors import kit

from .commai_helpers import base, business
from .connectors_more_helpers import (
    connect_real,
    connect_simulated,
    connection,
    conversation,
    deliver,
    execute_direct,
    fake,
    go_live,
    hook,
    propose_and_run,
    q,
    real_env,
)

ZD = r"https://acme\.zendesk\.com"


def _zd_ticket(tid=101, status="new", ext=None):
    return {
        "id": tid,
        "subject": "Card blocked",
        "status": status,
        "priority": "high",
        "external_id": ext,
        "updated_at": "2026-10-07T10:00:00Z",
    }


# ---- Zendesk ---------------------------------------------------------------------------


def test_zendesk_sign_in_at_the_business_subdomain(client, real_env, fake):
    b = business(client)
    go_live("zendesk", b["id"])
    u, h = base(b), b["agent"]["h"]
    r = client.post(f"{u}/integrations/zendesk/connect", headers=h)
    assert r.status_code == 200, r.text
    r = client.put(f"{u}/integrations/zendesk/settings", json={"settings": {"subdomain": "acme"}}, headers=h)
    assert r.status_code == 200, r.text
    assert (
        client.put(
            f"{u}/integrations/zendesk/settings", json={"settings": {"subdomain": "evil.com/x"}}, headers=h
        ).status_code
        == 422
    )
    r = client.post(f"{u}/integrations/zendesk/sign-in", headers=h)
    assert r.status_code == 200, r.text
    url = urllib.parse.urlsplit(r.json()["url"])
    assert url.netloc == "acme.zendesk.com" and url.path == "/oauth/authorizations/new"
    qs = urllib.parse.parse_qs(url.query)
    assert qs["scope"] == ["read write"]
    fake.on(
        "POST",
        ZD + "/oauth/tokens",
        (200, {"access_token": "zd-FAKE-token", "token_type": "bearer", "scope": "read write"}),
    )
    cb = client.get(f"/api/v1/commai/oauth/zendesk/callback?code=c1&state={qs['state'][0]}", follow_redirects=False)
    assert "signin=ok" in cb.headers["location"], cb.headers["location"]
    row = connection(b["id"], "zendesk")
    assert row["auth_status"] == "signed_in" and row["token_expires_at"] is None  # Zendesk tokens don't expire
    fake.on("GET", ZD + r"/api/v2/users/me\.json", (200, {"user": {"id": 1}}))
    r = client.get(f"{u}/integrations/zendesk/health?check=true", headers=h)
    assert r.status_code == 200 and r.json()["level"] == "healthy", r.text
    assert fake.last("GET", "users/me")["headers"]["Authorization"] == "Bearer zd-FAKE-token"


def test_zendesk_create_ticket_once_and_link_it(client, real_env, fake):
    b = business(client)
    go_live("zendesk", b["id"])
    connect_real(b["id"], "zendesk", ["create_ticket", "find_ticket"], settings={"subdomain": "acme"})
    conv = conversation(b["id"])
    made = []

    def create(call):
        made.append(call)
        return 201, {"ticket": _zd_ticket(ext=call["body"]["ticket"]["external_id"])}

    fake.on("GET", ZD + r"/api/v2/tickets\.json\?external_id=", (200, {"tickets": []}))
    fake.on("POST", ZD + r"/api/v2/tickets\.json$", create)
    inputs = {
        "subject": "Card blocked",
        "description": "Customer's card was blocked abroad.",
        "email": "ana@example.com",
        "priority": "high",
        "conversation_id": conv,
    }
    run = propose_and_run(b["id"], "zendesk", "create_ticket", inputs, "k-zd-1")
    assert run["status"] == "succeeded", run["error"]
    assert run["result"]["ticket_id"] == "101"
    assert run["result"]["url"] == "https://acme.zendesk.com/agent/tickets/101"
    body = made[0]["body"]["ticket"]
    assert body["external_id"] == kit.ref("k-zd-1") and body["comment"]["public"] is False
    assert made[0]["headers"]["Idempotency-Key"] == kit.ref("k-zd-1")
    link = q("SELECT * FROM commai_ticket_links WHERE customer_id = %s", b["id"])
    assert link[0]["conversation_id"] is not None and str(link[0]["conversation_id"]) == conv

    # The same key again: recorded, so nothing is sent.
    out = execute_direct(b["id"], "zendesk", "create_ticket", inputs, "k-zd-1")
    assert out["replayed"] and len(made) == 1
    # An attempt that created the ticket but crashed before recording it: found by external_id.
    q("DELETE FROM commai_connector_objects WHERE customer_id = %s", b["id"])
    fake.on("GET", ZD + r"/api/v2/tickets\.json\?external_id=", (200, {"tickets": [_zd_ticket(ext="x")]}))
    out = execute_direct(b["id"], "zendesk", "create_ticket", inputs, "k-zd-1")
    assert out["existing"] and len(made) == 1

    # A read through the action service.
    fake.on("GET", ZD + r"/api/v2/tickets/101\.json", (200, {"ticket": _zd_ticket(status="open")}))
    run = propose_and_run(b["id"], "zendesk", "find_ticket", {"ticket_id": "#101"}, "k-zd-2")
    assert run["status"] == "succeeded" and run["result"]["ticket"]["status"] == "open"


def test_zendesk_backoff_then_repair_cause_on_expired_sign_in(client, real_env, fake):
    b = business(client)
    go_live("zendesk", b["id"])
    connect_real(b["id"], "zendesk", ["find_ticket"], settings={"subdomain": "acme"})
    tries = []

    def limited(call):
        tries.append(call)
        if len(tries) < 3:
            return 429, {"error": "TooManyRequests"}, {"Retry-After": "2"}
        return 200, {"ticket": _zd_ticket()}

    fake.on("GET", ZD + r"/api/v2/tickets/101\.json", limited)
    out = execute_direct(b["id"], "zendesk", "find_ticket", {"ticket_id": "101"}, "k1")
    assert out["found"] and len(tries) == 3
    # A long Retry-After is not waited out: a provider failure the job retries later.
    fake.on("GET", ZD + r"/api/v2/tickets/101\.json", (429, {}, {"Retry-After": "600"}))
    try:
        execute_direct(b["id"], "zendesk", "find_ticket", {"ticket_id": "101"}, "k2")
        raise AssertionError("expected a provider error")
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "cause", "") == "provider"
    fake.on("GET", ZD + r"/api/v2/tickets/101\.json", (401, {"error": "invalid_token"}))
    run = propose_and_run(b["id"], "zendesk", "find_ticket", {"ticket_id": "101"}, "k3")
    assert run["status"] == "failed" and "expired_signin" in run["error"]
    h = client.get(f"{base(b)}/integrations/zendesk/health", headers=b["agent"]["h"]).json()
    assert h["cause"] == "expired_signin" and h["repair"]["steps"][0]["id"] == "sign_in"


def test_zendesk_webhook_signature_and_status_sync(client, real_env, fake):
    b = business(client)
    go_live("zendesk", b["id"])
    connect_real(b["id"], "zendesk", ["create_ticket"], settings={"subdomain": "acme"})
    conv = conversation(b["id"])
    fake.on("GET", ZD + r"/api/v2/tickets\.json\?external_id=", (200, {"tickets": []}))
    fake.on("POST", ZD + r"/api/v2/tickets\.json$", (201, {"ticket": _zd_ticket()}))
    propose_and_run(
        b["id"],
        "zendesk",
        "create_ticket",
        {"subject": "S", "description": "D", "email": "a@example.com", "conversation_id": conv},
        "k1",
    )
    # Webhook set-up from CommAI: the signing secret is Zendesk's.
    fake.on("POST", ZD + r"/api/v2/webhooks$", (201, {"webhook": {"id": "01HW"}}))
    fake.on(
        "GET",
        ZD + r"/api/v2/webhooks/01HW/signing_secret",
        (200, {"signing_secret": {"algorithm": "SHA256", "secret": "zd-signing"}}),
    )
    r = client.post(f"{base(b)}/connectors/zendesk/webhooks", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    assert "/integration-hooks/" in r.json()["address"]
    sent = fake.last("POST", r"/api/v2/webhooks$")["body"]["webhook"]
    assert sent["subscriptions"] == ["zen:event-type:ticket.status_changed"]
    assert hook(b["id"], "zendesk")["secret"] == "zd-signing"

    body = json.dumps(
        {
            "id": "ev1",
            "type": "zen:event-type:ticket.status_changed",
            "detail": {"id": "101"},
            "event": {"current": "SOLVED", "previous": "NEW"},
        }
    ).encode()
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    good = {
        "X-Zendesk-Webhook-Signature-Timestamp": ts,
        "X-Zendesk-Webhook-Signature": kit.hmac_b64("zd-signing", ts.encode() + body),
    }
    assert deliver(b["id"], "zendesk", {**good, "X-Zendesk-Webhook-Signature": "bad"}, body) is None
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = {
        "X-Zendesk-Webhook-Signature-Timestamp": old,
        "X-Zendesk-Webhook-Signature": kit.hmac_b64("zd-signing", old.encode() + body),
    }
    assert deliver(b["id"], "zendesk", stale, body) is None
    evs = deliver(b["id"], "zendesk", good, body)
    assert evs[0]["data"] == {"ticket_id": "101", "status": "solved"}
    assert q("SELECT status FROM commai_ticket_links WHERE customer_id = %s", b["id"])[0]["status"] == "solved"
    notes = q("SELECT body FROM commai_notes WHERE conversation_id = %s", conv)
    assert any("101 is now solved" in n["body"] for n in notes)
    r = client.get(f"{base(b)}/connectors/tickets?conversation_id={conv}", headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()[0]["status"] == "solved"


def test_zendesk_stand_in_and_tenant_isolation(client, fake):
    a, other = business(client), business(client, "Other Bank")
    # Not live (no app credentials): connecting puts it on the stand-in.
    r = client.post(f"{base(a)}/integrations/zendesk/connect", headers=a["agent"]["h"])
    assert r.json()["connection"]["auth_method"] == "simulated"
    assert "EXA_ZENDESK_CLIENT_ID" in r.json()["not_live_reason"]
    connect_simulated(
        a["id"], "zendesk", ["create_ticket", "find_ticket", "list_tickets", "update_ticket", "add_comment"]
    )
    connect_simulated(other["id"], "zendesk", ["find_ticket", "list_tickets"])
    run = propose_and_run(
        a["id"], "zendesk", "create_ticket", {"subject": "S", "description": "D", "email": "ana@example.com"}, "k1"
    )
    assert run["status"] == "succeeded", run["error"]
    tid = run["result"]["ticket_id"]
    run = propose_and_run(a["id"], "zendesk", "update_ticket", {"ticket_id": tid, "status": "pending"}, "k2")
    assert run["result"]["status"] == "pending"
    run = propose_and_run(a["id"], "zendesk", "add_comment", {"ticket_id": tid, "body": "Called back"}, "k3")
    assert run["status"] == "succeeded" and run["result"]["public"] is False
    run = propose_and_run(a["id"], "zendesk", "list_tickets", {"email": "ana@example.com"}, "k4")
    assert run["result"]["count"] == 1
    # Another business sees nothing of it.
    run = propose_and_run(other["id"], "zendesk", "find_ticket", {"ticket_id": tid}, "k5")
    assert run["status"] == "succeeded" and run["result"]["found"] is False
    run = propose_and_run(other["id"], "zendesk", "list_tickets", {"email": "ana@example.com"}, "k6")
    assert run["result"]["count"] == 0
    assert (
        client.get(
            f"{base(other)}/connectors/tickets?conversation_id=00000000-0000-0000-0000-000000000000",
            headers=a["agent"]["h"],
        ).status_code
        == 403
    )
    assert not fake.calls  # the stand-in never touches the network
