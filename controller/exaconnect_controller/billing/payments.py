"""Online payment of issued invoices through a gateway (ADR 0024). OFF by default.

Two adapters behind one interface:

- **stripe**: a Stripe Checkout Session (``POST /v1/checkout/sessions``,
  form-encoded, ``client_reference_id`` = our payment id) and the
  ``checkout.session.completed`` webhook, checked with the ``Stripe-Signature``
  header (``t=<unix>,v1=<HMAC-SHA256 of "<t>.<body>">``, five minutes' tolerance).
- **hosted**: a generic hosted-payment-page provider shaped like First Atlantic
  Commerce / PowerTranz: ``POST <base>/hosted-page`` with the merchant id and
  password in ``PowerTranz-PowerTranzId`` / ``PowerTranz-PowerTranzPassword``,
  the amount and ISO 4217 numeric currency; the customer pays on the provider's
  page and the provider posts a callback signed with ``X-Signature`` (hex
  HMAC-SHA256 of the raw body with the shared secret).

Modes (``EXA_PAYMENTS``): ``off`` (default: nothing is offered), ``simulated``
(no network call is ever made; the "session" is made up locally and only a
correctly signed webhook marks an invoice paid), or ``live``. Live needs the
provider's credentials as well; a provider without them stays simulated. Real
payment processing is ExaCarib's decision and stays switched off until it is
made: no credentials are in the repository, only placeholder names.

An invoice is never changed by a payment (issued invoices are frozen); it is
paid when a payment recorded against it has status ``paid`` for its total.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from .. import audit
from .core import BillingError, dec

PROVIDERS = {
    "stripe": "Stripe Checkout",
    "hosted": "Hosted payment page (First Atlantic Commerce / PowerTranz style)",
}
# The environment each provider needs before it can go live (names only; values never logged).
REQUIRED_ENV = {
    "stripe": ("EXA_STRIPE_SECRET_KEY", "EXA_STRIPE_WEBHOOK_SECRET"),
    "hosted": ("EXA_HOSTED_PAY_URL", "EXA_HOSTED_PAY_MERCHANT_ID", "EXA_HOSTED_PAY_PASSWORD", "EXA_HOSTED_PAY_SECRET"),
}
STRIPE_TOLERANCE_S = 300
# ISO 4217 numeric codes for the hosted-page shape (PowerTranz takes numbers).
ISO_NUMERIC = {
    "USD": "840",
    "TTD": "780",
    "JMD": "388",
    "BBD": "052",
    "XCD": "951",
    "BSD": "044",
    "KYD": "136",
    "GYD": "328",
    "BZD": "084",
    "DOP": "214",
    "EUR": "978",
    "GBP": "826",
}

# Simulated mode signs with the configured webhook secret, or with a secret made
# up for this process (so a simulated payment can't be completed from outside
# unless someone holds it).
_SIM_SECRETS = {p: secrets.token_hex(32) for p in PROVIDERS}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def payments_mode() -> str:
    m = _env("EXA_PAYMENTS").lower() or "off"
    return m if m in ("off", "simulated", "live") else "off"


def provider_mode(provider: str) -> str:
    """off, simulated or live for one provider."""
    mode = payments_mode()
    if mode == "off" or provider not in PROVIDERS:
        return "off"
    enabled = [p.strip() for p in (_env("EXA_PAYMENT_PROVIDERS") or "stripe,hosted").split(",") if p.strip()]
    if provider not in enabled:
        return "off"
    if mode == "live" and all(_env(n) for n in REQUIRED_ENV[provider]):
        return "live"
    return "simulated"


def webhook_secret(provider: str) -> str:
    name = "EXA_STRIPE_WEBHOOK_SECRET" if provider == "stripe" else "EXA_HOSTED_PAY_SECRET"
    return _env(name) or _SIM_SECRETS[provider]


def status() -> dict:
    """What an admin sees: each adapter's mode and which settings are missing (names only)."""
    return {
        "mode": payments_mode(),
        "providers": [
            {
                "key": p,
                "label": label,
                "mode": provider_mode(p),
                "missing_env": [n for n in REQUIRED_ENV[p] if not _env(n)],
            }
            for p, label in PROVIDERS.items()
        ],
    }


# ---- the adapter interface -------------------------------------------------------------


@dataclass(frozen=True)
class Checkout:
    reference: str  # the provider's id for the session or transaction
    url: str  # where the customer pays


@dataclass(frozen=True)
class Paid:
    event_id: str
    payment_id: str  # our billing_payments.id, as sent when the session was made
    reference: str
    amount: Decimal
    currency: str
    approved: bool


class Gateway(Protocol):
    def create(self, payment: dict, invoice: dict, return_url: str) -> Checkout: ...

    def verify(self, headers: dict[str, str], body: bytes) -> Paid | None:
        """Check the signature and read the event. None: a valid event we don't act on."""
        ...


