"""Billing phase 1 (ADR 0024) against a hand-worked example.

Price list (the Demo customer's own, effective from the start of the billed month):
  site fee 200.00 a month, commit 8.00 per Mbps, burst 10.00 per Mbps over commit,
  satellite data 3.00 per GB, circuits at their own price, tax 10%,
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
"""

import csv
import datetime as dt
import io
from decimal import Decimal

import psycopg
import pytest

from exaconnect_controller import db
from exaconnect_controller.billing import core
from exaconnect_controller.security import hash_password

from .test_flow import _enrol, _seed

# ---- pure arithmetic -------------------------------------------------------------------

TABLE = [
    {"below_pct": 99.9, "credit_pct": 5},
    {"below_pct": 99.5, "credit_pct": 10},
    {"below_pct": 99, "credit_pct": 25},
]


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


def test_pct_text_never_rounds_up_to_the_threshold():
    assert core.pct_text(Decimal("98.7")) == "98.7"
    assert core.pct_text(Decimal("99.4999")) == "99.49"
    assert core.pct_text(99) == "99"
    assert core.pct_text(Decimal("66.666")) == "66.66"


def test_active_fraction_and_periods():
    sep, octo = dt.date(2026, 9, 1), dt.date(2026, 10, 1)
    assert core.active_fraction(dt.datetime(2026, 1, 5, tzinfo=dt.UTC), sep, octo) == (30, 30)
    assert core.active_fraction(dt.datetime(2026, 9, 11, 15, tzinfo=dt.UTC), sep, octo) == (20, 30)
    assert core.parse_period("2026-09") == sep and core.next_month(dt.date(2026, 12, 1)) == dt.date(2027, 1, 1)
    assert core.period_label(sep) == "September 2026"
    with pytest.raises(core.BillingError):
        core.parse_period("2026-13")


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
    assert core.invoice_number(2026, None) == "EXA-2026-0001"
    assert core.invoice_number(2026, 41) == "EXA-2026-0042"


# ---- end to end ------------------------------------------------------------------------


def _login(client, email, password):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _user(conn, email, role, customer_id=None, carrier_id=None):
    conn.execute(
        "INSERT INTO users (email, password_hash, role, customer_id, carrier_id) VALUES (%s, %s, %s, %s, %s)",
        (email, hash_password("a long test password"), role, customer_id, carrier_id),
    )


def _metric(rows, at, cid, node, path, rtt, jitter, loss):
    rows.append((at, cid, node, path, 200, round(200 * (1 - loss / 100)), loss, rtt, rtt, rtt, jitter))


