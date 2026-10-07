# ruff: noqa: F401, F811  (pytest fixtures imported from the helpers)
"""Microsoft Teams connector (ADR 0035): incoming webhook messages, and
approval buttons through the bot with Bot Framework token checks."""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from exaconnect_controller import db
from exaconnect_controller.commai import actions
from exaconnect_controller.commai.automation import integrations
from exaconnect_controller.commai.connectors import teams

from .commai_helpers import base, business, connect_app
from .connectors_more_helpers import (
    connect_real,
    connect_simulated,
    conversation,
    execute_direct,
    fake,
    go_live,
    propose_and_run,
    q,
    real_env,
)

HOOK = "https://acme.webhook.office.com/webhookb2/abc-123@def/IncomingWebhook/xyz/0123"
SERVICE = "https://smba.trafficmanager.net/amer/"
BOT = "bot-app-id-for-tests"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


@pytest.fixture
def bot(monkeypatch, fake):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    monkeypatch.setenv(teams.BOT_ID_ENV, BOT)
    monkeypatch.setenv(teams.BOT_PASSWORD_ENV, "bot-password-for-tests")
    monkeypatch.setattr(teams, "_keys", {"at": 0.0, "keys": {}})
    fake.on(
        "GET", r"login\.botframework\.com/v1/\.well-known", (200, {"jwks_uri": "https://login.botframework.com/keys"})
    )
    fake.on(
        "GET",
        r"login\.botframework\.com/keys$",
        (
            200,
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": "k1",
                        "n": _b64(pub.n.to_bytes(256, "big")),
                        "e": _b64(pub.e.to_bytes(3, "big")),
                    }
                ]
            },
        ),
    )

    def token(aud=BOT, iss=teams.ISSUER, exp=None, signer=key):
        head = _b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        now = int(time.time())
        body = _b64(
            json.dumps(
                {"iss": iss, "aud": aud, "exp": exp or now + 600, "nbf": now - 10, "serviceurl": SERVICE}
            ).encode()
        )
        sig = signer.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"Bearer {head}.{body}.{_b64(sig)}"

    return token


def _activity(text="", value=None, tenant="tenant-acme", user="aad-agent2"):
    a = {
        "type": "message",
        "serviceUrl": SERVICE,
        "conversation": {"id": "19:chan@thread.tacv2", "tenantId": tenant},
        "from": {"id": "29:x", "aadObjectId": user},
        "text": text,
    }
    if value is not None:
        a["value"] = value
    return a


def _pending(customer_id) -> str:
    connect_app(customer_id, "sim_calendar", ["cancel"])
    with db.tx() as conn:
        run = actions.propose(
            conn,
            customer_id,
            role="person",
            app="sim_calendar",
            action="cancel",
            inputs={"booking_id": "x"},
            actor="user:someone@example.org",
            idempotency_key="p1",
        )
    return str(run["id"])


def test_teams_webhook_entry_and_messages(client, real_env, fake):
    b = business(client)
    go_live("teams", b["id"])
    with db.tx() as conn:
        integrations.connect(conn, b["id"], "teams", "user:admin@example.org")
        try:
            integrations.enter_credentials(
                conn, b["id"], "teams", {"webhook_url": "https://evil.example/hook/123456789"}, "user:admin@example.org"
            )
            raise AssertionError("only Teams webhook addresses are accepted")
        except integrations.SetupError:
            pass
        row = integrations.enter_credentials(conn, b["id"], "teams", {"webhook_url": HOOK}, "user:admin@example.org")
    assert row["signed_in"] and HOOK not in json.dumps(row, default=str)
    q(
        "UPDATE integration_connections SET status = 'live', allowed_actions = %s WHERE app = 'teams'",
        ["notify_staff", "handover_alert"],
    )
    conv = conversation(b["id"])
    sent = []
    tries = []

    def post(call):
        tries.append(1)
        if len(tries) == 1:
            return 429, "", {"Retry-After": "1"}
        sent.append(call)
        return 200, "1"

    fake.on("POST", r"acme\.webhook\.office\.com/webhookb2/", post)
    run = propose_and_run(b["id"], "teams", "notify_staff", {"text": "Ring 246-555-0100 about it"}, "k1")
    assert run["status"] == "succeeded", run["error"]
    card = sent[0]["body"]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and card["body"][1]["text"] == "Ring [phone] about it"
    assert "Authorization" not in sent[0]["headers"] and len(tries) == 2
    run = propose_and_run(b["id"], "teams", "handover_alert", {"conversation_id": conv}, "k2")
    assert run["status"] == "succeeded" and "Ana" not in json.dumps(sent[1]["body"])
    assert execute_direct(b["id"], "teams", "notify_staff", {"text": "x"}, "k1")["replayed"] and len(sent) == 2
    # A deleted workflow (410) means the stored address must be replaced.
    fake.on("POST", r"acme\.webhook\.office\.com/webhookb2/", (410, {"error": {"message": "Gone"}}))
    run = propose_and_run(b["id"], "teams", "notify_staff", {"text": "x"}, "k3")
    assert run["status"] == "failed" and "(expired_signin)" in run["error"]


