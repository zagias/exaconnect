"""Payment and accounting interfaces (ADR 0022): off by default, simulated
without credentials, live only against local fake gateways here. Nothing in
these tests may reach the internet."""

import csv
import datetime as dt
import io
import json
import time
import urllib.request
from decimal import Decimal

import pytest

from exaconnect_controller import db
from exaconnect_controller.billing import accounting, payments

from .billing_helpers import FakeGateway, add_user, fake_gateway, login, months, new_customer, plan_id

ENV = (
    "EXA_PAYMENTS",
    "EXA_PAYMENT_PROVIDERS",
    "EXA_STRIPE_SECRET_KEY",
    "EXA_STRIPE_WEBHOOK_SECRET",
    "EXA_STRIPE_API_BASE",
    "EXA_HOSTED_PAY_URL",
    "EXA_HOSTED_PAY_MERCHANT_ID",
    "EXA_HOSTED_PAY_PASSWORD",
    "EXA_HOSTED_PAY_SECRET",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def _issued(client, admin_headers, name="Chat Only Ltd", email="owner@chat.example"):
    """A CommAI-only organisation with one issued invoice of 49.00 (the example plan fee)."""
    billed, _, _ = months()
    with db.tx() as conn:
        cid = new_customer(conn, name)
        add_user(conn, email, "customer", cid)
        commai = plan_id(conn, "commai")
    client.post(
        f"/api/v1/customers/{cid}/plans", json={"plan_id": commai, "starts_on": "2026-01-01"}, headers=admin_headers
    )
    draft = client.post(
        "/api/v1/billing/invoices/generate",
        json={"period": f"{billed:%Y-%m}", "customer_id": cid},
        headers=admin_headers,
    ).json()["drafts"][0]
    return cid, draft, login(client, email)


def _issue(client, admin_headers, draft):
    r = client.post(f"/api/v1/billing/invoices/{draft['id']}/issue", headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


def _stripe_event(payment_id, cents, event_id, status="paid", currency="usd"):
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "client_reference_id": payment_id,
                    "amount_total": cents,
                    "currency": currency,
                    "payment_status": status,
                }
            },
        }
    ).encode()


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no network call may be made in simulated mode")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def test_payments_off_by_default_then_simulated(client, admin_headers, monkeypatch, no_network):
    cid, draft, owner = _issued(client, admin_headers)
    # A draft can't be paid, whatever the mode.
    monkeypatch.setenv("EXA_PAYMENTS", "simulated")
    r = client.post(f"/api/v1/billing/invoices/{draft['id']}/pay", json={"provider": "stripe"}, headers=admin_headers)
    assert r.status_code == 409 and "issued" in r.json()["detail"]
    monkeypatch.delenv("EXA_PAYMENTS")
    inv = _issue(client, admin_headers, draft)
    assert inv["total"] == "49.00"

    st = client.get("/api/v1/billing/payments/status", headers=admin_headers).json()
    assert st["mode"] == "off" and {p["mode"] for p in st["providers"]} == {"off"}
    r = client.post(f"/api/v1/billing/invoices/{inv['id']}/pay", json={"provider": "stripe"}, headers=owner)
    assert r.status_code == 409 and "switched off" in r.json()["detail"]
    assert client.post("/api/v1/billing/webhooks/hosted", content=b"{}").status_code == 404

    monkeypatch.setenv("EXA_PAYMENTS", "simulated")
    monkeypatch.setenv("EXA_STRIPE_WEBHOOK_SECRET", "whsec_test_only")
    st = client.get("/api/v1/billing/payments/status", headers=admin_headers).json()
    modes = {p["key"]: (p["mode"], p["missing_env"]) for p in st["providers"]}
    assert modes["stripe"] == ("simulated", ["EXA_STRIPE_SECRET_KEY"])
    assert modes["hosted"][0] == "simulated" and "EXA_HOSTED_PAY_SECRET" in modes["hosted"][1]
    assert "whsec_test_only" not in json.dumps(st)

    r = client.post(f"/api/v1/billing/invoices/{inv['id']}/pay", json={"provider": "stripe"}, headers=owner)
    assert r.status_code == 201, r.text
    pay = r.json()
    assert pay["mode"] == "simulated" and pay["reference"].startswith("cs_sim_") and "simulated=" in pay["url"]

    # Bad, missing and stale signatures are refused, and nothing is paid.
    body = _stripe_event(pay["payment_id"], 4900, "evt_1")
    for sig in ("", "t=1,v1=deadbeef", payments.stripe_signature(body, "wrong-secret")):
        r = client.post("/api/v1/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": sig})
        assert r.status_code == 400
    old = payments.stripe_signature(body, "whsec_test_only", int(time.time()) - 3600)
    assert (
        client.post("/api/v1/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": old}).status_code
        == 400
    )
    assert client.get(f"/api/v1/billing/invoices/{inv['id']}", headers=owner).json()["paid_at"] is None

    # A signed event for the wrong amount is kept for a person to look at; the invoice stays unpaid.
    wrong = _stripe_event(pay["payment_id"], 1000, "evt_wrong")
    r = client.post(
        "/api/v1/billing/webhooks/stripe",
        content=wrong,
        headers={"Stripe-Signature": payments.stripe_signature(wrong, "whsec_test_only")},
    )
    assert r.json() == {"handled": True, "status": "mismatch"}
    assert client.get(f"/api/v1/billing/invoices/{inv['id']}", headers=owner).json()["paid_at"] is None

    # Another organisation can't complete it; the owner (in simulated mode) can, through the signed webhook path.
    _, _, stranger = _issued(client, admin_headers, "Someone Else Ltd", "it@else.example")
    assert (
        client.post(f"/api/v1/billing/payments/{pay['payment_id']}/simulate", json={}, headers=stranger).status_code
        == 404
    )
    r = client.post(f"/api/v1/billing/payments/{pay['payment_id']}/simulate", json={}, headers=owner)
    assert r.status_code == 200 and r.json() == {"handled": True, "status": "paid"}, r.text
    got = client.get(f"/api/v1/billing/invoices/{inv['id']}", headers=owner).json()
    assert got["paid_at"] is not None and got["payments"][0]["status"] == "paid"

    # The same event delivered again is acted on once.
    good = _stripe_event(pay["payment_id"], 4900, "evt_again")
    hdr = {"Stripe-Signature": payments.stripe_signature(good, "whsec_test_only")}
    assert client.post("/api/v1/billing/webhooks/stripe", content=good, headers=hdr).json()["handled"] is True
    assert client.post("/api/v1/billing/webhooks/stripe", content=good, headers=hdr).json() == {
        "handled": False,
        "duplicate": True,
    }
    # A paid invoice can't be paid again or voided.
    assert (
        client.post(f"/api/v1/billing/invoices/{inv['id']}/pay", json={"provider": "hosted"}, headers=owner).status_code
        == 409
    )
    r = client.post(f"/api/v1/billing/invoices/{inv['id']}/void", json={"reason": "x"}, headers=admin_headers)
    assert r.status_code == 409 and "paid" in r.json()["detail"]

    # Hosted page, declined: the payment fails and the invoice is untouched.
    cid2, draft2, owner2 = _issued(client, admin_headers, "Declined Ltd", "it@declined.example")
    inv2 = _issue(client, admin_headers, draft2)
    hp = client.post(f"/api/v1/billing/invoices/{inv2['id']}/pay", json={"provider": "hosted"}, headers=owner2).json()
    assert hp["reference"].startswith("hp_sim_")
    r = client.post(f"/api/v1/billing/payments/{hp['payment_id']}/simulate", json={"approved": False}, headers=owner2)
    assert r.json()["status"] == "failed"
    assert client.get(f"/api/v1/billing/invoices/{inv2['id']}", headers=owner2).json()["paid_at"] is None
    with db.tx() as conn:
        actions = [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'billing.payment.%%' ORDER BY id")
        ]
    assert actions == [
        "billing.payment.start",
        "billing.payment.mismatch",
        "billing.payment.paid",
        "billing.payment.start",
        "billing.payment.failed",
    ]
    # Only the enabled providers are offered.
    monkeypatch.setenv("EXA_PAYMENT_PROVIDERS", "hosted")
    assert (
        client.post(
            f"/api/v1/billing/invoices/{inv2['id']}/pay", json={"provider": "stripe"}, headers=owner2
        ).status_code
        == 409
    )


