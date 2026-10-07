# ruff: noqa: F401, F811  (pytest fixtures imported from the helpers)
"""Slack connector (ADR 0035): sign-in, staff messages that respect the
business's sharing setting, approval buttons checked with Slack request
signing and decided by the action service."""

import json
import time
import urllib.parse

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import actions
from exaconnect_controller.commai.connectors import kit, slack

from .commai_helpers import base, business, connect_app
from .connectors_more_helpers import (
    connect_real,
    connect_simulated,
    connection,
    conversation,
    execute_direct,
    fake,
    go_live,
    propose_and_run,
    q,
    real_env,
)

SIGNING = "slack-signing-secret-for-tests"
API = r"https://slack\.com/api/"


@pytest.fixture
def signing(monkeypatch):
    monkeypatch.setenv(slack.SIGNING_ENV, SIGNING)


def _signed(body: bytes, secret: str = SIGNING, ts: int | None = None) -> dict:
    t = str(ts or int(time.time()))
    return {
        "X-Slack-Request-Timestamp": t,
        "X-Slack-Signature": "v0=" + kit.hmac_hex(secret, f"v0:{t}:".encode() + body),
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _press(client, customer_id, run_id, action="commai_approve", team="T0ACME", user="U0AGENT2", **kw):
    payload = {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "actions": [{"action_id": action, "value": json.dumps({"c": customer_id, "r": str(run_id)})}],
    }
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    return client.post("/api/v1/commai/slack/interactions", content=body, headers=_signed(body, **kw))


def _pending_refund(customer_id) -> str:
    """A sensitive action waiting for approval (an example app's cancel)."""
    connect_app(customer_id, "sim_calendar", ["cancel"])
    with db.tx() as conn:
        run = actions.propose(
            conn,
            customer_id,
            role="person",
            app="sim_calendar",
            action="cancel",
            inputs={"booking_id": "00000000-0000-0000-0000-000000000001"},
            actor="user:agent@examplebank.example",
            idempotency_key="pending-1",
        )
    assert run["status"] == "awaiting_approval"
    return str(run["id"])


def test_slack_sign_in_and_signing_secret_needed(client, real_env, fake, monkeypatch):
    b = business(client)
    go_live("slack", b["id"])
    r = client.post(f"{base(b)}/integrations/slack/connect", headers=b["agent"]["h"])
    assert r.json()["sign_in_ready"] is False and slack.SIGNING_ENV in r.json()["not_live_reason"]
    monkeypatch.setenv(slack.SIGNING_ENV, SIGNING)
    q("DELETE FROM integration_connections WHERE customer_id = %s", b["id"])
    r = client.post(f"{base(b)}/integrations/slack/connect", headers=b["agent"]["h"])
    out = r.json()
    assert out["sign_in_ready"], out
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(out["sign_in_url"]).query)
    assert out["sign_in_url"].startswith("https://slack.com/oauth/v2/authorize?")
    assert qs["scope"] == ["chat:write,channels:read,groups:read,channels:history,groups:history"]
    fake.on(
        "POST",
        API + r"oauth\.v2\.access",
        (200, {"ok": True, "access_token": "xoxb-FAKE", "scope": "chat:write", "team": {"id": "T0ACME"}}),
    )
    cb = client.get(f"/api/v1/commai/oauth/slack/callback?code=c&state={qs['state'][0]}", follow_redirects=False)
    assert "signin=ok" in cb.headers["location"]
    row = connection(b["id"], "slack")
    assert row["auth_status"] == "signed_in" and row["token_expires_at"] is None
    fake.on("POST", API + r"auth\.test", (200, {"ok": True, "team_id": "T0ACME", "team": "Acme"}))
    h = client.get(f"{base(b)}/integrations/slack/health?check=true", headers=b["agent"]["h"]).json()
    assert h["level"] == "healthy", h
    assert fake.last("POST", "auth.test")["headers"]["Authorization"] == "Bearer xoxb-FAKE"
    assert q("SELECT value FROM commai_connector_state WHERE app = 'slack'")[0]["value"]["id"] == "T0ACME"


def test_slack_staff_messages_share_only_what_the_business_allows(client, real_env, fake, signing):
    b = business(client)
    go_live("slack", b["id"])
    connect_real(b["id"], "slack", ["notify_staff", "handover_alert"], settings={"channel": "C0SUPPORT1"})
    conv = conversation(b["id"], name="Ana Lopez", subject="Card blocked")
    posts = []

    def post(call):
        posts.append(call["body"])
        return 200, {"ok": True, "channel": call["body"]["channel"], "ts": f"1700000000.00{len(posts)}"}

    fake.on("GET", API + r"conversations\.history", (200, {"ok": True, "messages": []}))
    fake.on("POST", API + r"chat\.postMessage", post)
    run = propose_and_run(
        b["id"],
        "slack",
        "notify_staff",
        {"text": "Call ana@example.com on +1 246 555 0100 about card 4111 1111 1111 1111"},
        "k1",
    )
    assert run["status"] == "succeeded", run["error"]
    assert posts[0]["text"] == "Call [email] on [phone] about card [card]"
    run = propose_and_run(b["id"], "slack", "handover_alert", {"conversation_id": conv}, "k2")
    assert run["status"] == "succeeded", run["error"]
    assert "Ana" not in posts[1]["text"] and "Card blocked" not in posts[1]["text"]
    assert f"/commai/inbox/{conv}" in posts[1]["text"]
    connect_real(b["id"], "slack", ["handover_alert"], settings={"channel": "C0SUPPORT1", "share": "summary"})
    run = propose_and_run(b["id"], "slack", "handover_alert", {"conversation_id": conv}, "k3")
    assert "Ana Lopez, on chat" in posts[2]["text"] and "Subject: Card blocked" in posts[2]["text"]
    assert "ana@example.com" not in json.dumps(posts)

    # Idempotent: the same key is never posted twice, even if the record was lost.
    assert execute_direct(b["id"], "slack", "notify_staff", {"text": "x"}, "k1")["replayed"]
    q("DELETE FROM commai_connector_objects WHERE customer_id = %s", b["id"])
    fake.on(
        "GET",
        API + r"conversations\.history",
        (
            200,
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "1700000000.001",
                        "metadata": {"event_type": "commai_message", "event_payload": {"ref": kit.ref("k1")}},
                    }
                ],
            },
        ),
    )
    out = execute_direct(b["id"], "slack", "notify_staff", {"text": "x"}, "k1")
    assert out["existing"] and len(posts) == 3


