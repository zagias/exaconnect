# ruff: noqa: F401, F811  (pytest fixtures imported from the helpers)
"""Stripe connector (ADR 0035): payment links (sensitive, idempotent) and
payment status; Stripe-Signature webhooks; no card data, ever."""

import json
import time
import urllib.parse

from exaconnect_controller import db
from exaconnect_controller.commai import actions
from exaconnect_controller.commai.connectors import kit, stripe

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

API = r"https://api\.stripe\.com/v1/"
BOSS = "user:manager@example.org"


def _live(b, allowed):
    go_live("stripe", b["id"])
    return connect_real(b["id"], "stripe", allowed, token="sk_acct_FAKE")


def test_stripe_connect_sign_in(client, real_env, fake):
    b = business(client)
    go_live("stripe", b["id"])
    out = client.post(f"{base(b)}/integrations/stripe/connect", headers=b["agent"]["h"]).json()
    assert out["sign_in_url"].startswith("https://connect.stripe.com/oauth/authorize?")
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(out["sign_in_url"]).query)
    assert qs["scope"] == ["read_write"] and qs["client_id"] == ["stripe-client-id"]
    fake.on(
        "POST",
        r"connect\.stripe\.com/oauth/token",
        (
            200,
            {
                "access_token": "sk_acct_FAKE",
                "refresh_token": "rt_FAKE",
                "stripe_user_id": "acct_123",
                "scope": "read_write",
            },
        ),
    )
    cb = client.get(f"/api/v1/commai/oauth/stripe/callback?code=ac_1&state={qs['state'][0]}", follow_redirects=False)
    assert "signin=ok" in cb.headers["location"]
    row = connection(b["id"], "stripe")
    assert row["settings"]["stripe_user_id"] == "acct_123" and row["token_expires_at"] is None
    assert fake.last("POST", "oauth/token")["body"]["client_secret"] == "stripe-client-secret"


def test_stripe_payment_link_needs_approval_and_is_made_once(client, real_env, fake):
    b = business(client)
    _live(b, ["create_payment_link", "payment_status"])
    conv = conversation(b["id"])
    calls = {"price": [], "link": []}

    def price(c):
        calls["price"].append(c)
        return 200, {"id": "price_1", "unit_amount": int(c["body"]["unit_amount"])}

    def link(c):
        calls["link"].append(c)
        return 200, {"id": "plink_ABC123", "url": "https://buy.stripe.com/abc", "active": True}

    fake.on("POST", API + "prices$", price)
    fake.on("POST", API + "payment_links$", link)
    inputs = {"amount": "49.50", "currency": "BBD", "description": "Deposit", "conversation_id": conv}
    run = propose_and_run(b["id"], "stripe", "create_payment_link", inputs, "pl1", approve_as=BOSS)
    assert run["sensitive"] and run["approved_by"] == BOSS
    assert run["status"] == "succeeded", run["error"]
    assert run["result"]["url"] == "https://buy.stripe.com/abc"
    pc = calls["price"][0]
    assert pc["body"] == {"currency": "bbd", "unit_amount": "4950", "product_data[name]": "Deposit"}
    assert pc["headers"]["Idempotency-Key"] == kit.ref("pl1") + "-price"
    assert pc["headers"]["Authorization"] == "Bearer sk_acct_FAKE"
    assert calls["link"][0]["body"]["metadata[conversation_id]"] == conv
    again = execute_direct(b["id"], "stripe", "create_payment_link", inputs, "pl1")
    assert again["replayed"] and again["url"] == "https://buy.stripe.com/abc" and len(calls["link"]) == 1

    # Card numbers are refused before anything is recorded.
    try:
        propose_and_run(
            b["id"],
            "stripe",
            "create_payment_link",
            {**inputs, "description": "card 4242 4242 4242 4242"},
            "pl2",
            approve_as=BOSS,
        )
        raise AssertionError("card data must be refused")
    except actions.ActionRefused as e:
        assert "never takes card numbers" in str(e)

    page = {"has_more": True, "data": [{"id": "cs_1", "payment_status": "unpaid", "status": "open", "created": 1}]}
    fake.on("GET", API + r"checkout/sessions\?payment_link=plink_ABC123&limit=20$", (200, page))
    fake.on(
        "GET",
        API + r"checkout/sessions\?.*starting_after=cs_1",
        (
            200,
            {
                "has_more": False,
                "data": [{"id": "cs_2", "payment_status": "paid", "status": "complete", "created": 1700000000}],
            },
        ),
    )
    run = propose_and_run(b["id"], "stripe", "payment_status", {"payment_link_id": "plink_ABC123"}, "st1")
    assert run["result"]["paid"] and run["result"]["payments"] == 1 and run["result"]["pending"] == 1