def test_payments_live_against_fake_gateways(client, admin_headers, monkeypatch):
    srv = fake_gateway()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        cid, draft, owner = _issued(client, admin_headers)
        inv = _issue(client, admin_headers, draft)
        # Live without credentials stays simulated.
        monkeypatch.setenv("EXA_PAYMENTS", "live")
        st = {
            p["key"]: p["mode"]
            for p in client.get("/api/v1/billing/payments/status", headers=admin_headers).json()["providers"]
        }
        assert st == {"stripe": "simulated", "hosted": "simulated"}

        monkeypatch.setenv("EXA_STRIPE_SECRET_KEY", "sk_test_placeholder")
        monkeypatch.setenv("EXA_STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
        monkeypatch.setenv("EXA_STRIPE_API_BASE", base)
        r = client.post(f"/api/v1/billing/invoices/{inv['id']}/pay", json={"provider": "stripe"}, headers=owner)
        assert r.status_code == 201, r.text
        pay = r.json()
        assert pay == {
            **pay,
            "mode": "live",
            "reference": "cs_test_123",
            "url": "https://checkout.stripe.test/cs_test_123",
        }
        (req,) = FakeGateway.seen
        assert req["path"] == "/v1/checkout/sessions"
        assert req["headers"]["authorization"] == "Bearer sk_test_placeholder"
        assert req["headers"]["idempotency-key"] == f"exa-payment-{pay['payment_id']}"
        form = dict(x.split("=", 1) for x in req["body"].split("&"))
        assert form["line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D"] == "4900"
        assert form["line_items%5B0%5D%5Bprice_data%5D%5Bcurrency%5D"] == "usd"
        assert form["client_reference_id"] == pay["payment_id"]
        # A simulated completion is refused for a live payment: only the provider's signed event counts.
        assert (
            client.post(f"/api/v1/billing/payments/{pay['payment_id']}/simulate", json={}, headers=owner).status_code
            == 409
        )
        body = _stripe_event(pay["payment_id"], 4900, "evt_live_1")
        r = client.post(
            "/api/v1/billing/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": payments.stripe_signature(body, "whsec_placeholder")},
        )
        assert r.json() == {"handled": True, "status": "paid"}
        assert client.get(f"/api/v1/billing/invoices/{inv['id']}", headers=owner).json()["paid_at"]

        # Hosted payment page (First Atlantic Commerce / PowerTranz shape).
        cid2, draft2, owner2 = _issued(client, admin_headers, "Hosted Ltd", "it@hosted.example")
        inv2 = _issue(client, admin_headers, draft2)
        monkeypatch.setenv("EXA_HOSTED_PAY_URL", base)
        monkeypatch.setenv("EXA_HOSTED_PAY_MERCHANT_ID", "merchant-placeholder")
        monkeypatch.setenv("EXA_HOSTED_PAY_PASSWORD", "password-placeholder")
        monkeypatch.setenv("EXA_HOSTED_PAY_SECRET", "hosted-secret-placeholder")
        r = client.post(f"/api/v1/billing/invoices/{inv2['id']}/pay", json={"provider": "hosted"}, headers=owner2)
        assert r.status_code == 201, r.text
        hp = r.json()
        assert hp["mode"] == "live" and hp["url"] == "https://pay.fac.test/hp/1"
        req = FakeGateway.seen[-1]
        sent = json.loads(req["body"])
        assert req["path"] == "/hosted-page" and req["headers"]["powertranz-powertranzid"] == "merchant-placeholder"
        assert sent == {
            "TransactionIdentifier": hp["payment_id"],
            "OrderIdentifier": inv2["number"],
            "TotalAmount": "49.00",
            "CurrencyCode": "840",
            "ExtendedData": {"MerchantResponseUrl": sent["ExtendedData"]["MerchantResponseUrl"]},
        }
        cb = json.dumps(
            {
                "EventId": "fac-1",
                "TransactionIdentifier": hp["payment_id"],
                "TotalAmount": "49.00",
                "CurrencyCode": "840",
                "Approved": True,
            }
        ).encode()
        bad = client.post("/api/v1/billing/webhooks/hosted", content=cb, headers={"X-Signature": "00"})
        assert bad.status_code == 400
        r = client.post(
            "/api/v1/billing/webhooks/hosted",
            content=cb,
            headers={"X-Signature": payments.hosted_signature(cb, "hosted-secret-placeholder")},
        )
        assert r.json() == {"handled": True, "status": "paid"}

        # A gateway that can't be reached: a clear error and no payment recorded.
        cid3, draft3, owner3 = _issued(client, admin_headers, "Unreachable Ltd", "it@unreachable.example")
        inv3 = _issue(client, admin_headers, draft3)
        monkeypatch.setenv("EXA_STRIPE_API_BASE", "http://127.0.0.1:1")
        r = client.post(f"/api/v1/billing/invoices/{inv3['id']}/pay", json={"provider": "stripe"}, headers=owner3)
        assert r.status_code == 502 and "could not be reached" in r.json()["detail"]
        assert client.get(f"/api/v1/billing/invoices/{inv3['id']}", headers=owner3).json()["payments"] == []
    finally:
        srv.shutdown()