def test_slack_errors_backoff_and_expired_sign_in(client, real_env, fake, signing):
    b = business(client)
    go_live("slack", b["id"])
    connect_real(b["id"], "slack", ["list_channels", "notify_staff"], settings={"channel": "C0SUPPORT1"})
    seen = []

    def limited(call):
        seen.append(call)
        if len(seen) == 1:
            return 429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "1"}
        if "cursor" not in call["url"]:
            return 200, {
                "ok": True,
                "channels": [{"id": "C0A1234567", "name": "support"}],
                "response_metadata": {"next_cursor": "abc"},
            }
        return 200, {
            "ok": True,
            "channels": [{"id": "G0B1234567", "name": "mgmt", "is_private": True}],
            "response_metadata": {"next_cursor": ""},
        }

    fake.on("GET", API + r"conversations\.list", limited)
    run = propose_and_run(b["id"], "slack", "list_channels", {}, "k1")
    assert run["status"] == "succeeded", run["error"]
    assert [c["id"] for c in run["result"]["channels"]] == ["C0A1234567", "G0B1234567"] and len(seen) == 3
    fake.on("GET", API + r"conversations\.history", (200, {"ok": False, "error": "channel_not_found"}))
    run = propose_and_run(b["id"], "slack", "notify_staff", {"text": "hello"}, "k2")
    assert run["status"] == "failed" and "(mapping)" in run["error"]
    fake.on("GET", API + r"conversations\.history", (200, {"ok": False, "error": "token_revoked"}))
    run = propose_and_run(b["id"], "slack", "notify_staff", {"text": "hello"}, "k3")
    assert run["status"] == "failed" and "(expired_signin)" in run["error"]
    h = client.get(f"{base(b)}/integrations/slack/health", headers=b["agent"]["h"]).json()
    assert h["repair"]["cause"] == "expired_signin"


def test_slack_approval_buttons(client, fake, signing):
    b, other = business(client), business(client, "Other Bank")
    connect_simulated(b["id"], "slack", ["request_approval"], settings={"approvals_channel": "C0MANAGERS"})
    run_id = _pending_refund(b["id"])
    out = propose_and_run(b["id"], "slack", "request_approval", {"run_id": run_id}, "ask-1")
    assert out["status"] == "succeeded", out["error"]
    msg = q("SELECT data FROM sim_records WHERE app = 'sim:slack' AND kind = 'message'")[0]["data"]
    buttons = msg["blocks"][1]["elements"]
    assert [x["action_id"] for x in buttons] == ["commai_approve", "commai_reject", "commai_open"]
    assert json.loads(buttons[0]["value"]) == {"c": b["id"], "r": run_id}

    # The stand-in's workspace is T0SIM; a press must come from it.
    assert _press(client, b["id"], run_id, team="T0SIM", secret="wrong").status_code == 401
    old = int(time.time()) - 600
    assert _press(client, b["id"], run_id, team="T0SIM", ts=old).status_code == 401
    r = _press(client, b["id"], run_id, team="T0OTHER")
    assert "not connected" in r.json()["text"]
    r = _press(client, b["id"], run_id, team="T0SIM", user="U0NOBODY")
    assert "not linked" in r.json()["text"]

    h = b["agent"]["h"]
    assert (
        client.put(
            f"{base(b)}/connectors/slack/identities",
            json={"external_user": "U0INT", "user_email": b["internal"]["email"]},
            headers=h,
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"{base(b)}/connectors/slack/identities",
            json={"external_user": "U0X", "user_email": other["agent"]["email"]},
            headers=h,
        ).status_code
        == 422
    )
    r = client.put(
        f"{base(b)}/connectors/slack/identities",
        json={"external_user": "U0AGENT", "user_email": b["agent"]["email"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    client.put(
        f"{base(b)}/connectors/slack/identities",
        json={"external_user": "U0AGENT2", "user_email": b["agent2"]["email"]},
        headers=h,
    )
    # The person who proposed can't approve their own action, even from Slack.
    r = _press(client, b["id"], run_id, team="T0SIM", user="U0AGENT")
    assert "Not done" in r.json()["text"]
    r = _press(client, b["id"], run_id, team="T0SIM", user="U0AGENT2")
    assert r.json()["replace_original"] and r.json()["text"].startswith("Approved")
    assert q("SELECT status, approved_by FROM action_runs WHERE id = %s", run_id)[0]["approved_by"] == (
        f"user:{b['agent2']['email']}"
    )
    assert q("SELECT 1 FROM audit_log WHERE action = 'commai.action.approve' AND target = %s", run_id)

    # Tenant isolation: business A's Slack can't decide business B's actions.
    other_run = _pending_refund(other["id"])
    r = _press(client, other["id"], other_run, team="T0SIM", user="U0AGENT2")
    assert "not connected" in r.json()["text"]
    assert q("SELECT status FROM action_runs WHERE id = %s", other_run)[0]["status"] == "awaiting_approval"
