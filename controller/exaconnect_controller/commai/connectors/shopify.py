"""Shopify connector (ADR 0029): order lookup for a verified customer, and
refunds and cancellations as sensitive actions.

Written from Shopify's GraphQL Admin API (version 2025-07: orders,
refundCreate, orderCancel, webhookSubscriptionCreate) and its OAuth for
apps. ExaCarib registers one Shopify app in the Partner Dashboard
(EXA_SHOPIFY_CLIENT_ID, EXA_SHOPIFY_CLIENT_SECRET) with the scopes
read_orders and write_orders, the redirect URI
{EXA_PUBLIC_URL}/api/v1/commai/oauth/shopify/callback, and the mandatory
privacy webhooks pointed at CommAI. Each shop signs in at
https://{shop}.myshopify.com/admin/oauth/authorize; offline tokens don't expire.

- Verified customer: an order is shown only when the order number and the
  email on the order both match what the customer gave, and only its status,
  total, dates and tracking (never addresses or payment details).
- Refunds and cancellations are sensitive (a person approves each one).
  A refund's note carries a reference from the idempotency key, checked
  before refunding, so a retry never refunds twice; it never exceeds what
  is left to refund.
- Rate limits: HTTP 429 with Retry-After, and GraphQL THROTTLED answers
  (cost-based), waited out briefly then retried later.
- Webhooks: X-Shopify-Hmac-Sha256 = base64 HMAC-SHA256 of the raw body under
  the app's client secret.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from ..automation import oauth
from . import ActionSpec, ConnectorError, Field, kit
from .more_common import MoreConnector, app_hook, hook_url

API_VERSION = "2025-07"
SECRET_ENV = "EXA_SHOPIFY_CLIENT_SECRET"

oauth.PROVIDERS["shopify"] = oauth.Provider(
    "shopify",
    "Shopify",
    "https://{shop}/admin/oauth/authorize",
    "https://{shop}/admin/oauth/access_token",
    ("read_orders", "write_orders"),
    "EXA_SHOPIFY",
    scope_sep=",",
    expiring=False,
)

ORDER_FIELDS = """id name email createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
  totalPriceSet { shopMoney { amount currencyCode } }
  totalRefundedSet { shopMoney { amount currencyCode } }
  fulfillments(first: 5) { status trackingInfo(first: 3) { company number url } }"""
FIND_ORDER = "query($q: String!) { orders(first: 1, query: $q) { nodes { %s } } }" % ORDER_FIELDS
REFUND_CONTEXT = (
    """query($q: String!) { orders(first: 1, query: $q) { nodes { %s
  refunds(first: 50) { id note }
  transactions(first: 20) { id kind status gateway amountSet { shopMoney { amount } } } } } }"""
    % ORDER_FIELDS
)
REFUND = """mutation($input: RefundInput!) { refundCreate(input: $input) {
  refund { id note totalRefundedSet { shopMoney { amount currencyCode } } } userErrors { field message } } }"""
CANCEL = """mutation($orderId: ID!) { orderCancel(orderId: $orderId, reason: CUSTOMER, refund: false, restock: true,
  notifyCustomer: true) { job { id } orderCancelUserErrors { field message code } } }"""
SUBSCRIBE = """mutation($topic: WebhookSubscriptionTopic!, $url: URL!) { webhookSubscriptionCreate(topic: $topic,
  webhookSubscription: { callbackUrl: $url, format: JSON }) { webhookSubscription { id } userErrors { message } } }"""
SHOP = "{ shop { name myshopifyDomain } }"


def _money(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _order_name(v: str) -> str:
    v = str(v).strip().lstrip("#")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,30}", v):
        raise ValueError("Give the order number, like 1001.")
    return v


def _public(o: dict) -> dict:
    """What a customer may be told about their order."""
    total = (o.get("totalPriceSet") or {}).get("shopMoney") or {}
    refunded = (o.get("totalRefundedSet") or {}).get("shopMoney") or {}
    tracking = [
        {k: t.get(k) for k in ("company", "number", "url")}
        for f in o.get("fulfillments") or []
        for t in f.get("trackingInfo") or []
    ]
    return {
        "order": o.get("name", ""),
        "placed": o.get("createdAt", ""),
        "payment": (o.get("displayFinancialStatus") or "").lower(),
        "fulfilment": (o.get("displayFulfillmentStatus") or "").lower(),
        "cancelled": bool(o.get("cancelledAt")),
        "total": total.get("amount", ""),
        "refunded": refunded.get("amount", "0.0"),
        "currency": total.get("currencyCode", ""),
        "tracking": tracking,
    }


class ShopifySim(kit.Simulator):
    """Answers like the GraphQL Admin API, with one example order to find."""

    app = "shopify"

    def _orders(self, conn, c) -> list[dict]:
        orders = self.all(conn, c, "order")
        if not orders:
            o = {
                "id": "gid://shopify/Order/5001",
                "name": "#1001",
                "email": "ana@example.com",
                "createdAt": "2026-10-01T12:00:00Z",
                "cancelledAt": None,
                "displayFinancialStatus": "PAID",
                "displayFulfillmentStatus": "FULFILLED",
                "totalPriceSet": {"shopMoney": {"amount": "120.00", "currencyCode": "BBD"}},
                "totalRefundedSet": {"shopMoney": {"amount": "0.0", "currencyCode": "BBD"}},
                "fulfillments": [
                    {
                        "status": "SUCCESS",
                        "trackingInfo": [
                            {"company": "DHL Express", "number": "JD0123", "url": "https://example.org/track/JD0123"}
                        ],
                    }
                ],
                "refunds": [],
                "transactions": [
                    {
                        "id": "gid://shopify/OrderTransaction/9001",
                        "kind": "SALE",
                        "status": "SUCCESS",
                        "gateway": "shopify_payments",
                        "amountSet": {"shopMoney": {"amount": "120.00"}},
                    }
                ],
            }
            self.put(conn, c, "order", "5001", o)
            orders = [o]
        return orders

    @kit.Simulator.route("POST", r"/admin/api/[0-9-]+/graphql\.json$")
    def graphql(self, conn, c, req, m):
        q = (req.body or {}).get("query", "")
        v = (req.body or {}).get("variables") or {}
        if "shop {" in q:
            return 200, {"data": {"shop": {"name": "Example shop", "myshopifyDomain": "example.myshopify.com"}}}
        if "orders(" in q:
            name = re.search(r"name:#?([A-Za-z0-9-]+)", v.get("q", ""))
            hits = [o for o in self._orders(conn, c) if name and o["name"].lstrip("#") == name.group(1)]
            return 200, {"data": {"orders": {"nodes": hits}}}
        if "refundCreate" in q:
            inp = v["input"]
            oid = inp["orderId"].rsplit("/", 1)[1]
            o = self.get(conn, c, "order", oid)
            amount = sum(_money(t["amount"]) for t in inp.get("transactions") or [])
            left = _money(o["totalPriceSet"]["shopMoney"]["amount"]) - _money(
                o["totalRefundedSet"]["shopMoney"]["amount"]
            )
            if amount > left:
                return 200, {
                    "data": {
                        "refundCreate": {
                            "refund": None,
                            "userErrors": [{"field": ["transactions"], "message": "Amount exceeds refundable amount"}],
                        }
                    }
                }
            r = {"id": f"gid://shopify/Refund/{self.new_id(conn, c, 'refund', digits=True)}", "note": inp.get("note")}
            o["refunds"].append(r)
            o["totalRefundedSet"]["shopMoney"]["amount"] = str(
                _money(o["totalRefundedSet"]["shopMoney"]["amount"]) + amount
            )
            o["displayFinancialStatus"] = "REFUNDED" if amount == left else "PARTIALLY_REFUNDED"
            self.put(conn, c, "order", oid, o)
            return 200, {
                "data": {
                    "refundCreate": {
                        "refund": {
                            **r,
                            "totalRefundedSet": {"shopMoney": {"amount": str(amount), "currencyCode": "BBD"}},
                        },
                        "userErrors": [],
                    }
                }
            }
        if "orderCancel" in q:
            oid = v["orderId"].rsplit("/", 1)[1]
            o = self.get(conn, c, "order", oid)
            o["cancelledAt"] = "2026-10-07T12:00:00Z"
            self.put(conn, c, "order", oid, o)
            return 200, {"data": {"orderCancel": {"job": {"id": "gid://shopify/Job/1"}, "orderCancelUserErrors": []}}}
        if "webhookSubscriptionCreate" in q:
            return 200, {
                "data": {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": {
                            "id": f"gid://shopify/WebhookSubscription/{self.new_id(conn, c, 'webhook', digits=True)}"
                        },
                        "userErrors": [],
                    }
                }
            }
        return 200, {"errors": [{"message": "Unknown query"}]}


class Shopify(MoreConnector):
    app = "shopify"
    label = "Shopify"
    category = "commerce"
    description = "Look up a verified customer's order in Shopify; refund or cancel it with a person's approval."
    auth = "oauth"
    settings_fields = (
        kit.Setting(
            "shop", "Shop address (name.myshopify.com)", r"[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com", required=True
        ),
    )
    simulator = ShopifySim()
    needs_from_exacarib = (
        "One Shopify app in the Partner Dashboard: EXA_SHOPIFY_CLIENT_ID and EXA_SHOPIFY_CLIENT_SECRET, scopes "
        "read_orders and write_orders, the privacy webhooks, and Shopify's app review before public listing."
    )
    webhooks = "Order and refund updates, signed with X-Shopify-Hmac-Sha256 (set up from CommAI)."
    docs_url = "https://shopify.dev/docs/api/admin-graphql"
    actions = {
        "find_order": ActionSpec(
            "find_order",
            "Look up an order for a verified customer",
            "read",
            fields=(Field("order_number", "Order number"), Field("email", "Email on the order", "email")),
        ),
        "refund_order": ActionSpec(
            "refund_order",
            "Refund an order",
            "refund",
            sensitive=True,
            fields=(
                Field("order_number", "Order number"),
                Field("email", "Email on the order", "email"),
                Field("amount", "Amount to refund", "number"),
                Field("reason", "Reason", "text", False),
            ),
        ),
        "cancel_order": ActionSpec(
            "cancel_order",
            "Cancel an order",
            "cancel",
            sensitive=True,
            fields=(Field("order_number", "Order number"), Field("email", "Email on the order", "email")),
        ),
    }

    def base_url(self, conn, connection: dict) -> str:
        return f"https://{self.settings(connection).get('shop', 'example.myshopify.com')}/admin/api/{API_VERSION}"

    def live_auth_headers(self, conn, connection: dict) -> dict:
        return {"X-Shopify-Access-Token": oauth.access_token(conn, connection)}

    def validate(self, action: str, inputs: dict) -> dict:
        out = super().validate(action, inputs)
        out["order_number"] = _order_name(out["order_number"])
        out["email"] = str(out["email"]).strip().lower()
        if action == "refund_order":
            amt = _money(out["amount"])
            if amt <= 0 or amt != amt.quantize(Decimal("0.01")):
                raise ValueError("The refund amount must be more than 0, with at most two decimal places.")
            out["amount"] = str(amt.quantize(Decimal("0.01")))
        return out

    # ---- GraphQL -----------------------------------------------------------------------

    def gql(self, conn, connection: dict, query: str, variables: dict | None = None) -> dict:
        for attempt in range(kit.INLINE_RETRIES + 1):
            r = self.call(
                conn, connection, "POST", "/graphql.json", json_body={"query": query, "variables": variables or {}}
            )
            body = r.body if isinstance(r.body, dict) else {}
            errs = body.get("errors") or []
            if not errs:
                return body.get("data") or {}
            codes = {(e.get("extensions") or {}).get("code") for e in errs if isinstance(e, dict)}
            if "THROTTLED" in codes:
                if attempt < kit.INLINE_RETRIES:
                    kit.sleep(1.0)
                    continue
                raise ConnectorError("Shopify is limiting requests (throttled). Trying again later.", "provider")
            if "ACCESS_DENIED" in codes:
                raise ConnectorError("Shopify refused: the app lacks a permission.", "permission")
            raise ConnectorError(f"Shopify refused the request: {str(errs[0].get('message', ''))[:200]}", "input")
        return {}

    def _order(self, conn, connection: dict, inputs: dict, query: str = FIND_ORDER) -> dict | None:
        data = self.gql(conn, connection, query, {"q": f"name:#{inputs['order_number']}"})
        nodes = ((data.get("orders") or {}).get("nodes")) or []
        o = nodes[0] if nodes else None
        # Verified customer: the email on the order must match exactly.
        if o is None or str(o.get("email") or "").lower() != inputs["email"]:
            return None
        return o

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_order":
            o = self._order(conn, connection, inputs)
            if o is None:
                return {"found": False, "message": "No order matches that number and email."}
            return {"found": True, **_public(o)}
        if action == "refund_order":
            known = self.known(conn, connection, key)
            if known:
                return {"refund_id": known["object_id"], "replayed": True}
            o = self._order(conn, connection, inputs, REFUND_CONTEXT)
            if o is None:
                raise ConnectorError("No order matches that number and email.", "input")
            ref = kit.ref(key)
            done = next((r for r in o.get("refunds") or [] if ref in str(r.get("note") or "")), None)
            if done:
                self.remember(conn, connection, key, "refund", done["id"])
                return {"refund_id": done["id"], "existing": True}
            amount = _money(inputs["amount"])
            left = _money(o["totalPriceSet"]["shopMoney"]["amount"]) - _money(
                ((o.get("totalRefundedSet") or {}).get("shopMoney") or {}).get("amount", 0)
            )
            if amount > left:
                raise ConnectorError(
                    f"Only {left} {o['totalPriceSet']['shopMoney']['currencyCode']} is left to refund.", "input"
                )
            parent = next(
                (
                    t
                    for t in o.get("transactions") or []
                    if t.get("kind") in ("SALE", "CAPTURE") and t.get("status") == "SUCCESS"
                ),
                None,
            )
            if parent is None:
                raise ConnectorError("The order has no captured payment to refund.", "input")
            note = f"{inputs.get('reason') or 'Refund'} (CommAI {ref})"
            payload = {
                "orderId": o["id"],
                "note": note,
                "notify": True,
                "transactions": [
                    {
                        "orderId": o["id"],
                        "parentId": parent["id"],
                        "gateway": parent["gateway"],
                        "kind": "REFUND",
                        "amount": inputs["amount"],
                    }
                ],
            }
            if self.dry(connection):
                return {"dry_run": True, "would_send": payload}
            out = (self.gql(conn, connection, REFUND, {"input": payload}).get("refundCreate")) or {}
            if out.get("userErrors"):
                raise ConnectorError(f"Shopify refused the refund: {out['userErrors'][0].get('message', '')}", "input")
            rid = out["refund"]["id"]
            self.remember(conn, connection, key, "refund", rid)
            return {
                "refund_id": rid,
                "order": o["name"],
                "amount": inputs["amount"],
                "currency": o["totalPriceSet"]["shopMoney"]["currencyCode"],
            }
        if action == "cancel_order":
            o = self._order(conn, connection, inputs)
            if o is None:
                raise ConnectorError("No order matches that number and email.", "input")
            if o.get("cancelledAt"):
                return {"order": o["name"], "cancelled": True, "existing": True}
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"orderCancel": o["id"]}}
            out = (self.gql(conn, connection, CANCEL, {"orderId": o["id"]}).get("orderCancel")) or {}
            if out.get("orderCancelUserErrors"):
                raise ConnectorError(f"Shopify refused: {out['orderCancelUserErrors'][0].get('message', '')}", "input")
            self.remember(conn, connection, key, "cancel", o["id"])
            return {"order": o["name"], "cancelled": True}
        raise ConnectorError(f"Unknown action {action}.", "input")

    def health(self, conn, connection: dict) -> dict:
        try:
            shop = self.gql(conn, connection, SHOP).get("shop") or {}
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        return {"ok": True, "cause": "", "detail": f"Shop {shop.get('name', '')!r} answering."}

    def sample_inputs(self, action: str) -> dict:
        return {"find_order": {"order_number": "1001", "email": "test@example.com"}}.get(action, {})

    # ---- webhooks ----------------------------------------------------------------------

    def register_webhooks(self, conn, connection: dict, actor: str) -> dict:
        hook = app_hook(conn, connection["customer_id"], self.app, actor)
        ids = []
        for topic in ("ORDERS_UPDATED", "REFUNDS_CREATE"):
            out = self.gql(conn, connection, SUBSCRIBE, {"topic": topic, "url": hook_url(hook)})
            sub = out.get("webhookSubscriptionCreate") or {}
            if sub.get("userErrors"):
                raise ConnectorError(f"Shopify refused the webhook: {sub['userErrors'][0].get('message', '')}", "input")
            ids.append(sub["webhookSubscription"]["id"])
        return {"manual": False, "address": hook_url(hook), "subscriptions": ids}

    def verify_webhook(self, conn, connection, hook, headers, body, query) -> bool:
        secret = os.environ.get(SECRET_ENV, "")
        if not secret:
            return False
        shop = kit.header(headers, "X-Shopify-Shop-Domain")
        if connection and shop and shop != (connection.get("settings") or {}).get("shop"):
            return False
        return kit.same(kit.header(headers, "X-Shopify-Hmac-Sha256"), kit.hmac_b64(secret, body))

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        topic = kit.header(headers, "X-Shopify-Topic")
        try:
            d = json.loads(body)
        except ValueError:
            return []
        wid = kit.header(headers, "X-Shopify-Webhook-Id")
        if topic in ("orders/updated", "refunds/create"):
            order = d.get("name") or str(d.get("order_id", ""))
            return [
                {
                    "type": "commerce.order_updated",
                    "id": wid,
                    "data": {
                        "topic": topic,
                        "order": order,
                        "payment": d.get("financial_status", ""),
                        "fulfilment": d.get("fulfillment_status") or "",
                        "cancelled": bool(d.get("cancelled_at")),
                    },
                }
            ]
        if topic in ("customers/data_request", "customers/redact", "shop/redact"):
            return [{"type": "commerce.privacy_request", "id": wid, "data": {"topic": topic}}]
        return []

    def on_webhook_event(self, conn, connection: dict, event: dict) -> None:
        from .. import events

        if event["type"] == "commerce.order_updated":
            events.emit(
                conn,
                connection["customer_id"],
                "commerce.order_updated",
                event["data"],
                f"shopify:{event['data'].get('order', '')}",
            )
        elif event["type"] == "commerce.privacy_request" and event["data"]["topic"] == "shop/redact":
            # The shop removed the app: forget its sign-in. CommAI keeps no shop customer records.
            oauth.sign_out(conn, connection)


kit.register(Shopify())
