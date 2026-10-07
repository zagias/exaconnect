"""Plans, subscriptions, the products helper, payables and margin, and who may
call each billing endpoint (ADR 0022)."""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from exaconnect_controller import db
from exaconnect_controller.billing import plans

from .billing_helpers import add_user, lab, login, months, new_customer, plan_id


def _money(v: Decimal) -> str:
    return f"{v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def test_plans_and_subscription_lifecycle(client, admin_headers):
    billed, this_month, start = months()
    with db.tx() as conn:
        cid = new_customer(conn, "Harbour Bank", start - dt.timedelta(days=400))
        standard, commai = plan_id(conn, "connect"), plan_id(conn, "commai")
        # The organisations work keeps customers.products; a new organisation with
        # no plan yet is left alone (NULL counts as both products).
        assert plans.has_products_column(conn) is True and plans.sync_products(conn, cid) is None

    # Plans: admins add and change them.
    r = client.post(
        "/api/v1/billing/plans",
        json={"product": "connect", "name": "Connect Plus", "description": "More sites"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    plus = r.json()["id"]
    assert (
        client.post(
            "/api/v1/billing/plans", json={"product": "connect", "name": "Connect Plus"}, headers=admin_headers
        ).status_code
        == 409
    )
    assert (
        client.post("/api/v1/billing/plans", json={"product": "fabric", "name": "X"}, headers=admin_headers).status_code
        == 422
    )
    r = client.patch(f"/api/v1/billing/plans/{plus}", json={"description": "Up to 50 sites"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["description"] == "Up to 50 sites"
    assert client.patch("/api/v1/billing/plans/nope", json={}, headers=admin_headers).status_code == 404
    r = client.post(
        "/api/v1/billing/price-lists",
        json={"plan_id": plus, "effective_from": "2026-01-01", "site_monthly": "300", "label": "Plus"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    listed = {p["name"]: p for p in client.get("/api/v1/billing/plans", headers=admin_headers).json()}
    assert set(listed) == {"Connect Standard", "Connect Plus", "CommAI Standard"}
    assert listed["Connect Plus"]["price_list"]["site_monthly"] == "300.0000"
    assert listed["CommAI Standard"]["price_list"]["monthly_fee"] == "49.0000"

    # Subscribe: one subscription per product at a time.
    base = f"/api/v1/customers/{cid}/plans"
    r = client.post(base, json={"plan_id": standard, "starts_on": "2026-01-01"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    sub = r.json()
    assert sub["state"] == "active" and sub["plan"] == "Connect Standard"
    r = client.post(base, json={"plan_id": plus, "starts_on": "2026-02-01"}, headers=admin_headers)
    assert r.status_code == 409 and "Change that plan instead" in r.json()["detail"]
    assert (
        client.post(base, json={"plan_id": "00000000-0000-0000-0000-000000000000"}, headers=admin_headers).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/customers/00000000-0000-0000-0000-000000000000/plans",
            json={"plan_id": standard},
            headers=admin_headers,
        ).status_code
        == 404
    )

    # Change to Plus from the 11th of the billed month.
    on = billed + dt.timedelta(days=10)
    bad = client.post(
        f"{base}/{sub['id']}/change", json={"plan_id": commai, "on": on.isoformat()}, headers=admin_headers
    )
    assert bad.status_code == 422 and "not a Connect plan" in bad.json()["detail"]
    same = client.post(f"{base}/{sub['id']}/change", json={"plan_id": standard}, headers=admin_headers)
    assert same.status_code == 409
    r = client.post(f"{base}/{sub['id']}/change", json={"plan_id": plus, "on": on.isoformat()}, headers=admin_headers)
    assert r.status_code == 200, r.text
    ch = r.json()
    assert ch["ended"]["ends_on"] == on.isoformat() and ch["ended"]["state"] == "ended"
    assert ch["started"]["starts_on"] == on.isoformat() and ch["started"]["plan"] == "Connect Plus"
    summary = client.get(base, headers=admin_headers).json()
    assert summary["products"] == ["connect"]
    assert [(s["plan"], s["state"]) for s in summary["subscriptions"]] == [
        ("Connect Plus", "active"),
        ("Connect Standard", "ended"),
    ]

    # The month splits between the two plans, each with its own invoice and prices.
    out = client.post(
        "/api/v1/billing/invoices/generate",
        json={"period": f"{billed:%Y-%m}", "customer_id": cid},
        headers=admin_headers,
    ).json()
    dim = (this_month - billed).days
    by_plan = {d["plan"]: d for d in out["drafts"]}
    assert set(by_plan) == {"Connect Standard", "Connect Plus"}
    s, p = by_plan["Connect Standard"], by_plan["Connect Plus"]
    assert (s["covered_from"], s["covered_to"]) == (billed.isoformat(), on.isoformat())
    assert (p["covered_from"], p["covered_to"]) == (on.isoformat(), this_month.isoformat())
    assert (
        s["total"] == _money(Decimal(150) * 10 / dim)
        and s["lines"][0]["description"] == f"Site fee: hq (10 of {dim} days)"
    )
    assert p["total"] == _money(Decimal(300) * (dim - 10) / dim)
    assert all(x["plan"] == "Connect Plus" for x in p["lines"])

    # Issue the Standard part; its plan can't then be ended earlier than the invoice covers.
    iss = client.post(f"/api/v1/billing/invoices/{s['id']}/issue", headers=admin_headers)
    assert iss.status_code == 200, iss.text
    # End Plus: not before it started; then it is ended from that day.
    assert (
        client.post(
            f"{base}/{ch['started']['id']}/end", json={"on": billed.isoformat()}, headers=admin_headers
        ).status_code
        == 422
    )
    end_on = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=5)
    r = client.post(f"{base}/{ch['started']['id']}/end", json={"on": end_on.isoformat()}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["ends_on"] == end_on.isoformat()
    assert (
        client.post(
            f"{base}/{ch['started']['id']}/end", json={"on": end_on.isoformat()}, headers=admin_headers
        ).status_code
        == 409
    )
    assert client.post(f"{base}/not-a-uuid/end", json={}, headers=admin_headers).status_code == 404
    with db.tx() as conn:
        assert plans.products(conn, cid) == ["connect"]
        assert plans.products(conn, cid, end_on) == []
        assert plans.products(conn, cid, dt.date(2025, 12, 31)) == []
        actions = [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'billing.%%' ORDER BY id").fetchall()
        ]
    assert actions[:6] == [
        "billing.plan.create",
        "billing.plan.update",
        "billing.price_list.create",
        "billing.subscription.start",
        "billing.subscription.change",
        "billing.invoice.draft",
    ]
    assert "billing.subscription.end" in actions

    # A plan no longer offered can't be taken up.
    client.patch(f"/api/v1/billing/plans/{plus}", json={"active": False}, headers=admin_headers)
    with db.tx() as conn:
        other = new_customer(conn, "Late Ltd")
    r = client.post(f"/api/v1/customers/{other}/plans", json={"plan_id": plus}, headers=admin_headers)
    assert r.status_code == 409


def test_products_column_kept_in_step(client, admin_headers):
    """When the organisations work adds customers.products, subscriptions drive it."""
    with db.tx() as conn:
        conn.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS products text[]")
        cid = new_customer(conn, "Gated Ltd")
        untouched = new_customer(conn, "Hand Set Ltd")
        conn.execute("UPDATE customers SET products = '{connect,commai}' WHERE id = %s", (untouched,))
        connect, commai = plan_id(conn, "connect"), plan_id(conn, "commai")
    today = dt.datetime.now(dt.UTC).date()
    base = f"/api/v1/customers/{cid}/plans"
    sub = client.post(base, json={"plan_id": commai, "starts_on": "2026-01-01"}, headers=admin_headers).json()

    def column(c):
        with db.tx() as conn:
            return conn.execute("SELECT products FROM customers WHERE id = %s", (c,)).fetchone()["products"]

    assert column(cid) == ["commai"]
    client.post(base, json={"plan_id": connect, "starts_on": "2026-01-01"}, headers=admin_headers)
    assert column(cid) == ["connect", "commai"]
    client.post(f"{base}/{sub['id']}/end", json={"on": today.isoformat()}, headers=admin_headers)
    assert column(cid) == ["connect"]
    # A scheduled start does not grant the product before its day.
    client.post(
        base, json={"plan_id": commai, "starts_on": (today + dt.timedelta(days=3)).isoformat()}, headers=admin_headers
    )
    assert column(cid) == ["connect"]
    # An organisation that never had a subscription keeps what was set by hand.
    with db.tx() as conn:
        assert plans.sync_all(conn) == 1
    assert column(untouched) == ["connect", "commai"]


def test_lab_seed_holds_both_plans(client, admin_headers):
    cid = lab(client, connect_only=False)["customer_id"]
    r = client.get(f"/api/v1/customers/{cid}/plans", headers=admin_headers).json()
    assert r["products"] == ["connect", "commai"]
    assert sorted(s["plan"] for s in r["subscriptions"]) == ["CommAI Standard", "Connect Standard"]


def test_payables_and_margin(client, admin_headers):
    cid = lab(client)["customer_id"]
    billed, _, start = months()
    period = f"{billed:%Y-%m}"
    with db.tx() as conn:
        conn.execute("UPDATE subscriptions SET starts_on = '2025-01-01' WHERE customer_id = %s", (cid,))
        conn.execute("UPDATE sites SET created_at = %s WHERE customer_id = %s", (start - dt.timedelta(days=30), cid))
        partner = conn.execute("SELECT id FROM partners WHERE slug = 'aws'").fetchone()["id"]
        circuit = conn.execute(
            """INSERT INTO circuits (customer_id, name, kind, bandwidth_mbps, price_per_mbps_month, created_by,
                                     created_at, partner_id)
               VALUES (%s, 'to-aws', 'site', 10, 5.0, 'test', %s, %s) RETURNING id""",
            (cid, start - dt.timedelta(days=1), partner),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO circuit_bandwidth (circuit_id, mbps, valid_from, valid_to, changed_by)
               VALUES (%s, 10, %s, %s, 'test')""",
            (circuit, start, start + dt.timedelta(hours=73)),
        )
        carriers = {
            r["name"]: (r["commit"], r["cost"])
            for r in conn.execute(
                """SELECT c.name, sum(l.commit_mbps) AS commit, sum(l.commit_mbps * l.cost_per_mbps) AS cost
                   FROM links l JOIN carriers c ON c.id = l.carrier_id GROUP BY c.name"""
            ).fetchall()
        }

    # Before the partner's cost is agreed, the circuit is unpriced (not zero).
    p = client.get(f"/api/v1/billing/payables?period={period}", headers=admin_headers).json()
    (aws,) = p["partners"]
    assert aws["unpriced"] == 1 and aws["circuits"][0]["total"] is None and aws["circuits"][0]["mbps_hours"] == "730.00"
    r = client.put(f"/api/v1/billing/partners/{partner}/cost", json={"cost_per_mbps_month": "3"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert client.put("/api/v1/billing/partners/999999/cost", json={}, headers=admin_headers).status_code == 404
    assert (
        client.put(
            f"/api/v1/billing/partners/{partner}/cost", json={"cost_per_mbps_month": "-1"}, headers=admin_headers
        ).status_code
        == 422
    )

    # Carriers: commit at the carrier's own price per link, from the settlement code (no usage: no burst).
    # Partner: 10 Mbps for 73 h at 3.00 per Mbps a month / 730 h = 3.00.
    p = client.get(f"/api/v1/billing/payables?period={period}", headers=admin_headers).json()
    got = {c["carrier"]: (c["commit"], c["burst"], c["total"], len(c["links"])) for c in p["carriers"]}
    assert got == {name: (_money(cost), "0.00", _money(cost), 3) for name, (_, cost) in carriers.items()}
    assert p["partners"][0]["total"] == "3.00" and p["partners"][0]["unpriced"] == 0
    carrier_total = sum((cost for _, cost in carriers.values()), Decimal(0))
    assert p["total"] == _money(carrier_total + 3)

    # Margin against the month worked out now (no invoice yet): example prices, 2 sites x 150 and
    # every site link's commit at 6.00, the circuit 10 x 73 h at its own 5.00 / 730 h = 5.00.
    m = client.get(f"/api/v1/billing/margin?period={period}", headers=admin_headers).json()
    (demo,) = m["customers"]
    with db.tx() as conn:
        site_commit = conn.execute(
            "SELECT sum(l.commit_mbps) AS c FROM links l JOIN sites s ON s.id = l.site_id WHERE s.kind = 'site'"
        ).fetchone()["c"]
    revenue = Decimal(300) + Decimal(site_commit) * 6 + 5
    assert demo["plans"] == [{"plan": "Connect Standard", "source": "estimate", "invoice_id": None, "number": None}]
    assert demo["revenue"] == _money(revenue) and demo["example"] is True
    assert demo["carrier_cost"] == _money(carrier_total) and demo["partner_cost"] == "3.00"
    assert demo["margin"] == _money(revenue - carrier_total - 3)
    svc = {s["service"]: s for s in demo["services"]}
    assert svc["fabric"]["revenue"] == "5.00" and svc["fabric"]["cost"] == "3.00" and svc["fabric"]["margin"] == "2.00"
    assert {s["service"] for s in m["services"]} == {"sites", "connectivity", "fabric"}
    # Customer rate cards and carrier settlement stay separate: changing the customer's price list
    # changes revenue only.
    with db.tx() as conn:
        connect = plan_id(conn, "connect")
    client.post(
        "/api/v1/billing/price-lists",
        json={"plan_id": connect, "customer_id": cid, "effective_from": "2025-01-01", "site_monthly": "1"},
        headers=admin_headers,
    )
    m2 = client.get(f"/api/v1/billing/margin?period={period}", headers=admin_headers).json()["customers"][0]
    assert m2["carrier_cost"] == demo["carrier_cost"] and m2["revenue"] != demo["revenue"]


def test_every_endpoint_by_role(client, admin_headers):
    cid = lab(client)["customer_id"]
    billed, _, _ = months()
    period = f"{billed:%Y-%m}"
    with db.tx() as conn:
        conn.execute("UPDATE subscriptions SET starts_on = '2025-01-01' WHERE customer_id = %s", (cid,))
        other = new_customer(conn, "Other Organisation")
        add_user(conn, "it@demo.example", "customer", cid)
        add_user(conn, "it@other.example", "customer", other)
        carrier = conn.execute("SELECT id FROM carriers ORDER BY name LIMIT 1").fetchone()["id"]
        add_user(conn, "noc@carrier.example", "carrier", carrier_id=carrier)
        connect = plan_id(conn, "connect")
        sid = str(conn.execute("SELECT id FROM subscriptions WHERE customer_id = %s", (cid,)).fetchone()["id"])
        partner = conn.execute("SELECT id FROM partners LIMIT 1").fetchone()["id"]
    inv = client.post(
        "/api/v1/billing/invoices/generate", json={"period": period, "customer_id": cid}, headers=admin_headers
    ).json()["drafts"][0]
    issued = client.post(f"/api/v1/billing/invoices/{inv['id']}/issue", headers=admin_headers).json()
    iid = issued["id"]
    own = login(client, "it@demo.example")
    stranger = login(client, "it@other.example")
    carrier_h = login(client, "noc@carrier.example")

    admin_only = [
        ("GET", "/api/v1/billing/plans", None),
        ("POST", "/api/v1/billing/plans", {"product": "commai", "name": "CommAI Lite"}),
        ("PATCH", f"/api/v1/billing/plans/{connect}", {"description": "x"}),
        ("POST", f"/api/v1/customers/{cid}/plans", {"plan_id": connect}),
        ("POST", f"/api/v1/customers/{cid}/plans/{sid}/change", {"plan_id": connect}),
        ("POST", f"/api/v1/customers/{cid}/plans/{sid}/end", {}),
        ("GET", "/api/v1/billing/price-lists", None),
        ("POST", "/api/v1/billing/price-lists", {"plan_id": connect, "effective_from": "2026-01-01"}),
        ("POST", "/api/v1/billing/invoices/generate", {"period": period}),
        ("POST", f"/api/v1/billing/invoices/{iid}/issue", None),
        ("POST", f"/api/v1/billing/invoices/{iid}/void", {"reason": "x"}),
        ("GET", f"/api/v1/billing/invoices/{iid}/export/xero", None),
        ("GET", f"/api/v1/billing/invoices/{iid}/export/quickbooks", None),
        ("GET", f"/api/v1/billing/invoices/{iid}/export/csv", None),
        ("GET", "/api/v1/billing/payments/status", None),
        ("GET", "/api/v1/billing/payables", None),
        ("PUT", f"/api/v1/billing/partners/{partner}/cost", {"cost_per_mbps_month": "1"}),
        ("GET", "/api/v1/billing/margin", None),
    ]
    own_reads = [
        ("GET", f"/api/v1/customers/{cid}/plans", None),
        ("GET", f"/api/v1/billing/charges?customer_id={cid}", None),
        ("GET", f"/api/v1/billing/invoices?customer_id={cid}", None),
        ("GET", f"/api/v1/billing/invoices/{iid}", None),
        ("GET", f"/api/v1/billing/invoices/{iid}/csv", None),
        ("GET", f"/api/v1/billing/invoices/{iid}/print", None),
    ]

    def call(h, method, path, body):
        return client.request(method, path, json=body, headers=h).status_code

    for method, path, body in admin_only:
        for h, who in ((own, "own customer"), (stranger, "other customer"), (carrier_h, "carrier")):
            assert call(h, method, path, body) == 403, (who, method, path)
    for method, path, body in own_reads:
        assert call(own, method, path, body) == 200, (method, path)
        assert call(stranger, method, path, body) in (403, 404), (method, path)
        assert call(carrier_h, method, path, body) == 403, (method, path)
        assert client.request(method, path).status_code == 401
    # Paying: the owner may try (payments are off by default), others may not see the invoice.
    assert call(own, "POST", f"/api/v1/billing/invoices/{iid}/pay", {"provider": "stripe"}) == 409
    assert call(stranger, "POST", f"/api/v1/billing/invoices/{iid}/pay", {"provider": "stripe"}) == 404
    assert call(carrier_h, "POST", f"/api/v1/billing/invoices/{iid}/pay", {"provider": "stripe"}) == 403
    # A stranger's own lists show nothing of Demo's.
    assert client.get("/api/v1/billing/invoices", headers=stranger).json() == []
    assert client.get("/api/v1/billing/charges", headers=stranger).json()["plans"] == []

    # Admins can do all of it.
    for method, path, body in own_reads + admin_only:
        status = call(admin_headers, method, path, body)
        assert status in (200, 201, 409, 422), (method, path, status)
    # Payments being off, the webhook does not exist.
    assert client.post("/api/v1/billing/webhooks/stripe", content=b"{}").status_code == 404