def test_teams_bot_link_and_approve(client, fake, bot):
    b, other = business(client), business(client, "Other Bank")
    connect_simulated(b["id"], "teams", ["request_approval"])
    r = client.post(f"{base(b)}/connectors/teams/link-code", headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    code = r.json()["code"]

    def post(act, tok=None):
        return client.post("/api/v1/commai/teams/messages", json=act, headers={"Authorization": tok or bot()})

    # Token checks: another audience, another signer, expired, a foreign service URL.
    assert post(_activity(f"link {code}"), bot(aud="someone-else")).status_code == 401
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert post(_activity(f"link {code}"), bot(signer=other_key)).status_code == 401
    assert post(_activity(f"link {code}"), bot(exp=int(time.time()) - 3600)).status_code == 401
    assert post({**_activity(f"link {code}"), "serviceUrl": "https://evil.example/"}).status_code == 401
    assert post(_activity("link 00000000")).json()["text"] == "That code is not valid."
    r = post(_activity(f"<at>Jibsy</at> link {code}"))
    assert r.status_code == 200 and "linked" in r.json()["text"], r.text
    assert post(_activity(f"link {code}")).json()["text"] == "That code is not valid."  # single use

    run_id = _pending(b["id"])
    out = propose_and_run(b["id"], "teams", "request_approval", {"run_id": run_id}, "ask-1")
    assert out["status"] == "succeeded" and out["result"]["via"] == "bot", out
    msg = [r["data"] for r in q("SELECT data FROM sim_records WHERE app = 'sim:teams'") if r["data"].get("attachments")]
    acts = msg[0]["attachments"][0]["content"]["actions"]
    assert [a["type"] for a in acts] == ["Action.Submit", "Action.Submit", "Action.OpenUrl"]
    value = acts[0]["data"]
    assert value == {"commai": "approve", "c": b["id"], "r": run_id}

    assert "not linked to that business" in post(_activity(value=value, tenant="tenant-evil")).json()["text"]
    assert "not linked to a Jibsy person" in post(_activity(value=value)).json()["text"]
    client.put(
        f"{base(b)}/connectors/teams/identities",
        json={"external_user": "aad-agent2", "user_email": b["agent2"]["email"]},
        headers=b["agent"]["h"],
    )
    r = post(_activity(value=value))
    assert r.json()["text"].startswith("Approved"), r.text
    assert q("SELECT status FROM action_runs WHERE id = %s", run_id)[0]["status"] in (
        "approved",
        "executing",
        "succeeded",
        "failed",
    )
    # Tenant isolation: the linked tenant can't decide another business's action.
    other_run = _pending(other["id"])
    r = post(_activity(value={"commai": "approve", "c": other["id"], "r": other_run}))
    assert "not linked to that business" in r.json()["text"]
    assert q("SELECT status FROM action_runs WHERE id = %s", other_run)[0]["status"] == "awaiting_approval"


def test_teams_without_bot_falls_back_to_open_in_commai(client, fake):
    b = business(client)
    connect_simulated(b["id"], "teams", ["request_approval"])
    run_id = _pending(b["id"])
    out = propose_and_run(b["id"], "teams", "request_approval", {"run_id": run_id}, "ask-1")
    assert out["result"]["via"] == "webhook"
    data = [r["data"] for r in q("SELECT data FROM sim_records WHERE app = %s", "sim:teams")][0]
    acts = data["attachments"][0]["content"]["actions"]
    assert [a["type"] for a in acts] == ["Action.OpenUrl"]