def test_accounting_exports(client, admin_headers):
    cid, draft, owner = _issued(client, admin_headers)
    r = client.get(f"/api/v1/billing/invoices/{draft['id']}/export/xero", headers=admin_headers)
    assert r.status_code == 409  # drafts are never exported
    inv = _issue(client, admin_headers, draft)
    x = client.get(f"/api/v1/billing/invoices/{inv['id']}/export/xero", headers=admin_headers).json()["Invoices"][0]
    assert (x["Type"], x["InvoiceNumber"], x["Contact"]["Name"], x["CurrencyCode"]) == (
        "ACCREC",
        inv["number"],
        "Chat Only Ltd",
        "USD",
    )
    assert x["Reference"].startswith("CommAI Standard") and x["Total"] == "49.00"
    assert sum(Decimal(li["LineAmount"]) for li in x["LineItems"]) == Decimal(x["SubTotal"])
    issued_on = dt.date.fromisoformat(x["Date"])
    assert dt.date.fromisoformat(x["DueDate"]) == issued_on + dt.timedelta(days=30)
    q = client.get(f"/api/v1/billing/invoices/{inv['id']}/export/quickbooks", headers=admin_headers).json()
    assert q["DocNumber"] == inv["number"] and q["TotalAmt"] == 49.0
    assert q["Line"][0]["SalesItemLineDetail"]["ItemRef"]["name"] == "CommAI"
    assert q["Line"][0]["DetailType"] == "SalesItemLineDetail"
    text = client.get(f"/api/v1/billing/invoices/{inv['id']}/export/csv", headers=admin_headers).text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == accounting.CSV_HEADER and rows[1][1] == inv["number"] and rows[1][8] == "49.00"
    assert client.get(f"/api/v1/billing/invoices/{inv['id']}/export/pdf", headers=admin_headers).status_code == 422