def _build(cid, start):
    """Inventory dates, usage samples, SLA windows and a circuit for the billed month."""
    with db.tx() as conn:
        conn.execute("UPDATE sites SET created_at = %s WHERE customer_id = %s", (start - dt.timedelta(days=365), cid))
        nodes = {
            r["name"]: (r["site"], r["node"])
            for r in conn.execute(
                "SELECT s.name, s.id AS site, n.id AS node FROM sites s JOIN nodes n ON n.site_id = s.id"
            ).fetchall()
        }
        links = {
            (r["site"], r["path"]): r
            for r in conn.execute(
                "SELECT l.id, l.carrier_id, s.name AS site, l.path FROM links l JOIN sites s ON s.id = l.site_id"
            ).fetchall()
        }
        usage = []
        a = links[("site-a", "carrier-a")]
        for k in range(20):  # 10, 20, ... 200 Mbps in; out is half
            usage.append(
                (a["id"], start + dt.timedelta(minutes=5 * k), cid, a["carrier_id"], 10.0 * (k + 1), 5.0 * (k + 1), 300)
            )
        sat = links[("site-a", "sat")]
        for k in range(4):
            usage.append(
                (sat["id"], start + dt.timedelta(hours=1, minutes=5 * k), cid, sat["carrier_id"], 8.0, 2.0, 300)
            )
        # Outside the month: never billed.
        usage.append((a["id"], start - dt.timedelta(minutes=5), cid, a["carrier_id"], 900.0, 900.0, 300))
        conn.cursor().executemany(
            """INSERT INTO usage_5m (link_id, bucket, customer_id, carrier_id, in_mbps, out_mbps, seconds)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            usage,
        )

        metrics = []
        site_a, node_a = nodes["site-a"]
        site_b, node_b = nodes["site-b"]
        for i in range(1000):
            at = start + dt.timedelta(seconds=10 * (i + 1))
            _metric(metrics, at, cid, node_a, "carrier-a", 25, 3, 3 if i < 13 else 0)
            _metric(metrics, at, cid, node_b, "carrier-b", 35, 40 if i < 6 else 4, 0)
        # A bad window ending exactly at midnight belongs to the month before.
        _metric(metrics, start, cid, node_b, "carrier-b", 900, 90, 50)
        conn.cursor().executemany(
            """INSERT INTO path_metrics (time, customer_id, node_id, path, sent, received, loss_pct,
                                         rtt_avg_ms, rtt_min_ms, rtt_max_ms, jitter_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            metrics,
        )
        for site, path in ((site_a, "carrier-a"), (site_b, "carrier-b")):
            for cls in ("voice", "business", "bulk"):
                conn.execute(
                    """INSERT INTO steering (site_id, class_name, customer_id, path, since) VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (site_id, class_name) DO UPDATE SET path = EXCLUDED.path""",
                    (site, cls, cid, path, start - dt.timedelta(days=30)),
                )

        circuit = conn.execute(
            """INSERT INTO circuits (customer_id, name, kind, bandwidth_mbps, price_per_mbps_month, created_by,
                                     created_at)
               VALUES (%s, 'dr-link', 'site', 20, 2.0, 'test', %s) RETURNING id""",
            (cid, start - dt.timedelta(days=1)),
        ).fetchone()["id"]
        t0 = start + dt.timedelta(days=1)
        t1 = t0 + dt.timedelta(hours=73)
        conn.execute(
            """INSERT INTO circuit_bandwidth (circuit_id, mbps, valid_from, valid_to, changed_by)
               VALUES (%s, 10, %s, %s, 'test'), (%s, 20, %s, %s, 'test')""",
            (circuit, t0, t1, circuit, t1, t1 + dt.timedelta(hours=36.5)),
        )


def test_billing_hand_worked_month(client, admin_headers):
    seeded = _seed()
    cid = seeded["customer_id"]
    for name in ("pop-miami", "site-a", "site-b"):
        _enrol(client, seeded["tokens"], name)
    now = dt.datetime.now(dt.UTC)
    this_month = now.date().replace(day=1)
    billed = (this_month - dt.timedelta(days=1)).replace(day=1)
    period = f"{billed:%Y-%m}"
    start = dt.datetime(billed.year, billed.month, 1, tzinfo=dt.UTC)
    _build(cid, start)

    # A second customer on the default (example) list, and users for both and a carrier.
    with db.tx() as conn:
        other = conn.execute("INSERT INTO customers (name) VALUES ('Other Organisation') RETURNING id").fetchone()["id"]
        conn.execute(
            """INSERT INTO sites (customer_id, name, kind, asn, overlay_host, created_at)
               VALUES (%s, 'hq', 'site', 65100, 21, %s)""",
            (other, start - dt.timedelta(days=10)),
        )
        _user(conn, "it@demo.example", "customer", cid)
        _user(conn, "it@other.example", "customer", other)
        carrier = conn.execute("SELECT id FROM carriers WHERE name = 'Carrier A'").fetchone()["id"]
        _user(conn, "noc@carrier-a.example", "carrier", carrier_id=carrier)
    demo_h = _login(client, "it@demo.example", "a long test password")
    other_h = _login(client, "it@other.example", "a long test password")
    carrier_h = _login(client, "noc@carrier-a.example", "a long test password")

    # Price lists: only admins write them; every save is a new version.
    body = {
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
    assert client.post("/api/v1/billing/price-lists", json=body, headers=demo_h).status_code == 403
    r = client.post("/api/v1/billing/price-lists", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert (v1["version"], v1["currency"], v1["site_monthly"]) == (1, "USD", "200.0000")
    # Version 2 takes effect this month: the billed month keeps version 1.
    r = client.post(
        "/api/v1/billing/price-lists",
        json={**body, "effective_from": this_month.isoformat(), "site_monthly": "999"},
        headers=admin_headers,
    )
    assert r.json()["version"] == 2
    bad = client.post("/api/v1/billing/price-lists", json={**body, "commit_per_mbps": "-1"}, headers=admin_headers)
    assert bad.status_code == 422
    lists = client.get(f"/api/v1/billing/price-lists?customer_id={cid}", headers=admin_headers).json()
    assert [p["version"] for p in lists["customer"]] == [2, 1]
    assert lists["default"][0]["example"] is True and lists["in_effect"]["version"] == 2

    # The month's charges, worked out live.
    ch = client.get(f"/api/v1/billing/charges?customer_id={cid}&period={period}", headers=admin_headers)
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    assert ch["price_list"]["version"] == 1 and ch["example"] is False and ch["complete"] is True
    got = [(x["kind"], x["amount"]) for x in ch["lines"]]
    assert got == [
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
    assert (ch["charges"], ch["credits"], ch["subtotal"], ch["tax"], ch["total"]) == (
        "4028.50",
        "-80.00",
        "3948.50",
        "394.85",
        "4343.35",
    )
    burst = next(x for x in ch["lines"] if x["kind"] == "burst")
    assert burst["quantity"] == "90.000000" and burst["unit_price"] == "10.000000"
    assert burst["inputs"]["samples"] == 20 and burst["inputs"]["discarded"] == 1
    assert burst["inputs"]["billable_mbps"] == 190.0 and "settlement.csv" in burst["inputs"]["samples_csv"]
    assert "95th percentile 190 Mbps is 90 Mbps over the 100 Mbps commit" in burst["description"]
    sat = next(x for x in ch["lines"] if x["kind"] == "satellite")
    assert sat["quantity"] == "1.500000" and "1.5 GB of satellite data" in sat["description"]
    circ = next(x for x in ch["lines"] if x["kind"] == "circuit")
    assert circ["quantity"] == "1460.000000" and circ["description"] == "Virtual circuit dr-link: 1460 Mbps-hours"
    credits = [x["description"] for x in ch["lines"] if x["kind"] == "credit"]
    assert credits == [
        "Voice at site-a met its SLA 98.7% of the month against 99%: 25% credit",
        "Business at site-a met its SLA 98.7% of the month against 99%: 25% credit, capped at 30% of the site fee",
        "Voice at site-b met its SLA 99.4% of the month against 99.5%: 10% credit",
    ]
    voice_a = next(x for x in ch["lines"] if x["kind"] == "credit")
    assert (voice_a["inputs"]["windows"], voice_a["inputs"]["met"]) == (1000, 987)

    # Customers see their own running charges only; carriers see nothing.
    assert client.get(f"/api/v1/billing/charges?period={period}", headers=demo_h).json()["total"] == "4343.35"
    assert client.get(f"/api/v1/billing/charges?customer_id={cid}", headers=other_h).status_code == 403
    assert client.get("/api/v1/billing/charges", headers=carrier_h).status_code == 403
    assert client.get("/api/v1/billing/invoices", headers=carrier_h).status_code == 403
    assert client.get("/api/v1/billing/margin", headers=demo_h).status_code == 403

    # Draft: generated, then generated again with identical lines on the same invoice.
    gen = {"period": period, "customer_id": cid}
    assert client.post("/api/v1/billing/invoices/generate", json=gen, headers=demo_h).status_code == 403
    d1 = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers)
    assert d1.status_code == 200, d1.text
    d1 = d1.json()["drafts"][0]
    d2 = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers).json()["drafts"][0]
    assert d1["id"] == d2["id"] and d2["status"] == "draft" and d2["number"] is None
    strip = lambda inv: [(x["position"], x["kind"], x["description"], x["amount"]) for x in inv["lines"]]  # noqa: E731
    assert strip(d1) == strip(d2) and len(d2["lines"]) == 14
    assert (d2["charges"], d2["credits"], d2["subtotal"], d2["tax_rate_pct"], d2["tax"], d2["total"]) == (
        "4028.50",
        "-80.00",
        "3948.50",
        "10.000",
        "394.85",
        "4343.35",
    )
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM billing_invoices").fetchone()["n"] == 1
        audits = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'billing.invoice.draft' ORDER BY id"
        ).fetchall()
        assert [a["detail"]["rebuilt"] for a in audits] == [False, True]
        assert (
            conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'billing.price_list.create'").fetchone()[
                "n"
            ]
            == 2
        )

    # Customers never see drafts.
    assert client.get("/api/v1/billing/invoices", headers=demo_h).json() == []
    assert client.get(f"/api/v1/billing/invoices/{d2['id']}", headers=demo_h).status_code == 404

    # Issue: numbered in order for the year, then frozen.
    year = now.year
    assert client.post(f"/api/v1/billing/invoices/{d2['id']}/issue", headers=demo_h).status_code == 403
    iss = client.post(f"/api/v1/billing/invoices/{d2['id']}/issue", headers=admin_headers)
    assert iss.status_code == 200, iss.text
    iss = iss.json()
    assert iss["status"] == "issued" and iss["number"] == f"EXA-{year}-0001" and iss["total"] == "4343.35"
    assert client.post(f"/api/v1/billing/invoices/{d2['id']}/issue", headers=admin_headers).status_code == 409
    again = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers)
    assert again.status_code == 409 and "Void it first" in again.json()["detail"]
    for sql in (
        "UPDATE billing_invoices SET total = 1 WHERE id = %s",
        "DELETE FROM billing_invoices WHERE id = %s",
        "UPDATE billing_invoice_lines SET amount = 0 WHERE invoice_id = %s",
        "DELETE FROM billing_invoice_lines WHERE invoice_id = %s",
        """INSERT INTO billing_invoice_lines (invoice_id, customer_id, position, kind, description, quantity, unit,
             unit_price, amount) SELECT id, customer_id, 99, 'site', 'x', 1, 'x', 1, 1 FROM billing_invoices
           WHERE id = %s""",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), db.tx() as conn:
            conn.execute(sql, (d2["id"],))
    with db.tx() as conn:
        assert (
            str(conn.execute("SELECT total FROM billing_invoices WHERE id = %s", (d2["id"],)).fetchone()["total"])
            == "4343.35"
        )

    # The customer sees it now, with its lines and the CSV; the other customer can't.
    mine = client.get("/api/v1/billing/invoices", headers=demo_h).json()
    assert [i["number"] for i in mine] == [f"EXA-{year}-0001"]
    assert client.get(f"/api/v1/billing/invoices/{d2['id']}", headers=demo_h).json()["total"] == "4343.35"
    assert client.get(f"/api/v1/billing/invoices/{d2['id']}", headers=other_h).status_code == 404
    assert client.get(f"/api/v1/billing/invoices/{d2['id']}/csv", headers=other_h).status_code == 404
    assert client.get("/api/v1/billing/invoices", headers=other_h).json() == []
    assert client.get(f"/api/v1/billing/invoices?customer_id={cid}", headers=other_h).status_code == 403
    text = client.get(f"/api/v1/billing/invoices/{d2['id']}/csv", headers=demo_h).text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["invoice", f"EXA-{year}-0001"]
    body_rows = [r for r in rows if r and r[0].isdigit()]
    assert len(body_rows) == 14 and sum(Decimal(r[7]) for r in body_rows) == Decimal("3948.50")
    assert ["total", "4343.35"] in rows and ["tax", "394.85"] in rows

    # Margin: what ExaCarib owes carriers for this customer's links (every link, the PoP's too,
    # at carrier prices): 3 sites x (100 x 4.00 + 50 x 2.50 + 20 x 12.00) = 2,295.00, plus
    # site-a carrier A's burst 90 Mbps x 6.00 = 540.00: 2,835.00 against 3,948.50 billed.
    m = client.get(f"/api/v1/billing/margin?period={period}", headers=admin_headers).json()
    demo = next(c for c in m["customers"] if c["customer_id"] == cid)
    assert (demo["revenue"], demo["carrier_cost"], demo["margin"], demo["margin_pct"]) == (
        "3948.50",
        "2835.00",
        "1113.50",
        "28.2",
    )
    assert demo["invoice_number"] == f"EXA-{year}-0001" and demo["revenue_source"] == "issued"
    assert len(demo["carrier_lines"]) == 9

    # Void with a reason, then a new draft and the next number.
    assert client.post(f"/api/v1/billing/invoices/{d2['id']}/void", json={}, headers=admin_headers).status_code == 422
    v = client.post(
        f"/api/v1/billing/invoices/{d2['id']}/void", json={"reason": "Wrong commit on site-b"}, headers=admin_headers
    ).json()
    assert v["status"] == "void" and v["number"] == f"EXA-{year}-0001" and v["void_reason"] == "Wrong commit on site-b"
    assert (
        client.post(
            f"/api/v1/billing/invoices/{d2['id']}/void", json={"reason": "x"}, headers=admin_headers
        ).status_code
        == 409
    )
    d3 = client.post("/api/v1/billing/invoices/generate", json=gen, headers=admin_headers).json()["drafts"][0]
    assert d3["id"] != d2["id"] and d3["total"] == "4343.35"
    i3 = client.post(f"/api/v1/billing/invoices/{d3['id']}/issue", headers=admin_headers).json()
    assert i3["number"] == f"EXA-{year}-0002"
    assert [i["status"] for i in client.get("/api/v1/billing/invoices", headers=demo_h).json()] == ["issued", "void"]

    # All customers at once: the Demo month is issued (skipped); the other customer gets a draft on the
    # default example list: one site added 10 days before the month, so a full month's fee of 150.00.
    allm = client.post("/api/v1/billing/invoices/generate", json={"period": period}, headers=admin_headers).json()
    assert [s["customer"] for s in allm["skipped"]] == ["Demo Organisation"]
    (od,) = allm["drafts"]
    assert od["customer"] == "Other Organisation" and od["example"] is True and od["total"] == "150.00"

    # This month has not ended: a draft is fine, issuing it is not. Next month can't be drafted.
    cur = client.post(
        "/api/v1/billing/invoices/generate",
        json={"period": f"{this_month:%Y-%m}", "customer_id": str(other)},
        headers=admin_headers,
    ).json()["drafts"][0]
    r = client.post(f"/api/v1/billing/invoices/{cur['id']}/issue", headers=admin_headers)
    assert r.status_code == 409 and "hasn't ended yet" in r.json()["detail"]
    nxt = (this_month + dt.timedelta(days=32)).replace(day=1)
    r = client.post(
        "/api/v1/billing/invoices/generate",
        json={"period": f"{nxt:%Y-%m}", "customer_id": str(other)},
        headers=admin_headers,
    )
    assert r.status_code == 422

    # Voiding a draft needs no reason.
    assert client.post(f"/api/v1/billing/invoices/{cur['id']}/void", json={}, headers=admin_headers).status_code == 200
    assert client.get("/api/v1/billing/invoices/not-a-uuid", headers=admin_headers).status_code == 404
