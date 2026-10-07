# ruff: noqa: F401, F811  (pytest fixtures imported from the helpers)
"""Shopify connector (ADR 0035): verified order lookup, refunds and
cancellations as sensitive actions, GraphQL throttling, webhook HMAC."""

import json
import urllib.parse

import pytest

from exaconnect_controller.commai.connectors import kit, shopify

from .commai_helpers import base, business
from .connectors_more_helpers import (
    connect_real,
    connect_simulated,
    connection,
    deliver,
    execute_direct,
    fake,
    go_live,
    propose_and_run,
    q,
    real_env,
)

GQL = r"https://acme\.myshopify\.com/admin/api/2025-07/graphql\.json"
ORDER = {
    "id": "gid://shopify/Order/77",
    "name": "#1001",
    "email": "ana@example.com",
    "createdAt": "2026-10-01T12:00:00Z",
    "cancelledAt": None,
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "FULFILLED",
    "totalPriceSet": {"shopMoney": {"amount": "120.00", "currencyCode": "BBD"}},
    "totalRefundedSet": {"shopMoney": {"amount": "0.0", "currencyCode": "BBD"}},
    "fulfillments": [{"trackingInfo": [{"company": "DHL", "number": "JD1", "url": "https://example.org/t"}]}],
    "shippingAddress": {"address1": "1 Bay Street"},
    "refunds": [],
    "transactions": [
        {"id": "gid://shopify/OrderTransaction/9", "kind": "SALE", "status": "SUCCESS", "gateway": "shopify_payments"}
    ],
}


def _live(b, actions):
    go_live("shopify", b["id"])
    return connect_real(b["id"], "shopify", actions, settings={"shop": "acme.myshopify.com"}, token="shpat-FAKE")


def test_shopify_sign_in_at_the_shop(client, real_env, fake):
    b = business(client)
    go_live("shopify", b["id"])
    u, h = base(b), b["agent"]["h"]
    client.post(f"{u}/integrations/shopify/connect", headers=h)
    client.put(f"{u}/integrations/shopify/settings", json={"settings": {"shop": "acme.myshopify.com"}}, headers=h)
    assert (
        client.put(
            f"{u}/integrations/shopify/settings", json={"settings": {"shop": "acme.example.com"}}, headers=h
        ).status_code
        == 422
    )
    url = client.post(f"{u}/integrations/shopify/sign-in", headers=h).json()["url"]
    parts = urllib.parse.urlsplit(url)
    assert parts.netloc == "acme.myshopify.com" and parts.path == "/admin/oauth/authorize"
    qs = urllib.parse.parse_qs(parts.query)
    assert qs["scope"] == ["read_orders,write_orders"]
    fake.on(
        "POST",
        r"acme\.myshopify\.com/admin/oauth/access_token",
        (200, {"access_token": "shpat-FAKE", "scope": "read_orders,write_orders"}),
    )
    cb = client.get(f"/api/v1/commai/oauth/shopify/callback?code=c&state={qs['state'][0]}", follow_redirects=False)
    assert "signin=ok" in cb.headers["location"]
    fake.on("POST", GQL, (200, {"data": {"shop": {"name": "Acme"}}}))
    hl = client.get(f"{u}/integrations/shopify/health?check=true", headers=h).json()
    assert hl["level"] == "healthy", hl
    call = fake.last("POST", "graphql")
    assert call["headers"]["X-Shopify-Access-Token"] == "shpat-FAKE" and "Authorization" not in call["headers"]


def test_shopify_order_lookup_needs_the_matching_email(client, real_env, fake):
    b = business(client)
    _live(b, ["find_order"])
    fake.on("POST", GQL, (200, {"data": {"orders": {"nodes": [ORDER]}}}))
    run = propose_and_run(b["id"], "shopify", "find_order", {"order_number": "#1001", "email": "ANA@example.com"}, "k1")
    assert run["status"] == "succeeded", run["error"]
    res = run["result"]
    assert res["found"] and res["payment"] == "paid" and res["tracking"][0]["number"] == "JD1"
    assert "Bay Street" not in json.dumps(res)
    assert json.loads(json.dumps(fake.last("POST", "graphql")["body"]))["variables"] == {"q": "name:#1001"}
    run = propose_and_run(b["id"], "shopify", "find_order", {"order_number": "1001", "email": "eve@example.com"}, "k2")
    assert run["result"] == {"found": False, "message": "No order matches that number and email."}


