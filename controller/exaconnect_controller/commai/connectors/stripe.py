"""Stripe connector (ADR 0035): payment links and payment status only.

Written from Stripe's public API (Prices, Payment Links, Checkout Sessions,
Webhook Endpoints) and Stripe Connect OAuth. ExaCarib registers one Connect
platform (EXA_STRIPE_CLIENT_ID = the platform's ca_... client id,
EXA_STRIPE_CLIENT_SECRET = the platform's secret key) with the redirect URI
{EXA_PUBLIC_URL}/api/v1/commai/oauth/stripe/callback. Each business connects
its own Stripe account; money goes straight to it.

- No card data ever touches CommAI: customers pay on Stripe's own page.
  Inputs that look like a card number are refused, and CommAI never asks
  for, stores or shows card details.
- A payment link is sensitive: a person approves each one. Creates send
  Stripe's ``Idempotency-Key`` (derived from the action's key), so a retry
  returns the first link instead of making another.
- Status comes from Checkout Sessions made by the link, and from Stripe's
  webhooks (``Stripe-Signature``: t=..., v1=HMAC-SHA256 of "t.body" under the
  endpoint's secret, five-minute tolerance). A payment for a link made from a
  conversation adds a note to it.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from ..automation import oauth
from . import ActionSpec, ConnectorError, Field, kit
from .more_common import MoreConnector, app_hook, hook_url, luhn_card, set_hook_secret, state_get, state_put

API_VERSION = "2024-06-20"
ZERO_DECIMAL = {
    "bif",
    "clp",
    "djf",
    "gnf",
    "jpy",
    "kmf",
    "krw",
    "mga",
    "pyg",
    "rwf",
    "ugx",
    "vnd",
    "vuv",
    "xaf",
    "xof",
    "xpf",
}
EVENTS = (
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
)

oauth.PROVIDERS["stripe"] = oauth.Provider(
    "stripe",
    "Stripe",
    "https://connect.stripe.com/oauth/authorize",
    "https://connect.stripe.com/oauth/token",
    ("read_write",),
    "EXA_STRIPE",
    expiring=False,
    keep=("stripe_user_id",),
)


def minor_units(amount: str, currency: str) -> int:
    d = Decimal(amount)
    return int(d) if currency.lower() in ZERO_DECIMAL else int((d * 100).quantize(Decimal("1")))


def verify_signature(secret: str, header_value: str, body: bytes, now: float | None = None) -> bool:
    parts = [p.split("=", 1) for p in header_value.split(",") if "=" in p]
    ts = next((v for k, v in parts if k.strip() == "t"), "")
    sigs = [v for k, v in parts if k.strip() == "v1"]
    if not secret or not ts or not sigs or not kit.fresh(ts, 300, now):
        return False
    expected = kit.hmac_hex(secret, f"{ts}.".encode() + body)
    return any(kit.same(s.strip(), expected) for s in sigs)


class StripeSim(kit.Simulator):
    """Answers like Stripe's API, Idempotency-Key replays included."""

    app = "stripe"

    def _replay(self, conn, c, req):
        k = kit.header(req.headers, "Idempotency-Key")
        return (k, self.get(conn, c, "idem", k)) if k else ("", None)

    def _keep(self, conn, c, k, body):
        if k:
            self.put(conn, c, "idem", k, body)
        return body

    @kit.Simulator.route("GET", r"/v1/account$")
    def account(self, conn, c, req, m):
        return 200, {"id": "acct_SIMULATED", "object": "account", "business_profile": {"name": "Example business"}}

    @kit.Simulator.route("POST", r"/v1/prices$")
    def price(self, conn, c, req, m):
        k, seen = self._replay(conn, c, req)
        if seen:
            return 200, seen
        b = req.body or {}
        if int(b.get("unit_amount", "0")) <= 0:
            return 400, {"error": {"type": "invalid_request_error", "message": "Invalid unit_amount"}}
        p = {
            "id": f"price_{self.new_id(conn, c, 'price')}",
            "object": "price",
            "currency": b.get("currency"),
            "unit_amount": int(b["unit_amount"]),
        }
        return 200, self._keep(conn, c, k, p)

    @kit.Simulator.route("POST", r"/v1/payment_links$")
    def link(self, conn, c, req, m):
        k, seen = self._replay(conn, c, req)
        if seen:
            return 200, seen
        lid = f"plink_{self.new_id(conn, c, 'link')}"
        pl = {
            "id": lid,
            "object": "payment_link",
            "active": True,
            "url": f"https://buy.stripe.com/test_{lid[6:]}",
            "metadata": {k2[9:-1]: v for k2, v in (req.body or {}).items() if k2.startswith("metadata[")},
        }
        self.put(conn, c, "link", lid, pl)
        return 200, self._keep(conn, c, k, pl)

    @kit.Simulator.route("POST", r"/v1/payment_links/(plink_\w+)$")
    def update_link(self, conn, c, req, m):
        pl = self.get(conn, c, "link", m.group(1))
        if not pl:
            return 404, {"error": {"type": "invalid_request_error", "message": "No such payment link"}}
        pl["active"] = (req.body or {}).get("active", "true") == "true"
        self.put(conn, c, "link", m.group(1), pl)
        return 200, pl

    @kit.Simulator.route("GET", r"/v1/checkout/sessions$")
    def sessions(self, conn, c, req, m):
        lid = req.query.get("payment_link", "")
        if not self.get(conn, c, "link", lid):
            return 404, {"error": {"type": "invalid_request_error", "message": "No such payment link"}}
        return 200, {
            "object": "list",
            "data": [s for s in self.all(conn, c, "session") if s["payment_link"] == lid],
            "has_more": False,
        }

    @kit.Simulator.route("POST", r"/v1/webhook_endpoints$")
    def endpoint(self, conn, c, req, m):
        return 200, {
            "id": f"we_{self.new_id(conn, c, 'endpoint')}",
            "secret": "whsec_simulated",
            "url": (req.body or {}).get("url"),
        }