def test_books_lines_multiply_exactly():
    """Xero and QuickBooks work out quantity x unit price; part-month lines are sent
    as one unit at the line amount so the books match the invoice to the cent."""
    inv = {
        "status": "issued",
        "number": "EXA-2026-0009",
        "customer": "Harbour Bank",
        "plan": "Connect Standard",
        "product": "connect",
        "label": "September 2026",
        "currency": "USD",
        "issued_at": "2026-10-02T09:00:00+00:00",
        "tax_rate_pct": "12.5",
        "subtotal": "850.00",
        "tax": "106.25",
        "total": "956.25",
        "lines": [
            {
                "description": "Site fee: hq (10 of 30 days)",
                "quantity": "1",
                "unit": "site a month",
                "unit_price": "150",
                "amount": "50.00",
            },
            {
                "description": "Carrier A at hq: 100 Mbps commit",
                "quantity": "100",
                "unit": "Mbps a month",
                "unit_price": "8",
                "amount": "800.00",
            },
        ],
    }
    books = accounting.lines_for_books(inv)
    assert [(b["quantity"], b["unit_price"], b["amount"]) for b in books] == [
        (Decimal(1), Decimal("50.00"), Decimal("50.00")),
        (Decimal(1e2), Decimal(8), Decimal("800.00")),
    ]
    assert books[0]["description"] == "Site fee: hq (10 of 30 days) (1 site a month at 150)"
    x = accounting.xero(inv)["Invoices"][0]
    assert x["LineItems"][0]["TaxType"] == "OUTPUT" and x["DueDate"] == "2026-11-01"
    q = accounting.quickbooks(inv)
    assert q["Line"][1]["SalesItemLineDetail"]["TaxCodeRef"] == {"value": "TAX"}
    assert q["Line"][1]["SalesItemLineDetail"]["ItemRef"]["name"] == "Connect"
