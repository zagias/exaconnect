"""Billing phase 1 (ADR 0022) against a hand-worked example.

Demo Organisation holds both plans for the month just ended.

CONNECT, on Connect Standard with its own price list (effective from the start of
the billed month): site fee 200.00 a month, commit 8.00 per Mbps, burst 10.00 per Mbps
over commit, satellite data 3.00 per GB, circuits at their own price, tax 10%,
SLA credits: below 99.9% -> 5%, below 99.5% -> 10%, below 99% -> 25%, capped at 30%.

Inventory (the lab seed): site-a and site-b, each with carrier A (commit 100 Mbps),
carrier B (50) and satellite (20). The PoP is not billed to the customer.

  Site fees            2 x 200.00                                      =   400.00
  Commit               2 x (100 + 50 + 20) Mbps x 8.00                 = 2,720.00
  Burst, site-a A      20 samples 10..200 Mbps in (out half); 5% of 20 = 1 discarded,
                       95th = 190; 190 - 100 = 90 Mbps x 10.00         =   900.00
  Satellite, site-a    4 samples of 8 in + 2 out Mbps for 300 s = 0.375 GB each,
                       1.5 GB x 3.00                                   =     4.50
  Circuit              10 Mbps for 73 h + 20 Mbps for 36.5 h = 1,460 Mbps-hours
                       at 2.00 per Mbps a month / 730 h                =     4.00
                                                             Charges     4,028.50
  Credits  site-a voice     987 of 1000 windows (loss 3% > 1%) = 98.7%: 25% of 200 = -50.00
           site-a business  same windows (3% > 2%) = 98.7%: 25% = 50, capped at 30% (60) = -10.00
           site-b voice     994 of 1000 (jitter 40 > 30 ms) = 99.4%: 10% of 200 = -20.00
           site-b business  jitter has no limit: 100%, no credit; bulk is best effort
                                                             Credits       -80.00
                                                             Subtotal    3,948.50
                                                             Tax 10%       394.85
                                                             Total       4,343.35

COMMAI, on CommAI Standard with its own price list: plan fee 50.00 a month,
ai_reply 0.02, message_out:* 0.01, tax 10%. Copilot is not priced on it.

  Plan fee                                                             =    50.00
  AI replies           1,000 x 0.02                                    =    20.00
  Copilot              5, not priced                                   =     0.00
  Outbound SMS         200 x 0.01 (the message_out:* price)            =     2.00
  Outbound WhatsApp    300 x 0.01                                      =     3.00
  Voice calls          rated by voice billing: 0.15 + 0.30             =     0.45
  Voice users          Reception, a full month on the example card     =    12.00
  (99 voice minutes in usage_records are voice's to rate, not the plan's)
                                                             Charges        87.45
                                                             Tax 10%         8.75 (8.745 half up)
                                                             Total          96.20

Issued in that order: Connect is EXA-<year>-0001, CommAI EXA-<year>-0002.
"""

import csv
import datetime as dt
import io
from decimal import Decimal

import psycopg
import pytest

from exaconnect_controller import db
from exaconnect_controller.billing import core

from .billing_helpers import (
    TABLE,
    add_user,
    build_commai_month,
    build_connect_month,
    lab,
    login,
    months,
    new_customer,
    plan_id,
)

# ---- pure arithmetic -------------------------------------------------------------------


def test_credit_tiers():
    t = core.validate_credit_table(list(reversed(TABLE)))
    assert [r["below_pct"] for r in t] == [99.9, 99.5, 99.0]
    assert core.credit_for(Decimal("100"), t) is None
    assert core.credit_for(Decimal("99.9"), t) is None  # at the threshold is met
    assert core.credit_for(Decimal("99.89"), t)["credit_pct"] == 5
    assert core.credit_for(Decimal("99.4"), t)["credit_pct"] == 10
    assert core.credit_for(Decimal("98.7"), t)["credit_pct"] == 25
    assert core.credit_for(Decimal("0"), t)["credit_pct"] == 25
    with pytest.raises(core.BillingError):
        core.validate_credit_table([{"below_pct": 99.9, "credit_pct": 10}, {"below_pct": 99, "credit_pct": 5}])
    with pytest.raises(core.BillingError):
        core.validate_credit_table([{"below_pct": 101, "credit_pct": 5}])
    with pytest.raises(core.BillingError):
        core.validate_credit_table([{"below_pct": 99, "credit_pct": 5}, {"below_pct": 99, "credit_pct": 6}])
    with pytest.raises(core.BillingError):
        core.validate_credit_table("not a list")