def _post(url: str, data: bytes, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise BillingError(f"The payment provider refused the request ({e.code}).", 502) from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise BillingError("The payment provider could not be reached.", 502) from e


def _cents(v: Any) -> int:
    return int((dec(v) * 100).to_integral_value())


class StripeGateway:
    def __init__(self, live: bool):
        self.live = live

    def create(self, payment: dict, invoice: dict, return_url: str) -> Checkout:
        if not self.live:
            ref = f"cs_sim_{uuid.uuid4().hex}"
            return Checkout(ref, f"{return_url}&simulated={ref}")
        form = {
            "mode": "payment",
            "client_reference_id": str(payment["id"]),
            "metadata[payment_id]": str(payment["id"]),
            "metadata[invoice_number]": invoice["number"],
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": invoice["currency"].lower(),
            "line_items[0][price_data][unit_amount]": str(_cents(invoice["total"])),
            "line_items[0][price_data][product_data][name]": f"ExaCarib invoice {invoice['number']}",
            "success_url": f"{return_url}&paid=1",
            "cancel_url": return_url,
        }
        base = (_env("EXA_STRIPE_API_BASE") or "https://api.stripe.com").rstrip("/")
        out = _post(
            f"{base}/v1/checkout/sessions",
            urllib.parse.urlencode(form).encode(),
            {
                "Authorization": f"Bearer {_env('EXA_STRIPE_SECRET_KEY')}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Idempotency-Key": f"exa-payment-{payment['id']}",
            },
        )
        if not out.get("id") or not out.get("url"):
            raise BillingError("The payment provider's answer had no checkout page.", 502)
        return Checkout(out["id"], out["url"])

    def verify(self, headers: dict[str, str], body: bytes) -> Paid | None:
        sig = headers.get("stripe-signature", "")
        parts: dict[str, list[str]] = {}
        for item in sig.split(","):
            k, _, v = item.strip().partition("=")
            parts.setdefault(k, []).append(v)
        try:
            t = int(parts.get("t", [""])[0])
        except ValueError as e:
            raise BillingError("Bad signature.", 400) from e
        if abs(time.time() - t) > STRIPE_TOLERANCE_S:
            raise BillingError("The signature is too old.", 400)
        want = hmac.new(webhook_secret("stripe").encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(want, v) for v in parts.get("v1", [])):
            raise BillingError("Bad signature.", 400)
        event = json.loads(body)
        if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            return None
        obj = (event.get("data") or {}).get("object") or {}
        return Paid(
            event_id=str(event.get("id")),
            payment_id=str(obj.get("client_reference_id") or (obj.get("metadata") or {}).get("payment_id") or ""),
            reference=str(obj.get("id") or ""),
            amount=Decimal(int(obj.get("amount_total") or 0)) / 100,
            currency=str(obj.get("currency") or "").upper(),
            approved=obj.get("payment_status") == "paid",
        )


def stripe_signature(body: bytes, secret: str, t: int | None = None) -> str:
    """The Stripe-Signature header for a body (the simulated stand-in and tests)."""
    t = int(time.time()) if t is None else t
    return f"t={t},v1={hmac.new(secret.encode(), f'{t}.'.encode() + body, hashlib.sha256).hexdigest()}"


class HostedPageGateway:
    def __init__(self, live: bool):
        self.live = live

    def create(self, payment: dict, invoice: dict, return_url: str) -> Checkout:
        if not self.live:
            ref = f"hp_sim_{uuid.uuid4().hex}"
            return Checkout(ref, f"{return_url}&simulated={ref}")
        currency = ISO_NUMERIC.get(invoice["currency"])
        if currency is None:
            raise BillingError(f"The hosted payment page doesn't take {invoice['currency']}.", 409)
        body = {
            "TransactionIdentifier": str(payment["id"]),
            "OrderIdentifier": invoice["number"],
            "TotalAmount": f"{dec(invoice['total']):.2f}",
            "CurrencyCode": currency,
            "ExtendedData": {"MerchantResponseUrl": return_url},
        }
        base = _env("EXA_HOSTED_PAY_URL").rstrip("/")
        out = _post(
            f"{base}/hosted-page",
            json.dumps(body).encode(),
            {
                "Content-Type": "application/json",
                "PowerTranz-PowerTranzId": _env("EXA_HOSTED_PAY_MERCHANT_ID"),
                "PowerTranz-PowerTranzPassword": _env("EXA_HOSTED_PAY_PASSWORD"),
            },
        )
        url = out.get("RedirectUrl") or out.get("HostedPageUrl")
        if not url:
            raise BillingError("The payment provider's answer had no payment page.", 502)
        return Checkout(str(out.get("TransactionIdentifier") or payment["id"]), url)

    def verify(self, headers: dict[str, str], body: bytes) -> Paid | None:
        want = hmac.new(webhook_secret("hosted").encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, headers.get("x-signature", "")):
            raise BillingError("Bad signature.", 400)
        ev = json.loads(body)
        code = str(ev.get("CurrencyCode") or "")
        currency = next((k for k, v in ISO_NUMERIC.items() if v == code), code)
        return Paid(
            event_id=str(ev.get("EventId") or ev.get("RRN") or ev.get("TransactionIdentifier")),
            payment_id=str(ev.get("TransactionIdentifier") or ""),
            reference=str(ev.get("TransactionIdentifier") or ""),
            amount=dec(ev.get("TotalAmount") or 0),
            currency=currency,
            approved=bool(ev.get("Approved")),
        )


def hosted_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def gateway(provider: str) -> Gateway:
    mode = provider_mode(provider)
    if mode == "off":
        raise BillingError("Online payment is switched off.", 409)
    live = mode == "live"
    return StripeGateway(live) if provider == "stripe" else HostedPageGateway(live)


# ---- recording payments ----------------------------------------------------------------


def start(conn: psycopg.Connection, invoice: dict, provider: str, actor: str, portal_url: str) -> dict:
    """Open a payment for an issued, unpaid invoice and return where to pay."""
    gw = gateway(provider)
    if invoice["status"] != "issued":
        raise BillingError("Only an issued invoice can be paid.", 409)
    if invoice.get("paid_at"):
        raise BillingError("This invoice is already paid.", 409)
    if dec(invoice["total"]) <= 0:
        raise BillingError("Nothing to pay on this invoice.", 409)
    payment = conn.execute(
        """INSERT INTO billing_payments (invoice_id, customer_id, provider, mode, amount, currency, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            invoice["id"],
            invoice["customer_id"],
            provider,
            provider_mode(provider),
            invoice["total"],
            invoice["currency"],
            actor,
        ),
    ).fetchone()
    return_url = f"{portal_url.rstrip('/')}/billing/invoices/{invoice['id']}?payment={payment['id']}"
    co = gw.create(payment, invoice, return_url)
    conn.execute(
        "UPDATE billing_payments SET reference = %s, url = %s WHERE id = %s", (co.reference, co.url, payment["id"])
    )
    audit.record(
        conn,
        actor,
        "billing.payment.start",
        invoice["number"],
        invoice["customer_id"],
        {"payment_id": str(payment["id"]), "provider": provider, "mode": payment["mode"]},
    )
    return {
        "payment_id": payment["id"],
        "provider": provider,
        "mode": payment["mode"],
        "url": co.url,
        "reference": co.reference,
    }


def webhook(conn: psycopg.Connection, provider: str, headers: dict[str, str], body: bytes) -> dict:
    """A signed event from a gateway. Only this path marks a payment paid."""
    if provider_mode(provider) == "off":
        raise BillingError("Online payment is switched off.", 404)
    gw = gateway(provider)
    try:
        paid = gw.verify({k.lower(): v for k, v in headers.items()}, body)
    except (ValueError, json.JSONDecodeError) as e:
        if isinstance(e, BillingError):
            raise
        raise BillingError("The event could not be read.", 400) from e
    if paid is None:
        return {"handled": False}
    seen = conn.execute(
        """INSERT INTO billing_payment_events (provider, event_id) VALUES (%s, %s)
           ON CONFLICT DO NOTHING RETURNING event_id""",
        (provider, paid.event_id),
    ).fetchone()
    if seen is None:
        return {"handled": False, "duplicate": True}
    try:
        uuid.UUID(paid.payment_id)
    except ValueError:
        return {"handled": False}
    pay = conn.execute(
        "SELECT * FROM billing_payments WHERE id = %s AND provider = %s FOR UPDATE", (paid.payment_id, provider)
    ).fetchone()
    if pay is None:
        return {"handled": False}
    inv = conn.execute(
        "SELECT status, total, currency, number FROM billing_invoices WHERE id = %s", (pay["invoice_id"],)
    ).fetchone()
    detail = {"event_id": paid.event_id, "amount": f"{paid.amount:.2f}", "currency": paid.currency}
    if not paid.approved:
        new = "failed"
    elif paid.amount != dec(inv["total"]) or paid.currency != inv["currency"] or inv["status"] != "issued":
        new = "mismatch"  # kept for a person to look at; the invoice is not marked paid
    else:
        new = "paid"
    if pay["status"] != "paid":
        conn.execute(
            """UPDATE billing_payments SET status = %s, event_id = %s, detail = %s,
                 paid_at = CASE WHEN %s = 'paid' THEN now() END WHERE id = %s""",
            (new, paid.event_id, Jsonb(detail), new, pay["id"]),
        )
        audit.record(
            conn,
            f"gateway:{provider}",
            f"billing.payment.{new}",
            inv["number"] or "",
            pay["customer_id"],
            {"payment_id": str(pay["id"]), **detail},
        )
    return {"handled": True, "status": new if pay["status"] != "paid" else "paid"}


def payments_for(conn: psycopg.Connection, invoice_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT id, provider, mode, status, amount, currency, created_at, paid_at FROM billing_payments
           WHERE invoice_id = %s ORDER BY created_at DESC""",
        (invoice_id,),
    ).fetchall()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
