"""Webhook security, endpoint by endpoint.

Inbound (each provider's own scheme, through the real HTTP route):
  a bad or missing signature is refused and stores nothing; a replay of the
  same delivery (same event id) is processed once; a stale timestamp is
  refused where the scheme carries one. Schemes without a timestamp (Meta's
  X-Hub-Signature-256, Twilio, Telegram's secret header, the simulated
  provider, the email shared secret, Shopify, Pipedrive) rely on the event id
  for replays; that is said per test.

Outbound (commai/webhooks.py, ADR 0016 and 0028): the signature a receiver
computes from the spec matches, in the ExaCarib v1, CloudEvents and Standard
Webhooks forms; a stale delivery fails the receiver's check; failures are
retried with growing delays; after the last attempt the delivery is marked
failed (the dead-letter state shown on the deliveries screen) and nothing
more is sent. The design retries every failure, 4xx included (the module
docstring promises "a delivery that fails is retried"), so that is what is
tested.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import connectors, webhooks
from exaconnect_controller.commai.channels import providers, social, whatsapp_cloud
from exaconnect_controller.commai.connectors import kit
from exaconnect_controller.commai.standards import inbound, webhooks_std

from .commai_connector_kit import connect_real, go_live, real_env  # noqa: F401 - fixture
from .commai_helpers import base, business, run_jobs, switch_on


def _count(sql: str, *args) -> int:
    with db.tx() as conn:
        return conn.execute(sql, args).fetchone()["n"]


def _in_messages(customer_id) -> int:
    return _count("SELECT count(*) AS n FROM messages WHERE customer_id = %s AND direction = 'in'", customer_id)


def _path(url: str) -> str:
    return "/api/v1" + url.split("/api/v1", 1)[1]


def _channel_account(client, b, channel, address, provider="simulated", settings=None) -> dict:
    r = client.post(
        f"{base(b)}/channel-accounts",
        json={
            "channel": channel,
            "provider": provider,
            "address": address,
            "name": channel,
            "settings": settings or {},
        },
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


# ==== inbound: messaging channels ======================================================================


def test_simulated_provider_hook(client):
    """X-Exa-Signature (HMAC of the body, no timestamp): replays are caught by the message id."""
    b = business(client, people=("agent",))
    acct = _channel_account(client, b, "whatsapp", "+18685550100")
    path = _path(acct["webhook_url"])
    raw = json.dumps({"messages": [{"id": "wamid.S1", "from": "+18685550101", "body": "Hi"}]}).encode()
    good = {"Content-Type": "application/json", "X-Exa-Signature": providers.Simulated.sign(acct["secret"], raw)}
    assert client.post(path, content=raw, headers={"Content-Type": "application/json"}).status_code == 403
    bad = {**good, "X-Exa-Signature": providers.Simulated.sign("not-the-secret", raw)}
    assert client.post(path, content=raw, headers=bad).status_code == 403
    assert client.post(path, content=raw + b" ", headers=good).status_code == 403  # body changed after signing
    assert _in_messages(b["id"]) == 0
    for _ in range(3):
        assert client.post(path, content=raw, headers=good).status_code == 200
    assert _in_messages(b["id"]) == 1
    assert client.post(_path(acct["webhook_url"]) + "x", content=raw, headers=good).status_code == 404


def test_twilio_hook(client, monkeypatch):
    """X-Twilio-Signature over URL and form (no timestamp in Twilio's scheme): MessageSid dedupes."""
    monkeypatch.setenv("EXA_PUBLIC_URL", "https://connect.example")
    monkeypatch.setenv("EXA_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("EXA_TWILIO_AUTH_TOKEN", "twilio-" + "token-for-tests")
    b = business(client, people=("agent",))
    acct = _channel_account(client, b, "sms", "+18685550100", provider="twilio")
    path = _path(acct["webhook_url"])
    form = {"MessageSid": "SM1", "From": "+18685550101", "To": "+18685550100", "Body": "Hi", "NumMedia": "0"}
    sig = providers.Twilio.signature("twilio-token-for-tests", f"https://connect.example{path}", form)
    assert client.post(path, data=form).status_code == 403
    assert client.post(path, data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    # Signed for another address (a capture replayed at a different hook) fails too.
    other = providers.Twilio.signature("twilio-token-for-tests", "https://connect.example/elsewhere", form)
    assert client.post(path, data=form, headers={"X-Twilio-Signature": other}).status_code == 403
    assert client.post(path, data={**form, "Body": "changed"}, headers={"X-Twilio-Signature": sig}).status_code == 403
    for _ in range(2):
        assert client.post(path, data=form, headers={"X-Twilio-Signature": sig}).status_code == 200
    assert _in_messages(b["id"]) == 1


def test_360dialog_hook(client, monkeypatch):
    """X-Hub-Signature-256 with the 360dialog webhook secret: the wamid dedupes."""
    secret = "dialog-" + "secret-for-tests"
    monkeypatch.setenv("EXA_360DIALOG_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("EXA_360DIALOG_API_KEY", "dialog-key-for-tests")
    b = business(client, people=("agent",))
    acct = _channel_account(client, b, "whatsapp", "+18685550100", provider="360dialog")
    path = _path(acct["webhook_url"])
    raw = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"wa_id": "18685550101", "profile": {"name": "Ana"}}],
                                "messages": [
                                    {"from": "18685550101", "id": "wamid.D1", "type": "text", "text": {"body": "Hi"}}
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    good = {"Content-Type": "application/json", "X-Hub-Signature-256": social.meta_sign(secret, raw)}
    assert client.post(path, content=raw, headers={"Content-Type": "application/json"}).status_code == 403
    assert (
        client.post(path, content=raw, headers={**good, "X-Hub-Signature-256": social.meta_sign("x", raw)}).status_code
        == 403
    )
    for _ in range(2):
        assert client.post(path, content=raw, headers=good).status_code == 200
    assert _in_messages(b["id"]) == 1


def test_email_hook(client):
    """The shared secret (header or ?secret=): Message-Id dedupes a re-delivered email."""
    b = business(client, people=("agent",))
    acct = _channel_account(client, b, "email", "help@examplebank.tt")
    path = _path(acct["webhook_url"])
    mail = {"from": "ana@example.org", "subject": "Card", "text": "Hello", "message_id": "<e1@example.org>"}
    assert client.post(path, json=mail).status_code == 403
    assert client.post(path, json=mail, headers={"X-Exa-Email-Secret": "wrong"}).status_code == 403
    assert client.post(f"{path}?secret=wrong", json=mail).status_code == 403
    for _ in range(2):
        assert client.post(path, json=mail, headers={"X-Exa-Email-Secret": acct["secret"]}).status_code == 200
    assert client.post(f"{path}?secret={acct['secret']}", json=mail).status_code == 200
    assert _in_messages(b["id"]) == 1


def test_meta_account_hooks_messenger_and_instagram(client):
    """X-Hub-Signature-256 per account (no timestamp in Meta's scheme): the mid dedupes."""
    b = business(client, people=("agent",))
    for ch, addr in (("messenger", "1029384756"), ("instagram", "17841400000000")):
        switch_on("channel", ch)
        r = client.post(
            f"{base(b)}/social-accounts",
            json={"channel": ch, "address": addr, "simulated": True, "name": ch},
            headers=b["agent"]["h"],
        )
        assert r.status_code == 201, r.text
        acct = r.json()
        with db.tx() as conn:
            row = conn.execute("SELECT * FROM channel_accounts WHERE id = %s", (acct["id"],)).fetchone()
        raw = json.dumps(social.sim_payload(row, "PSID-1", "Hello", f"m_{ch}_1")).encode()
        path = _path(acct["webhook_url"])
        assert client.post(path, content=raw).status_code == 403
        assert (
            client.post(path, content=raw, headers={"X-Hub-Signature-256": social.meta_sign("x", raw)}).status_code
            == 403
        )
        good = {"X-Hub-Signature-256": social.meta_sign(acct["secret"], raw)}
        assert client.post(path, content=raw, headers=good).json()["received"] == 1
        assert client.post(path, content=raw, headers=good).json()["received"] == 0
    assert _in_messages(b["id"]) == 2


def test_meta_app_wide_hook(client, monkeypatch):
    """EXA_META_APP_SECRET signs every Page's traffic: unset fails closed; bad refused; mid dedupes."""
    b = business(client, people=("agent",))
    switch_on("channel", "messenger")
    r = client.post(
        f"{base(b)}/social-accounts",
        json={"channel": "messenger", "address": "1029384756", "simulated": False, "name": "Page"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    raw = json.dumps(
        {
            "object": "page",
            "entry": [
                {
                    "id": "1029384756",
                    "messaging": [{"sender": {"id": "P1"}, "message": {"mid": "m_app_1", "text": "Hi"}}],
                }
            ],
        }
    ).encode()
    url = "/api/v1/commai/channels/meta/webhook"
    monkeypatch.delenv("EXA_META_APP_SECRET", raising=False)
    assert client.post(url, content=raw, headers={"X-Hub-Signature-256": social.meta_sign("", raw)}).status_code == 403
    app_secret = "meta-app-" + "secret-for-tests"
    monkeypatch.setenv("EXA_META_APP_SECRET", app_secret)
    assert client.post(url, content=raw).status_code == 403
    assert client.post(url, content=raw, headers={"X-Hub-Signature-256": social.meta_sign("x", raw)}).status_code == 403
    good = {"X-Hub-Signature-256": social.meta_sign(app_secret, raw)}
    assert client.post(url, content=raw, headers=good).json()["received"] == 1
    assert client.post(url, content=raw, headers=good).json()["received"] == 0
    assert _in_messages(b["id"]) == 1


def test_telegram_hook(client):
    """X-Telegram-Bot-Api-Secret-Token (a shared secret, no timestamp): update and message ids dedupe."""
    b = business(client, people=("agent",))
    switch_on("channel", "telegram")
    r = client.post(
        f"{base(b)}/social-accounts",
        json={"channel": "telegram", "address": "@ExampleBankBot", "simulated": True, "name": "tg"},
        headers=b["agent"]["h"],
    )
    acct = r.json()
    path = _path(acct["webhook_url"])
    update = {
        "update_id": 7,
        "message": {
            "message_id": 7,
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "date": 1,
            "text": "Hi",
        },
    }
    assert client.post(path, json=update).status_code == 403
    assert client.post(path, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    for _ in range(2):
        r = client.post(path, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": acct["secret"]})
        assert r.status_code == 200
    assert _in_messages(b["id"]) == 1


def test_whatsapp_cloud_hook(client):
    """X-Hub-Signature-256 with the app secret: the wamid dedupes; statuses never double count."""
    b = business(client, people=("agent",))
    switch_on("channel", "whatsapp-cloud")
    r = client.post(f"{base(b)}/whatsapp-cloud/signup", json={"number": "+18685550300"}, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    acct = r.json()
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM channel_accounts WHERE id = %s", (acct["id"],)).fetchone()
    raw = json.dumps(whatsapp_cloud.sim_payload(row, "+18685550301", "Hello", "wamid.C1")).encode()
    path = _path(acct["webhook_url"])
    assert client.post(path, content=raw).status_code == 403
    assert (
        client.post(path, content=raw, headers={"X-Hub-Signature-256": social.meta_sign("x", raw)}).status_code == 403
    )
    good = {"X-Hub-Signature-256": social.meta_sign(acct["secret"], raw)}
    for _ in range(2):
        assert client.post(path, content=raw, headers=good).status_code == 200
    assert _in_messages(b["id"]) == 1


# ==== inbound: generic hooks (ExaCarib v1 and Standard Webhooks) ========================================


def _std_headers(whsec: str, msg_id: str, body: bytes, ts: int | None = None) -> dict:
    """A Standard Webhooks sender, written from the spec."""
    ts = ts or int(time.time())
    key = base64.b64decode(whsec.removeprefix("whsec_"))
    mac = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": f"v1,{mac}"}


def _v1_headers(secret: str, event_id: str, body: bytes, ts: int | None = None) -> dict:
    """An ExaCarib v1 sender, written from the documented scheme."""
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-ExaCarib-Event-Id": event_id, "X-ExaCarib-Timestamp": str(ts), "X-ExaCarib-Signature": f"v1={mac}"}


def _received() -> int:
    return _count("SELECT count(*) AS n FROM commai_events WHERE type = 'inbound_webhook.received'")


def test_generic_hook_signatures_staleness_and_replays(client):
    b = business(client, people=("agent",))
    r = client.post(
        f"{base(b)}/inbound-hooks", json={"name": "Orders", "event_type": "order.created"}, headers=b["agent"]["h"]
    )
    hook = r.json()
    path, secret = urllib.parse.urlsplit(hook["url"]).path, hook["secret"]
    body = json.dumps({"order": "A-1"}).encode()
    ct = {"Content-Type": "application/json"}
    now = int(time.time())
    for headers in (
        {},  # unsigned
        _std_headers("whsec_" + base64.b64encode(b"k" * 32).decode(), "m1", body),  # another key
        _std_headers(secret, "m1", body, now - 600),  # stale
        _std_headers(secret, "m1", body, now + 600),  # from the future
        _v1_headers("wrong", "e1", body),
        _v1_headers(secret, "e1", body, now - 600),
        {**_std_headers(secret, "m1", body), "webhook-id": "m-other"},  # the id is signed
    ):
        assert client.post(path, content=body, headers={**ct, **headers}).status_code == 401, headers
    assert _received() == 0

    std = _std_headers(secret, "m1", body)
    assert client.post(path, content=body, headers={**ct, **std}).status_code == 202
    assert client.post(path, content=body, headers={**ct, **std}).json()["duplicate"] is True
    v1 = _v1_headers(secret, "e1", body)
    assert client.post(path, content=body, headers={**ct, **v1}).status_code == 202
    assert client.post(path, content=body, headers={**ct, **v1}).json()["duplicate"] is True
    # A sender's own retry (same event id, signed again later) is still one delivery.
    retry = _v1_headers(secret, "e1", body, now - 30)
    assert client.post(path, content=body, headers={**ct, **retry}).json()["duplicate"] is True
    # A captured v1 delivery replayed with a different, unsigned event id header is a replay.
    forged = {**v1, "X-ExaCarib-Event-Id": "e1-again"}
    r = client.post(path, content=body, headers={**ct, **forged})
    assert r.status_code == 200 and r.json()["duplicate"] is True, r.text
    assert _received() == 2


# ==== inbound: app hooks (/integration-hooks/{token}) ==================================================


def _app_hook(customer_id, app, secret: str | None = None) -> str:
    with db.tx() as conn:
        h = inbound.app_hook(conn, customer_id, app)
        if secret is not None:
            conn.execute("UPDATE commai_inbound_hooks SET secret = %s WHERE id = %s", (secret, h["id"]))
    return f"/api/v1/commai/integration-hooks/{h['token']}"


def _integration_events(customer_id) -> int:
    return _count(
        "SELECT count(*) AS n FROM commai_events WHERE type = 'integration.event' AND customer_id = %s", customer_id
    )


def test_every_app_hook_refuses_an_unsigned_or_wrongly_signed_delivery(client, real_env, monkeypatch):  # noqa: F811
    """Each connector that takes webhooks, through the HTTP route, with no
    signature and with garbage in every signature header we know of."""
    b = business(client, people=("agent",))
    apps = sorted(c.app for c in connectors.all_connectors() if hasattr(c, "verify_webhook"))
    assert {"stripe", "shopify", "zendesk", "hubspot", "calendly", "pipedrive"} <= set(apps)
    junk = {
        "Stripe-Signature": f"t={int(time.time())},v1=00",
        "X-Shopify-Hmac-Sha256": "AAAA",
        "X-Shopify-Webhook-Id": "w1",
        "X-Zendesk-Webhook-Signature": "AAAA",
        "X-Zendesk-Webhook-Signature-Timestamp": "2026-10-07T00:00:00Z",
        "X-HubSpot-Signature-v3": "AAAA",
        "X-HubSpot-Request-Timestamp": str(int(time.time() * 1000)),
        "Calendly-Webhook-Signature": f"t={int(time.time())},v1=00",
        "Authorization": "Basic " + base64.b64encode(b"commai:wrong").decode(),
        "X-Hook-Signature": "00",
        "X-Hub-Signature-256": "sha256=00",
        "X-Goog-Channel-Token": "wrong",
        "ClientState": "wrong",
    }
    body = json.dumps({"id": "evt_1", "type": "x", "value": [{"clientState": "wrong"}]}).encode()
    bad = []
    for app in apps:
        connect_real(b["id"], app, [])
        path = _app_hook(b["id"], app)
        for headers in ({"Content-Type": "application/json"}, {"Content-Type": "application/json", **junk}):
            r = client.post(path, content=body, headers=headers)
            if r.status_code < 400 and "validationToken" not in r.text:
                bad.append(f"{app}: {r.status_code} {r.text[:100]}")
    assert not bad, bad
    assert _integration_events(b["id"]) == 0


def test_stripe_app_hook_stale_and_replay(client, real_env):  # noqa: F811
    b = business(client, people=("agent",))
    go_live("stripe", b["id"])
    connect_real(b["id"], "stripe", [], token="sk_acct_FAKE")
    secret = "whsec_" + "stripe-for-tests"
    path = _app_hook(b["id"], "stripe", secret)
    body = json.dumps(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_1", "payment_status": "paid", "amount_total": 100, "currency": "usd"}},
        }
    ).encode()

    def sig(ts):
        return {"Stripe-Signature": f"t={ts},v1={kit.hmac_hex(secret, f'{ts}.'.encode() + body)}"}

    now = int(time.time())
    assert client.post(path, content=body).status_code == 401
    assert client.post(path, content=body, headers={"Stripe-Signature": f"t={now},v1=bad"}).status_code == 401
    assert client.post(path, content=body, headers=sig(now - 900)).status_code == 401
    assert _integration_events(b["id"]) == 0
    assert client.post(path, content=body, headers=sig(now)).json()["events"] == 1
    assert client.post(path, content=body, headers=sig(now)).json()["events"] == 0  # replay
    assert client.post(path, content=body, headers=sig(now + 5)).json()["events"] == 0  # Stripe's own retry
    assert _integration_events(b["id"]) == 1


def test_shopify_app_hook_replay(client, real_env, monkeypatch):  # noqa: F811
    """Shopify signs the body only (no timestamp); X-Shopify-Webhook-Id dedupes."""
    secret = "shopify-" + "secret-for-tests"
    monkeypatch.setenv("EXA_SHOPIFY_CLIENT_SECRET", secret)
    b = business(client, people=("agent",))
    go_live("shopify", b["id"])
    connect_real(b["id"], "shopify", [], settings={"shop": "acme.myshopify.com"})
    path = _app_hook(b["id"], "shopify")
    body = json.dumps({"name": "#1001", "financial_status": "paid"}).encode()
    good = {
        "X-Shopify-Topic": "orders/updated",
        "X-Shopify-Shop-Domain": "acme.myshopify.com",
        "X-Shopify-Webhook-Id": "w-1",
        "X-Shopify-Hmac-Sha256": kit.hmac_b64(secret, body),
    }
    assert (
        client.post(path, content=body, headers={**good, "X-Shopify-Hmac-Sha256": kit.hmac_b64("x", body)}).status_code
        == 401
    )
    assert (
        client.post(path, content=body, headers={**good, "X-Shopify-Shop-Domain": "evil.myshopify.com"}).status_code
        == 401
    )
    assert client.post(path, content=body, headers=good).json()["events"] == 1
    assert client.post(path, content=body, headers=good).json()["events"] == 0
    assert _integration_events(b["id"]) == 1


def test_zendesk_app_hook_stale(client, real_env):  # noqa: F811
    b = business(client, people=("agent",))
    connect_real(b["id"], "zendesk", [], settings={"subdomain": "acme"})
    secret = "zendesk-" + "secret-for-tests"
    path = _app_hook(b["id"], "zendesk", secret)
    body = json.dumps({"id": "z1", "type": "ticket.updated", "detail": {"id": "1"}}).encode()

    def headers(ts):
        return {
            "X-Zendesk-Webhook-Signature-Timestamp": ts,
            "X-Zendesk-Webhook-Signature": kit.hmac_b64(secret, ts.encode() + body),
        }

    import datetime as dt

    old = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert client.post(path, content=body, headers=headers(old)).status_code == 401
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    first = client.post(path, content=body, headers=headers(now))
    assert first.status_code == 200, first.text
    again = client.post(path, content=body, headers=headers(now))
    assert again.json()["events"] == 0


# ==== inbound: Slack, Teams and the LiveKit voice worker ================================================


def test_slack_interactions_signature_and_staleness(client, monkeypatch):
    from exaconnect_controller.commai.connectors import slack

    signing = "slack-signing-" + "secret-for-tests"
    url = "/api/v1/commai/slack/interactions"
    body = urllib.parse.urlencode({"payload": json.dumps({"type": "block_actions", "actions": []})}).encode()
    ct = {"Content-Type": "application/x-www-form-urlencoded"}

    def signed(ts, secret=signing):
        return {
            **ct,
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": "v0=" + kit.hmac_hex(secret, f"v0:{ts}:".encode() + body),
        }

    monkeypatch.delenv(slack.SIGNING_ENV, raising=False)
    assert client.post(url, content=body, headers=signed(int(time.time()))).status_code == 401  # not set up
    monkeypatch.setenv(slack.SIGNING_ENV, signing)
    now = int(time.time())
    assert client.post(url, content=body, headers=ct).status_code == 401
    assert client.post(url, content=body, headers=signed(now, "wrong")).status_code == 401
    assert client.post(url, content=body, headers=signed(now - 600)).status_code == 401
    assert client.post(url, content=body + b"&x=1", headers=signed(now)).status_code == 401
    assert client.post(url, content=body, headers=signed(now)).status_code == 200
    # A replayed approval press is decided by the action service, which approves once
    # (test_commai_connectors_slack covers that a decided run is not decided again).


def test_teams_bot_endpoint_refuses_without_a_valid_token(client, monkeypatch):
    from exaconnect_controller.commai.connectors import teams

    monkeypatch.setenv(teams.BOT_ID_ENV, "bot-app-id-for-tests")
    act = {"type": "message", "serviceUrl": "https://smba.trafficmanager.net/amer/", "text": "link 00000000"}
    url = "/api/v1/commai/teams/messages"
    assert client.post(url, json=act).status_code == 401
    assert client.post(url, json=act, headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    none_alg = ".".join(
        base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=")
        for p in ({"alg": "none"}, {"aud": "bot-app-id-for-tests", "iss": teams.ISSUER, "exp": 2_000_000_000})
    )
    assert client.post(url, json=act, headers={"Authorization": f"Bearer {none_alg}."}).status_code == 401
    assert client.post(url, json={**act, "serviceUrl": "https://evil.example/"}).status_code == 401
    # Expired and wrongly signed tokens: test_commai_connectors_teams.


def test_livekit_worker_endpoints_need_the_agent_secret(client, monkeypatch):
    url = "/api/v1/commai/voice/livekit/calls"
    monkeypatch.delenv("EXA_LIVEKIT_AGENT_SECRET", raising=False)
    assert client.post(url, json={"dialled": "+18685550100", "caller": "+18685550101"}).status_code in (401, 503)
    monkeypatch.setenv("EXA_LIVEKIT_AGENT_SECRET", "livekit-" + "secret-for-tests")
    assert client.post(url, json={"dialled": "+18685550100", "caller": "+18685550101"}).status_code == 401
    r = client.post(
        url, json={"dialled": "+18685550100", "caller": "+18685550101"}, headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 401


# ==== outbound ===================================================================================


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    got: list[tuple[bytes, dict]] = []
    answers: list = []

    def fake(url, body, headers, timeout_s=10):
        got.append((body, headers))
        a = answers.pop(0) if answers else 200
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(webhooks, "sender", fake)
    return got, answers


def _endpoint(client, b, events=("message.*",)) -> dict:
    r = client.post(
        f"{base(b)}/webhooks", json={"url": "http://hooks.example/in", "events": list(events)}, headers=b["agent"]["h"]
    )
    assert r.status_code == 201, r.text
    return r.json()


def _inbound(b, ext="m1"):
    from exaconnect_controller.commai import inbox

    with db.tx() as conn:
        inbox.receive(conn, b["id"], "web", "+18685550101", "Hello", external_id=ext, name="Ana")


def _deliver_jobs():
    with db.tx() as conn:
        return conn.execute("SELECT * FROM jobs WHERE kind = 'webhook.deliver' ORDER BY id").fetchall()


def test_outbound_signatures_verify_as_a_receiver_computes_them(client, capture):
    got, _ = capture
    b = business(client, people=("agent",))
    ep = _endpoint(client, b)
    _inbound(b)
    run_jobs()
    body, headers = next((bd, h) for bd, h in got if json.loads(bd)["type"] == "message.received")
    # ExaCarib v1, recomputed from the documented scheme.
    ts = headers["X-ExaCarib-Timestamp"]
    want = "v1=" + hmac.new(ep["secret"].encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers["X-ExaCarib-Signature"] == want
    assert webhooks.verify(ep["secret"], ts, body, want)
    assert not webhooks.verify(ep["secret"], ts, body + b" ", want)  # body changed
    assert not webhooks.verify(ep["secret"], ts, body, want, now=int(ts) + 301)  # stale at the receiver
    assert json.loads(body)["id"] == headers["X-ExaCarib-Event-Id"]

    # CloudEvents with Standard Webhooks headers.
    r = client.put(
        f"{base(b)}/webhooks/{ep['id']}/format",
        json={"format": "cloudevents", "standard_headers": True},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text
    whsec = r.json()["standard_secret"]
    got.clear()
    _inbound(b, "m2")
    run_jobs()
    body, headers = next((bd, h) for bd, h in got if json.loads(bd)["type"].endswith("message.received"))
    ce = json.loads(body)
    assert ce["specversion"] == "1.0" and ce["type"] == "com.exacarib.commai.message.received"
    assert headers["Content-Type"].startswith("application/cloudevents+json")
    key = base64.b64decode(whsec.removeprefix("whsec_"))
    msg, t = headers["webhook-id"], headers["webhook-timestamp"]
    mac = base64.b64encode(hmac.new(key, f"{msg}.{t}.".encode() + body, hashlib.sha256).digest()).decode()
    assert f"v1,{mac}" in headers["webhook-signature"].split()
    assert webhooks_std.verify_standard(whsec, headers, body)
    assert not webhooks_std.verify_standard(whsec, headers, body, now=int(t) + 301)
    assert webhooks_std.verify_v1(ep["secret"], headers, body)  # the v1 header is always there too


def test_outbound_retries_with_backoff_then_gives_up(client, capture):
    got, answers = capture
    b = business(client, people=("agent",))
    ep = _endpoint(client, b)
    answers.extend([503, 500, ConnectionError("refused"), 502, 504, 500, 200])
    _inbound(b)
    run_jobs()
    delays = []
    for _ in range(10):
        job = _deliver_jobs()[0]
        if job["status"] != "queued":
            break
        with db.tx() as conn:
            delays.append(
                conn.execute(
                    "SELECT extract(epoch FROM run_after - now()) AS s FROM jobs WHERE id = %s", (job["id"],)
                ).fetchone()["s"]
            )
        run_jobs()
    job = _deliver_jobs()[0]
    # Six attempts (the documented maximum), each later than the one before, then no more.
    assert job["attempts"] == 6 and job["status"] in ("done", "dead")
    assert len(delays) == 5 and all(b2 > a2 for a2, b2 in zip(delays, delays[1:], strict=False)), delays
    assert 25 <= delays[0] <= 31 and delays[-1] > 400
    sent = [h for bd, h in got if json.loads(bd)["type"] == "message.received"]
    assert len(sent) == 6 and len({h["X-ExaCarib-Event-Id"] for h in sent}) == 1
    deliveries = client.get(f"{base(b)}/webhooks/{ep['id']}/deliveries", headers=b["agent"]["h"]).json()
    d = next(x for x in deliveries if x["event_type"] == "message.received")
    assert d["status"] == "failed" and d["attempts"] == 6 and d["last_error"]
    # Given up: nothing more goes out, however often the worker runs.
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET run_after = now() WHERE kind = 'webhook.deliver'")
    run_jobs()
    assert len([1 for bd, _ in got if json.loads(bd)["type"] == "message.received"]) == 6


def test_outbound_success_after_retry_and_4xx_is_retried_by_design(client, capture):
    got, answers = capture
    b = business(client, people=("agent",))
    ep = _endpoint(client, b, events=("message.received",))
    answers.extend([410, 200])
    _inbound(b)
    run_jobs()
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET run_after = now() WHERE kind = 'webhook.deliver'")
    run_jobs()
    run_jobs()
    assert len(got) == 2
    sigs = {h["X-ExaCarib-Signature"] for _, h in got}
    assert len(sigs) in (1, 2)  # each attempt is signed with its own timestamp
    deliveries = client.get(f"{base(b)}/webhooks/{ep['id']}/deliveries", headers=b["agent"]["h"]).json()
    assert [(d["status"], d["attempts"], d["response_code"]) for d in deliveries] == [("delivered", 2, 200)]
    # A paused (deleted) endpoint gets nothing more.
    client.delete(f"{base(b)}/webhooks/{ep['id']}", headers=b["agent"]["h"])
    _inbound(b, "m9")
    run_jobs()
    assert len(got) == 2