def test_stripe_errors_rate_limit_and_expired(client, real_env, fake):
    b = business(client)
    _live(b, ["payment_status"])
    n = []

    def limited(c):
        n.append(1)
        return (
            (429, {"error": {"message": "Rate limit"}}, {"Retry-After": "1"})
            if len(n) == 1
            else (200, {"has_more": False, "data": []})
        )

    fake.on("GET", API + "checkout/sessions", limited)
    assert (
        execute_direct(b["id"], "stripe", "payment_status", {"payment_link_id": "plink_ABC123"}, "k")["paid"] is False
    )
    fake.on(
        "GET",
        API + "checkout/sessions",
        (401, {"error": {"type": "invalid_request_error", "message": "Expired API Key provided"}}),
    )
    run = propose_and_run(b["id"], "stripe", "payment_status", {"payment_link_id": "plink_ABC123"}, "k2")
    assert run["status"] == "failed" and "(expired_signin)" in run["error"]
    h = client.get(f"{base(b)}/integrations/stripe/health", headers=b["agent"]["h"]).json()
    assert h["cause"] == "expired_signin"


def test_stripe_webhook_signature_and_payment_note(client, real_env, fake):
    b = business(client)
    _live(b, ["create_payment_link"])
    conv = conversation(b["id"])
    fake.on("POST", API + "prices$", (200, {"id": "price_1"}))
    fake.on("POST", API + "payment_links$", (200, {"id": "plink_ABC123", "url": "https://buy.stripe.com/abc"}))
    propose_and_run(
        b["id"],
        "stripe",
        "create_payment_link",
        {"amount": "10", "currency": "usd", "description": "Fee", "conversation_id": conv},
        "pl",
        approve_as=BOSS,
    )
    fake.on("POST", API + "webhook_endpoints$", (200, {"id": "we_1", "secret": "whsec_from_stripe"}))
    r = client.post(f"{base(b)}/connectors/stripe/webhooks", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    sent = fake.last("POST", "webhook_endpoints")["body"]
    assert sent["enabled_events[0]"] == "checkout.session.completed" and "/integration-hooks/" in sent["url"]
    assert hook(b["id"], "stripe")["secret"] == "whsec_from_stripe"

    body = json.dumps(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_9",
                    "payment_link": "plink_ABC123",
                    "payment_status": "paid",
                    "amount_total": 1000,
                    "currency": "usd",
                }
            },
        }
    ).encode()
    t = str(int(time.time()))
    sig = kit.hmac_hex("whsec_from_stripe", f"{t}.".encode() + body)
    assert deliver(b["id"], "stripe", {"Stripe-Signature": f"t={t},v1=bad"}, body) is None
    old = str(int(time.time()) - 900)
    stale = kit.hmac_hex("whsec_from_stripe", f"{old}.".encode() + body)
    assert deliver(b["id"], "stripe", {"Stripe-Signature": f"t={old},v1={stale}"}, body) is None
    evs = deliver(b["id"], "stripe", {"Stripe-Signature": f"t={t},v1=other,v1={sig}"}, body)
    assert evs[0]["type"] == "payment.succeeded"
    notes = q("SELECT body FROM commai_notes WHERE conversation_id = %s", conv)
    assert notes[0]["body"] == "Stripe payment received: 10.00 USD."
    assert q("SELECT data FROM commai_events WHERE type = 'payment.succeeded'")[0]["data"]["conversation_id"] == conv


def test_stripe_stand_in_and_isolation(client, fake):
    a, other = business(client), business(client, "Other Bank")
    connect_simulated(a["id"], "stripe", ["create_payment_link", "payment_status", "deactivate_payment_link"])
    connect_simulated(other["id"], "stripe", ["payment_status"])
    run = propose_and_run(
        a["id"],
        "stripe",
        "create_payment_link",
        {"amount": "1000", "currency": "jpy", "description": "Fee"},
        "p1",
        approve_as=BOSS,
    )
    assert run["status"] == "succeeded", run["error"]
    lid = run["result"]["payment_link_id"]
    assert run["result"]["url"].startswith("https://buy.stripe.com/test_")
    run = propose_and_run(a["id"], "stripe", "payment_status", {"payment_link_id": lid}, "s1")
    assert run["result"]["paid"] is False
    run = propose_and_run(a["id"], "stripe", "deactivate_payment_link", {"payment_link_id": lid}, "d1")
    assert run["result"]["active"] is False
    run = propose_and_run(other["id"], "stripe", "payment_status", {"payment_link_id": lid}, "s1")
    assert run["status"] == "failed" and "(input)" in run["error"]  # not their link
    assert not fake.calls