def test_pct_text_never_rounds_up_to_the_threshold():
    assert core.pct_text(Decimal("98.7")) == "98.7"
    assert core.pct_text(Decimal("99.4999")) == "99.49"
    assert core.pct_text(99) == "99"
    assert core.pct_text(Decimal("66.666")) == "66.66"


def test_active_days_and_periods():
    sep, octo = dt.date(2026, 9, 1), dt.date(2026, 10, 1)
    assert core.active_fraction(dt.datetime(2026, 1, 5, tzinfo=dt.UTC), sep, octo) == (30, 30)
    assert core.active_fraction(dt.datetime(2026, 9, 11, 15, tzinfo=dt.UTC), sep, octo) == (20, 30)
    # A subscription covering 1 to 10 September, a site added on the 5th: 6 of 30 days.
    assert core.days_active(dt.datetime(2026, 9, 5, tzinfo=dt.UTC), sep, octo, sep, dt.date(2026, 9, 11)) == (6, 30)
    assert core.days_active(None, sep, octo, dt.date(2026, 9, 11), octo) == (20, 30)
    assert core.parse_period("2026-09") == sep and core.next_month(dt.date(2026, 12, 1)) == dt.date(2027, 1, 1)
    assert core.period_label(sep) == "September 2026"
    for bad in ("2026-13", "26-09", "", "2026-9"):
        with pytest.raises(core.BillingError):
            core.parse_period(bad)
    with pytest.raises(core.BillingError):
        core.dec("NaN")
    with pytest.raises(core.BillingError):
        core.dec("ten")


def test_totals_and_numbering():
    lines = [
        {"kind": "site", "amount": "200.00"},
        {"kind": "commit", "amount": "33.33"},
        {"kind": "credit", "amount": "-20.00"},
    ]
    t = core.totals(lines, "12.5")
    assert (t["charges"], t["credits"], t["subtotal"], t["tax"], t["total"]) == (
        Decimal("233.33"),
        Decimal("-20.00"),
        Decimal("213.33"),
        Decimal("26.67"),  # 26.66625 rounds half up
        Decimal("240.00"),
    )
    # Credits larger than charges: no tax on a negative subtotal.
    t = core.totals([{"kind": "site", "amount": "10"}, {"kind": "credit", "amount": "-15"}], "10")
    assert (t["subtotal"], t["tax"], t["total"]) == (Decimal("-5.00"), Decimal("0.00"), Decimal("-5.00"))
    assert core.invoice_number(2026, None) == "EXA-2026-0001"
    assert core.invoice_number(2026, 41) == "EXA-2026-0042"


# ---- the hand-worked month, end to end -------------------------------------------------


def _strip(inv):
    return [(x["position"], x["plan"], x["kind"], x["description"], x["amount"]) for x in inv["lines"]]