class Stripe(MoreConnector):
    app = "stripe"
    label = "Stripe"
    category = "payments"
    description = (
        "Send customers a Stripe payment link and check whether they paid. No card data passes through CommAI."
    )
    auth = "oauth"
    simulator = StripeSim()
    health_path = "/v1/account"
    needs_from_exacarib = (
        "A Stripe Connect platform (Standard accounts, OAuth): EXA_STRIPE_CLIENT_ID (ca_...) and "
        "EXA_STRIPE_CLIENT_SECRET (the platform's secret key)."
    )
    webhooks = "Checkout payments, signed with Stripe-Signature (endpoint set up from CommAI)."
    docs_url = "https://docs.stripe.com/api"
    actions = {
        "payment_status": ActionSpec(
            "payment_status",
            "Check a payment link's payments",
            "read",
            fields=(Field("payment_link_id", "Payment link"),),
        ),
        "create_payment_link": ActionSpec(
            "create_payment_link",
            "Create a payment link",
            "create",
            sensitive=True,
            fields=(
                Field("amount", "Amount", "number"),
                Field("currency", "Currency (like USD or BBD)"),
                Field("description", "What it is for"),
                Field("conversation_id", "Conversation", required=False),
            ),
        ),
        "deactivate_payment_link": ActionSpec(
            "deactivate_payment_link",
            "Switch a payment link off",
            "cancel",
            fields=(Field("payment_link_id", "Payment link"),),
        ),
    }

    def base_url(self, conn, connection: dict) -> str:
        return "https://api.stripe.com"

    def message(self, status: int, body: Any) -> str:
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return f"Stripe answered {status}: {str(err.get('message', ''))[:200]}"
        return super().message(status, body)

    def validate(self, action: str, inputs: dict) -> dict:
        if any(luhn_card(str(v)) for v in inputs.values()):
            raise ValueError("CommAI never takes card numbers. Send the customer a payment link instead.")
        out = super().validate(action, inputs)
        if action == "create_payment_link":
            cur = str(out["currency"]).strip().lower()
            if not re.fullmatch(r"[a-z]{3}", cur):
                raise ValueError("Currency is a three-letter code, like USD or BBD.")
            try:
                amt = Decimal(str(out["amount"]))
            except InvalidOperation:
                raise ValueError("Amount must be a number.") from None
            places = Decimal("1") if cur in ZERO_DECIMAL else Decimal("0.01")
            if amt <= 0 or amt != amt.quantize(places) or amt > Decimal("999999.99"):
                raise ValueError("Amount must be more than 0 and at most 999,999.99, in the currency's units.")
            out.update(currency=cur, amount=str(amt.quantize(places)), description=str(out["description"])[:250])
        if "payment_link_id" in out and not re.fullmatch(r"plink_[A-Za-z0-9]{6,64}", str(out["payment_link_id"])):
            raise ValueError("That is not a Stripe payment link id (plink_...).")
        return out

    def _h(self, key: str) -> dict:
        return {"Idempotency-Key": key, "Stripe-Version": API_VERSION}

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "create_payment_link":
            known = self.known(conn, connection, key)
            if known:
                return {
                    "payment_link_id": known["object_id"],
                    "replayed": True,
                    "url": state_get(conn, connection["customer_id"], self.app, f"link:{known['object_id']}").get(
                        "url", ""
                    ),
                }
            ref = kit.ref(key)
            price = {
                "currency": inputs["currency"],
                "unit_amount": minor_units(inputs["amount"], inputs["currency"]),
                "product_data[name]": inputs["description"],
            }
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"price": price}}
            p = self.call(conn, connection, "POST", "/v1/prices", form=price, headers=self._h(f"{ref}-price")).body
            link = {"line_items[0][price]": p["id"], "line_items[0][quantity]": 1, "metadata[commai_ref]": ref}
            if inputs.get("conversation_id"):
                link["metadata[conversation_id]"] = str(inputs["conversation_id"])
            pl = self.call(
                conn, connection, "POST", "/v1/payment_links", form=link, headers=self._h(f"{ref}-link")
            ).body
            self.remember(conn, connection, key, "payment_link", pl["id"])
            state_put(
                conn,
                connection["customer_id"],
                self.app,
                f"link:{pl['id']}",
                {"id": pl["id"], "url": pl["url"], "conversation_id": str(inputs.get("conversation_id") or "")},
            )
            return {
                "payment_link_id": pl["id"],
                "url": pl["url"],
                "amount": inputs["amount"],
                "currency": inputs["currency"].upper(),
            }
        if action == "payment_status":
            sessions = list(
                self.paginate(
                    conn,
                    connection,
                    "/v1/checkout/sessions",
                    params={"payment_link": inputs["payment_link_id"], "limit": 20},
                    items=lambda b: (b or {}).get("data") or [],
                    next_page=lambda r: (
                        {"starting_after": r.body["data"][-1]["id"]}
                        if r.body.get("has_more") and r.body.get("data")
                        else None
                    ),
                    max_items=100,
                )
            )
            paid = [s for s in sessions if s.get("payment_status") == "paid"]
            return {
                "payment_link_id": inputs["payment_link_id"],
                "paid": bool(paid),
                "payments": len(paid),
                "pending": sum(1 for s in sessions if s.get("status") == "open"),
                "last_paid_at": max((s.get("created", 0) for s in paid), default=None),
            }
        if action == "deactivate_payment_link":
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"active": False}}
            pl = self.call(
                conn,
                connection,
                "POST",
                f"/v1/payment_links/{inputs['payment_link_id']}",
                form={"active": "false"},
                headers={"Stripe-Version": API_VERSION},
            ).body
            return {"payment_link_id": pl["id"], "active": pl.get("active", False)}
        raise ConnectorError(f"Unknown action {action}.", "input")

    # ---- webhooks ----------------------------------------------------------------------

    def register_webhooks(self, conn, connection: dict, actor: str) -> dict:
        """An endpoint on the business's own Stripe account; its signing secret
        (chosen by Stripe) is kept with the CommAI address."""
        hook = app_hook(conn, connection["customer_id"], self.app, actor)
        form: dict = {"url": hook_url(hook), "description": "ExaCarib CommAI: payment status"}
        for i, e in enumerate(EVENTS):
            form[f"enabled_events[{i}]"] = e
        we = self.call(
            conn, connection, "POST", "/v1/webhook_endpoints", form=form, headers={"Stripe-Version": API_VERSION}
        ).body
        set_hook_secret(conn, hook, we["secret"])
        return {"manual": False, "address": hook_url(hook), "endpoint_id": we["id"]}

    def verify_webhook(self, conn, connection, hook, headers, body, query) -> bool:
        return verify_signature(hook.get("secret", ""), kit.header(headers, "Stripe-Signature"), body)

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        try:
            d = json.loads(body)
        except ValueError:
            return []
        if d.get("type") not in EVENTS:
            return []
        s = (d.get("data") or {}).get("object") or {}
        failed = d["type"].endswith("failed")
        if not failed and s.get("payment_status") != "paid":
            return []
        return [
            {
                "type": "payment.failed" if failed else "payment.succeeded",
                "id": str(d.get("id", "")),
                "data": {
                    "payment_link_id": s.get("payment_link") or "",
                    "amount": s.get("amount_total"),
                    "currency": str(s.get("currency") or "").upper(),
                    "session_id": s.get("id", ""),
                },
            }
        ]

    def on_webhook_event(self, conn, connection: dict, event: dict) -> None:
        from .. import events, inbox

        cid = connection["customer_id"]
        d = event["data"]
        link = state_get(conn, cid, self.app, f"link:{d.get('payment_link_id')}")
        conv = link.get("conversation_id") or ""
        events.emit(conn, cid, event["type"], {**d, "conversation_id": conv}, d.get("payment_link_id", ""))
        if conv:
            amount = d.get("amount")
            if not isinstance(amount, int):
                shown = d["currency"]
            elif d["currency"].lower() in ZERO_DECIMAL:
                shown = f"{amount} {d['currency']}"
            else:
                shown = f"{Decimal(amount) / 100:.2f} {d['currency']}"
            word = "received" if event["type"] == "payment.succeeded" else "failed"
            try:
                inbox.add_note(conn, cid, conv, author="CommAI", body=f"Stripe payment {word}: {shown}.")
            except inbox.InboxError:
                pass


kit.register(Stripe())