def test_shopify_refund_is_sensitive_and_never_twice(client, real_env, fake):
    b = business(client)
    _live(b, ["refund_order"])
    state = {"refunds": []}

    def gql(call):
        qy = call["body"]["query"]
        if "refundCreate" in qy:
            r = {"id": "gid://shopify/Refund/1", "note": call["body"]["variables"]["input"]["note"]}
            state["refunds"].append(r)
            return 200, {"data": {"refundCreate": {"refund": r, "userErrors": []}}}
        return 200, {"data": {"orders": {"nodes": [{**ORDER, "refunds": list(state["refunds"])}]}}}

    fake.on("POST", GQL, gql)
    inputs = {"order_number": "1001", "email": "ana@example.com", "amount": "20", "reason": "Damaged"}
    try:
        propose_and_run(b["id"], "shopify", "refund_order", inputs, "r1")
        raise AssertionError("a refund must wait for a person")
    except AssertionError as e:
        assert "needs approval" in str(e)
    run = propose_and_run(b["id"], "shopify", "refund_order", inputs, "r2", approve_as="user:manager@example.org")
    assert run["status"] == "succeeded", run["error"]
    sent = next(c for c in fake.calls if "refundCreate" in c["body"]["query"])["body"]["variables"]["input"]
    assert sent["transactions"][0] == {
        "orderId": ORDER["id"],
        "parentId": "gid://shopify/OrderTransaction/9",
        "gateway": "shopify_payments",
        "kind": "REFUND",
        "amount": "20.00",
    }
    assert kit.ref("r2") in sent["note"]
    # The record was lost after Shopify refunded: the note finds it, no second refund.
    q("DELETE FROM commai_connector_objects WHERE customer_id = %s", b["id"])
    out = execute_direct(b["id"], "shopify", "refund_order", inputs, "r2")
    assert out["existing"] and len(state["refunds"]) == 1
    # More than is left is refused before Shopify is asked.
    big = {**inputs, "amount": "500"}
    run = propose_and_run(b["id"], "shopify", "refund_order", big, "r3", approve_as="user:manager@example.org")
    assert run["status"] == "failed" and "left to refund" in run["error"] and len(state["refunds"]) == 1


def test_shopify_throttled_then_expired(client, real_env, fake):
    b = business(client)
    _live(b, ["find_order"])
    tries = []

    def throttled(call):
        tries.append(1)
        if len(tries) < 3:
            return 200, {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}
        return 200, {"data": {"orders": {"nodes": [ORDER]}}}

    fake.on("POST", GQL, throttled)
    assert execute_direct(b["id"], "shopify", "find_order", {"order_number": "1001", "email": "ana@example.com"}, "k1")[
        "found"
    ]
    fake.on("POST", GQL, (401, {"errors": "[API] Invalid API key or access token"}))
    run = propose_and_run(b["id"], "shopify", "find_order", {"order_number": "1001", "email": "ana@example.com"}, "k2")
    assert run["status"] == "failed" and "(expired_signin)" in run["error"]


def test_shopify_webhook_hmac_and_shop(client, real_env, fake):
    b = business(client)
    _live(b, ["find_order"])
    fake.on(
        "POST",
        GQL,
        (
            200,
            {
                "data": {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/1"},
                        "userErrors": [],
                    }
                }
            },
        ),
    )
    r = client.post(f"{base(b)}/connectors/shopify/webhooks", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    topics = [c["body"]["variables"]["topic"] for c in fake.calls]
    assert topics == ["ORDERS_UPDATED", "REFUNDS_CREATE"]
    body = json.dumps({"name": "#1001", "financial_status": "refunded", "cancelled_at": None}).encode()
    secret = "shopify-client-secret"
    good = {
        "X-Shopify-Topic": "orders/updated",
        "X-Shopify-Shop-Domain": "acme.myshopify.com",
        "X-Shopify-Webhook-Id": "w1",
        "X-Shopify-Hmac-Sha256": kit.hmac_b64(secret, body),
    }
    assert deliver(b["id"], "shopify", {**good, "X-Shopify-Hmac-Sha256": kit.hmac_b64("nope", body)}, body) is None
    assert deliver(b["id"], "shopify", {**good, "X-Shopify-Shop-Domain": "other.myshopify.com"}, body) is None
    evs = deliver(b["id"], "shopify", good, body)
    assert evs[0]["data"]["payment"] == "refunded"
    assert q("SELECT 1 FROM commai_events WHERE type = 'commerce.order_updated' AND customer_id = %s", b["id"])
    # The shop removed the app: its sign-in is forgotten.
    gone = json.dumps({"shop_domain": "acme.myshopify.com"}).encode()
    deliver(
        b["id"],
        "shopify",
        {**good, "X-Shopify-Topic": "shop/redact", "X-Shopify-Hmac-Sha256": kit.hmac_b64(secret, gone)},
        gone,
    )
    assert connection(b["id"], "shopify")["auth_status"] == "none"


def test_shopify_stand_in_and_isolation(client, fake):
    a, other = business(client), business(client, "Other Bank")
    for b in (a, other):
        connect_simulated(b["id"], "shopify", ["find_order", "refund_order", "cancel_order"])
    run = propose_and_run(
        a["id"],
        "shopify",
        "refund_order",
        {"order_number": "1001", "email": "ana@example.com", "amount": "120"},
        "r1",
        approve_as="user:manager@example.org",
    )
    assert run["status"] == "succeeded", run["error"]
    run = propose_and_run(a["id"], "shopify", "find_order", {"order_number": "1001", "email": "ana@example.com"}, "k1")
    assert run["result"]["payment"] == "refunded"
    run = propose_and_run(
        other["id"], "shopify", "find_order", {"order_number": "1001", "email": "ana@example.com"}, "k1"
    )
    assert run["result"]["payment"] == "paid"  # each business has its own stand-in shop
    run = propose_and_run(
        a["id"],
        "shopify",
        "cancel_order",
        {"order_number": "1001", "email": "ana@example.com"},
        "c1",
        approve_as="user:manager@example.org",
    )
    assert run["status"] == "succeeded" and run["result"]["cancelled"]
    assert not fake.calls