def test_billing_hand_worked_month(client, admin_headers):
    cid = lab(client)["customer_id"]
    billed, this_month, start = months()
    period = f"{billed:%Y-%m}"
    year = dt.datetime.now(dt.UTC).year
    build_connect_month(cid, start)
    build_commai_month(cid, start)
    with db.tx() as conn:
        # The lab seed put Demo on Connect Standard from the day it was added (today); move that back.
        conn.execute("UPDATE subscriptions SET starts_on = %s WHERE customer_id = %s", (billed.replace(year=2025), cid))
        connect, commai = plan_id(conn, "connect"), plan_id(conn, "commai")
        add_user(conn, "it@demo.example", "customer", cid)
    demo_h = login(client, "it@demo.example")

    # Demo takes CommAI from the first day of the billed month.
    sub = {"plan_id": commai, "starts_on": billed.isoformat()}
    r = client.post(f"/api/v1/customers/{cid}/plans", json=sub, headers=admin_headers)
    assert r.status_code == 201, r.text
    held = client.get(f"/api/v1/customers/{cid}/plans", headers=demo_h).json()
    assert held["products"] == ["connect", "commai"] and len(held["subscriptions"]) == 2

    # Price lists: every save is a new version on its plan.
    body = {
        "plan_id": connect,
        "customer_id": cid,
        "effective_from": billed.isoformat(),
        "label": "Demo contract",
        "currency": "usd",
        "site_monthly": "200",
        "commit_per_mbps": "8",
        "burst_per_mbps": "10",
        "satellite_per_gb": "3",
        "tax_rate_pct": "10",
        "sla_credits": TABLE,
        "credit_cap_pct": "30",
    }
    r = client.post("/api/v1/billing/price-lists", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert (v1["version"], v1["currency"], v1["site_monthly"], v1["plan"]) == (1, "USD", "200.0000", "Connect Standard")
    # Version 2 takes effect this month: the billed month keeps version 1.
    r = client.post(
        "/api/v1/billing/price-lists",
        json={**body, "effective_from": this_month.isoformat(), "site_monthly": "999"},
        headers=admin_headers,
    )
    assert r.json()["version"] == 2
    cbody = {
        "plan_id": commai,
        "customer_id": cid,
        "effective_from": billed.isoformat(),
        "monthly_fee": "50",
        "meter_prices": {"ai_reply": "0.02", "message_out:*": "0.01"},
        "tax_rate_pct": "10",
    }
    r = client.post("/api/v1/billing/price-lists", json=cbody, headers=admin_headers)
    assert r.status_code == 201 and r.json()["version"] == 1, r.text
    for bad in (
        {**body, "commit_per_mbps": "-1"},
        {**body, "meter_prices": {"ai_reply": "1"}},  # meters belong on CommAI lists
        {**cbody, "site_monthly": "5"},  # sites belong on Connect lists
        {**cbody, "meter_prices": {"voice_minute": "0.1"}},  # voice rates itself
        {**cbody, "meter_prices": {"Bad Meter!": "0.1"}},
        {**body, "currency": "US"},
        {**body, "tax_rate_pct": "101"},
    ):
        assert client.post("/api/v1/billing/price-lists", json=bad, headers=admin_headers).status_code == 422, bad
    assert (
        client.post(
            "/api/v1/billing/price-lists",
            json={**body, "plan_id": "00000000-0000-0000-0000-000000000000"},
            headers=admin_headers,
        ).status_code
        == 404
    )
    lists = client.get(f"/api/v1/billing/price-lists?customer_id={cid}", headers=admin_headers).json()
    assert [(p["plan"], p["version"]) for p in lists["customer"]] == [
        ("CommAI Standard", 1),
        ("Connect Standard", 2),
        ("Connect Standard", 1),
    ]
    assert all(p["example"] for p in lists["plans"])  # the plans' own lists are examples until prices are agreed
    assert {x["plan"]: x["price_list"]["version"] for x in lists["in_effect"]} == {
        "Connect Standard": 2,
        "CommAI Standard": 1,
    }

    # The month's charges, worked out live, one block per plan.
    ch = client.get(f"/api/v1/billing/charges?customer_id={cid}&period={period}", headers=admin_headers)
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    assert ch["products"] == ["connect", "commai"] and ch["totals"] == [{"currency": "USD", "total": "4439.55"}]
    con, com = ch["plans"]
    assert con["plan"] == "Connect Standard" and con["price_list"]["version"] == 1 and con["complete"] is True
    assert con["example"] is True  # the plan itself is still marked as an example
    assert [(x["kind"], x["amount"]) for x in con["lines"]] == [
        ("site", "200.00"),
        ("commit", "800.00"),
        ("burst", "900.00"),
        ("commit", "400.00"),
        ("commit", "160.00"),
        ("satellite", "4.50"),
        ("site", "200.00"),
        ("commit", "800.00"),
        ("commit", "400.00"),
        ("commit", "160.00"),
        ("circuit", "4.00"),
        ("credit", "-50.00"),
        ("credit", "-10.00"),
        ("credit", "-20.00"),
    ]
    assert all(x["plan"] == "Connect Standard" and x["product"] == "connect" for x in con["lines"])
    assert (con["charges"], con["credits"], con["subtotal"], con["tax"], con["total"]) == (
        "4028.50",
        "-80.00",
        "3948.50",
        "394.85",
        "4343.35",
    )
    burst = next(x for x in con["lines"] if x["kind"] == "burst")
    assert burst["quantity"] == "90.000000" and burst["unit_price"] == "10.000000"
    assert burst["inputs"]["samples"] == 20 and burst["inputs"]["discarded"] == 1
    assert burst["inputs"]["billable_mbps"] == 190.0 and "settlement.csv" in burst["inputs"]["samples_csv"]
    assert "95th percentile 190 Mbps is 90 Mbps over the 100 Mbps commit" in burst["description"]
    sat = next(x for x in con["lines"] if x["kind"] == "satellite")
    assert sat["quantity"] == "1.500000" and "1.5 GB of satellite data" in sat["description"]
    circ = next(x for x in con["lines"] if x["kind"] == "circuit")
    assert circ["quantity"] == "1460.000000"
    assert circ["description"] == "Fabric virtual circuit dr-link, elastic bandwidth: 1460 Mbps-hours"
    assert circ["inputs"]["metered"] == "hourly" and len(circ["inputs"]["segments"]) == 2
    assert [x["description"] for x in con["lines"] if x["kind"] == "credit"] == [
        "SLA credit: Voice at site-a met its SLA 98.7% of the month against 99%, so 25% of the site fee",
        "SLA credit: Business at site-a met its SLA 98.7% of the month against 99%, so 25% of the site fee,"
        " capped at 30%",
        "SLA credit: Voice at site-b met its SLA 99.4% of the month against 99.5%, so 10% of the site fee",
    ]
    voice_a = next(x for x in con["lines"] if x["kind"] == "credit")
    assert (voice_a["inputs"]["windows"], voice_a["inputs"]["met"]) == (1000, 987)

    assert com["plan"] == "CommAI Standard"
    assert [(x["kind"], x["description"], x["amount"]) for x in com["lines"]] == [
        ("plan", "CommAI Standard: monthly plan fee", "50.00"),
        ("usage", "AI replies: 1000", "20.00"),
        ("usage", "Copilot suggestions: 5 (not priced on this plan)", "0.00"),
        ("usage", "Outbound messages (sms): 200", "2.00"),
        ("usage", "Outbound messages (whatsapp): 300", "3.00"),
        ("voice", "Voice calls: 30 minutes (2 rated charges)", "0.45"),
        ("voice", "Voice users: 1 user-months (1 rated charge)", "12.00"),
    ]
    assert (com["charges"], com["subtotal"], com["tax"], com["total"]) == ("87.45", "87.45", "8.75", "96.20")
    sms = next(x for x in com["lines"] if "sms" in x["description"])
    assert sms["inputs"]["price_key"] == "message_out:*" and sms["inputs"]["records"] == 200
    calls = next(x for x in com["lines"] if x["kind"] == "voice")
    assert calls["inputs"]["source"] == "voice_charges" and calls["inputs"]["rated_amount"] == "0.4500"

    # Customers see their own running charges.
    assert client.get(f"/api/v1/billing/charges?period={period}", headers=demo_h).json()["totals"][0]["total"] == (
        "4439.55"
    )

    # Drafts: one per plan, generated, then generated again with identical lines on the same invoices.
    gen = {"period": period, "customer_id": cid}
    d1 = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers)
    assert d1.status_code == 200, d1.text
    d1 = d1.json()["drafts"]
    d2 = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers).json()["drafts"]
    assert [d["id"] for d in d1] == [d["id"] for d in d2] and [d["plan"] for d in d2] == [
        "Connect Standard",
        "CommAI Standard",
    ]
    assert [_strip(d) for d in d1] == [_strip(d) for d in d2]
    cinv, minv = d2
    assert cinv["status"] == "draft" and cinv["number"] is None and len(cinv["lines"]) == 14
    assert (cinv["charges"], cinv["credits"], cinv["subtotal"], cinv["tax_rate_pct"], cinv["tax"], cinv["total"]) == (
        "4028.50",
        "-80.00",
        "3948.50",
        "10.000",
        "394.85",
        "4343.35",
    )
    assert (minv["product"], minv["total"], len(minv["lines"])) == ("commai", "96.20", 7)
    assert (cinv["covered_from"], cinv["covered_to"]) == (billed.isoformat(), this_month.isoformat())
    # Generating a product only touches that plan's invoice.
    only = client.post(
        "/api/v1/billing/invoices/generate", json={**gen, "product": "commai"}, headers=admin_headers
    ).json()
    assert [d["id"] for d in only["drafts"]] == [minv["id"]]
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM billing_invoices").fetchone()["n"] == 2
        audits = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'billing.invoice.draft' ORDER BY id"
        ).fetchall()
        assert [a["detail"]["rebuilt"] for a in audits] == [False, False, True, True, True]
        n = conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'billing.price_list.create'").fetchone()
        assert n["n"] == 3
        # Voice's monthly fee was made by voice billing itself, once, however often we draft.
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM voice_charges WHERE customer_id = %s AND kind = 'monthly_user'", (cid,)
            ).fetchone()["n"]
            == 1
        )

    # Customers never see drafts.
    assert client.get("/api/v1/billing/invoices", headers=demo_h).json() == []
    assert client.get(f"/api/v1/billing/invoices/{cinv['id']}", headers=demo_h).status_code == 404

    # Issue: numbered in order for the year across plans, then frozen.
    iss = client.post(f"/api/v1/billing/invoices/{cinv['id']}/issue", headers=admin_headers)
    assert iss.status_code == 200, iss.text
    iss = iss.json()
    assert iss["status"] == "issued" and iss["number"] == f"EXA-{year}-0001" and iss["total"] == "4343.35"
    iss2 = client.post(f"/api/v1/billing/invoices/{minv['id']}/issue", headers=admin_headers).json()
    assert iss2["number"] == f"EXA-{year}-0002" and iss2["plan"] == "CommAI Standard"
    assert client.post(f"/api/v1/billing/invoices/{cinv['id']}/issue", headers=admin_headers).status_code == 409
    again = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers).json()
    assert again["drafts"] == [] and all("Void it first" in s["reason"] for s in again["skipped"])
    assert [s["plan"] for s in again["skipped"]] == ["Connect Standard", "CommAI Standard"]
    for sql in (
        "UPDATE billing_invoices SET total = 1 WHERE id = %s",
        "UPDATE billing_invoices SET plan = 'Other' WHERE id = %s",
        "UPDATE billing_invoices SET status = 'draft' WHERE id = %s",
        "DELETE FROM billing_invoices WHERE id = %s",
        "UPDATE billing_invoice_lines SET amount = 0 WHERE invoice_id = %s",
        "DELETE FROM billing_invoice_lines WHERE invoice_id = %s",
        """INSERT INTO billing_invoice_lines (invoice_id, customer_id, position, product, plan, kind, description,
             quantity, unit, unit_price, amount) SELECT id, customer_id, 99, 'connect', 'x', 'site', 'x', 1, 'x', 1, 1
           FROM billing_invoices WHERE id = %s""",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), db.tx() as conn:
            conn.execute(sql, (cinv["id"],))
    with db.tx() as conn:
        row = conn.execute("SELECT total FROM billing_invoices WHERE id = %s", (cinv["id"],)).fetchone()
        assert str(row["total"]) == "4343.35"

    # The customer sees them now, with lines, the CSV and the printable page.
    mine = client.get("/api/v1/billing/invoices", headers=demo_h).json()
    assert sorted(i["number"] for i in mine) == [f"EXA-{year}-0001", f"EXA-{year}-0002"]
    assert [i["number"] for i in client.get("/api/v1/billing/invoices?product=commai", headers=demo_h).json()] == [
        f"EXA-{year}-0002"
    ]
    assert client.get(f"/api/v1/billing/invoices/{cinv['id']}", headers=demo_h).json()["total"] == "4343.35"
    text = client.get(f"/api/v1/billing/invoices/{cinv['id']}/csv", headers=demo_h).text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["invoice", f"EXA-{year}-0001"] and rows[3] == ["plan", "Connect Standard", "connect"]
    body_rows = [r for r in rows if r and r[0].isdigit()]
    assert len(body_rows) == 14 and all(r[1] == "Connect Standard" for r in body_rows)
    assert sum(Decimal(r[8]) for r in body_rows) == Decimal("3948.50")
    assert ["total", "4343.35"] in rows and ["tax", "394.85"] in rows
    page = client.get(f"/api/v1/billing/invoices/{minv['id']}/print", headers=demo_h)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert f"EXA-{year}-0002" in page.text and "96.20" in page.text and "CommAI Standard" in page.text
    assert "Example data" in page.text and "default-src 'none'" in page.headers["content-security-policy"]

    # Margin: what ExaCarib owes carriers for this customer's links (every link, the PoP's too,
    # at carrier prices): 3 sites x (100 x 4.00 + 50 x 2.50 + 20 x 12.00) = 2,295.00, plus
    # site-a carrier A's burst 90 Mbps x 6.00 = 540.00: 2,835.00 against 3,948.50 + 87.45 billed.
    m = client.get(f"/api/v1/billing/margin?period={period}", headers=admin_headers).json()
    demo = next(c for c in m["customers"] if c["customer_id"] == cid)
    assert (demo["revenue"], demo["carrier_cost"], demo["margin"], demo["margin_pct"]) == (
        "4035.95",
        "2835.00",
        "1200.95",
        "29.8",
    )
    assert [(p["plan"], p["source"], p["number"]) for p in demo["plans"]] == [
        ("Connect Standard", "issued", f"EXA-{year}-0001"),
        ("CommAI Standard", "issued", f"EXA-{year}-0002"),
    ]
    svc = {s["service"]: (s["revenue"], s["cost"], s["margin"]) for s in demo["services"]}
    assert svc == {
        "plan_fees": ("50.00", "0.00", "50.00"),
        "sites": ("400.00", "0.00", "400.00"),
        "connectivity": ("3624.50", "2835.00", "789.50"),
        "fabric": ("4.00", "0.00", "4.00"),
        "credits": ("-80.00", "0.00", "-80.00"),
        "commai_usage": ("25.00", "0.00", "25.00"),
        "voice": ("12.45", "0.00", "12.45"),
    }
    assert len(demo["carrier_lines"]) == 9

    # Void with a reason, then a new draft and the next number.
    assert client.post(f"/api/v1/billing/invoices/{cinv['id']}/void", json={}, headers=admin_headers).status_code == 422
    v = client.post(
        f"/api/v1/billing/invoices/{cinv['id']}/void", json={"reason": "Wrong commit on site-b"}, headers=admin_headers
    ).json()
    assert v["status"] == "void" and v["number"] == f"EXA-{year}-0001" and v["void_reason"] == "Wrong commit on site-b"
    r = client.post(f"/api/v1/billing/invoices/{cinv['id']}/void", json={"reason": "x"}, headers=admin_headers)
    assert r.status_code == 409
    d3 = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers).json()["drafts"]
    assert len(d3) == 1 and d3[0]["id"] != cinv["id"] and d3[0]["total"] == "4343.35"
    i3 = client.post(f"/api/v1/billing/invoices/{d3[0]['id']}/issue", headers=admin_headers).json()
    assert i3["number"] == f"EXA-{year}-0003"
    assert sorted(i["status"] for i in client.get("/api/v1/billing/invoices", headers=demo_h).json()) == [
        "issued",
        "issued",
        "void",
    ]

    # This month has not ended: a draft is fine, issuing it is not. Next month can't be drafted.
    cur = client.post(
        "/api/v1/billing/invoices/generate",
        json={"period": f"{this_month:%Y-%m}", "customer_id": cid, "product": "connect"},
        headers=admin_headers,
    ).json()["drafts"][0]
    assert cur["price_list_version"] == 2  # this month's own version
    r = client.post(f"/api/v1/billing/invoices/{cur['id']}/issue", headers=admin_headers)
    assert r.status_code == 409 and "hasn't ended yet" in r.json()["detail"]
    nxt = (this_month + dt.timedelta(days=32)).replace(day=1)
    r = client.post(
        "/api/v1/billing/invoices/generate", json={"period": f"{nxt:%Y-%m}", "customer_id": cid}, headers=admin_headers
    )
    assert r.status_code == 422
    # Voiding a draft needs no reason.
    assert client.post(f"/api/v1/billing/invoices/{cur['id']}/void", json={}, headers=admin_headers).status_code == 200
    assert client.get("/api/v1/billing/invoices/not-a-uuid", headers=admin_headers).status_code == 404


def test_connect_only_commai_only_and_all_at_once(client, admin_headers):
    billed, _, start = months()
    period = f"{billed:%Y-%m}"
    with db.tx() as conn:
        # Connect only: one site added 10 days before the month, on the example list: 150.00.
        conn_only = new_customer(conn, "Connect Only Ltd", start - dt.timedelta(days=10))
        chat_only = new_customer(conn, "Chat Only Ltd")
        neither = new_customer(conn, "Prospect Ltd", start - dt.timedelta(days=10))
        connect, commai = plan_id(conn, "connect"), plan_id(conn, "commai")
    for cid, pid in ((conn_only, connect), (chat_only, commai)):
        r = client.post(
            f"/api/v1/customers/{cid}/plans", json={"plan_id": pid, "starts_on": "2026-01-01"}, headers=admin_headers
        )
        assert r.status_code == 201, r.text
    assert client.get(f"/api/v1/customers/{conn_only}/plans", headers=admin_headers).json()["products"] == ["connect"]
    assert client.get(f"/api/v1/customers/{chat_only}/plans", headers=admin_headers).json()["products"] == ["commai"]
    assert client.get(f"/api/v1/customers/{neither}/plans", headers=admin_headers).json()["products"] == []

    out = client.post("/api/v1/billing/invoices/generate", json={"period": period}, headers=admin_headers).json()
    got = {
        (d["customer"], d["plan"]): (d["total"], d["example"], [x["kind"] for x in d["lines"]]) for d in out["drafts"]
    }
    assert got == {
        ("Chat Only Ltd", "CommAI Standard"): ("49.00", True, ["plan"]),
        ("Connect Only Ltd", "Connect Standard"): ("150.00", True, ["site"]),
    }
    assert out["skipped"] == []  # a customer without a plan is not billed at all

    # The CommAI-only organisation's running charges have no Connect block, and vice versa.
    ch = client.get(f"/api/v1/billing/charges?customer_id={chat_only}&period={period}", headers=admin_headers).json()
    assert [b["product"] for b in ch["plans"]] == ["commai"] and ch["example"] is True
    ch = client.get(f"/api/v1/billing/charges?customer_id={neither}&period={period}", headers=admin_headers).json()
    assert ch["plans"] == [] and ch["totals"] == []
