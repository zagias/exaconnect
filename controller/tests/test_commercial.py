"""Catalogue, quotes (CPQ), sales orders, contracts and rated usage, end to end
through the API and onto the plan invoice (ADR 0055).

Hand-worked example (the example Connect price list: USD 150 a site a month,
USD 6 per committed Mbps; the example catalogue):

  plan             1 x 0                                        =      0.00 a month
  2 sites          2 x 150 = 300, 10% off                       =    270.00 a month
  link 100 Mbps    100 x 6                                      =    600.00 a month
  managed, 2 sites 2 x 120 = 240, 36 months: 10% term discount  =    216.00 a month
  2 boxes          2 x 450                                      =    900.00 once
  installation     2 x 300 = 600, 30% off (above the 25% limit) =    420.00 once
  engineer hours   about 5 a month, 2 included: 3 x 85          =    255.00 (estimate)

  one-off 1,320.00; monthly 1,086.00; 36 months: 1,320 + 36 x 1,086 = 40,416.00
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from exaconnect_controller import db
from exaconnect_controller.billing import service

from .billing_helpers import add_user, login, new_customer

API = "/api/v1"


def _money(v) -> str:
    return f"{Decimal(v).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _org(client, admin_headers, name):
    """An organisation with an owner, an admin and a member; no plan yet."""
    with db.tx() as conn:
        cid = new_customer(conn, name)
        slug = name.lower().replace(" ", "")
        for who in ("owner", "admin", "member"):
            add_user(conn, f"{who}@{slug}.example", "customer", cid)
        conn.execute(
            """UPDATE org_memberships SET role = 'member' WHERE customer_id = %s
               AND user_id = (SELECT id FROM users WHERE email = %s)""",
            (cid, f"member@{slug}.example"),
        )
    return cid, {who: login(client, f"{who}@{slug}.example") for who in ("owner", "admin", "member")}


def test_catalogue(client, admin_headers):
    cid, people = _org(client, admin_headers, "Catalogue Co")
    items = {i["code"]: i for i in client.get(f"{API}/catalogue/items", headers=people["owner"]).json()["items"]}
    assert {"connect.plan", "connect.site", "connect.link", "connect.box", "commai.seat"} <= set(items)
    assert all(i["example"] for i in items.values())
    assert items["commai.seat"]["price_text"] == (
        "per seat, all at the band reached (1-10: USD 25; 11-50: USD 20; 51+: USD 15)"
    )
    assert items["connect.site"]["plan_priced"] and items["connect.site"]["price"] is None
    assert items["connect.engineer-hours"]["meter"] == "engineer_hours"

    # Only ExaCarib changes the catalogue.
    new = {
        "code": "connect.firewall",
        "name": "Managed firewall",
        "product": "connect",
        "charge_type": "recurring",
        "unit": "site",
        "max_discount_pct": 5,
        "price": {"model": "per_unit", "unit_price": "40", "term_discounts": [{"min_months": 24, "pct": 5}]},
    }
    assert client.post(f"{API}/catalogue/items", json=new, headers=people["owner"]).status_code == 403
    r = client.post(f"{API}/catalogue/items", json=new, headers=admin_headers)
    assert r.status_code == 201, r.text
    assert r.json()["price"]["version"] == 1 and r.json()["fulfilment"] == "immediate"
    assert client.post(f"{API}/catalogue/items", json=new, headers=admin_headers).status_code == 409
    # A new price is a new version; the old one stays.
    r = client.post(
        f"{API}/catalogue/items/connect.firewall/prices",
        json={"model": "per_unit", "unit_price": "45"},
        headers=admin_headers,
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    detail = client.get(f"{API}/catalogue/items/connect.firewall", headers=admin_headers).json()
    assert [p["unit_price"] for p in detail["prices"]] == ["45", "40"]
    assert detail["price"]["unit_price"] == "45"
    # Plan-priced items are changed on the plan price list, not here.
    r = client.post(f"{API}/catalogue/items/connect.site/prices", json={"unit_price": 1}, headers=admin_headers)
    assert r.status_code == 409
    # Bad prices are refused with a reason.
    r = client.post(
        f"{API}/catalogue/items/connect.firewall/prices",
        json={"model": "tiered", "tiers": [{"up_to": 5, "unit_price": 1}]},
        headers=admin_headers,
    )
    assert r.status_code == 422 and "open-ended" in r.json()["detail"]
    # Retire an item: it leaves the list people see.
    r = client.patch(f"{API}/catalogue/items/connect.firewall", json={"active": False}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["active"] is False
    codes = {i["code"] for i in client.get(f"{API}/catalogue/items", headers=people["owner"]).json()["items"]}
    assert "connect.firewall" not in codes

    r = client.post(
        f"{API}/rating/preview",
        json={
            "price": {"model": "tiered", "tiers": [{"up_to": 10, "unit_price": 25}, {"up_to": None, "unit_price": 20}]},
            "quantity": 12,
            "term_months": 1,
            "discount_pct": 10,
        },
        headers=people["member"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "290.0000" and r.json()["charge"] == "261.0000"


QUOTE = {
    "title": "Two branches",
    "term_months": 36,
    "lines": [
        {"item": "connect.plan", "quantity": 1},
        {
            "item": "connect.site",
            "quantity": 2,
            "discount_pct": 10,
            "config": {"locations": ["Kingston HQ", "Montego Bay"]},
        },
        {
            "item": "connect.link",
            "quantity": 100,
            "config": {"location": "Kingston HQ", "carrier": "Flow", "underlay_type": "fibre"},
        },
        {"item": "connect.managed", "quantity": 2},
        {"item": "connect.box", "quantity": 2},
        {"item": "connect.install", "quantity": 2, "discount_pct": 30},
        {"item": "connect.engineer-hours", "quantity": 5},
    ],
}


def test_quote_to_order_to_invoice(client, admin_headers):
    cid, people = _org(client, admin_headers, "Harbour Bank")

    # Live pricing for the quote builder: nothing saved.
    r = client.post(f"{API}/quotes/preview", json={**QUOTE, "customer_id": cid}, headers=admin_headers)
    assert r.status_code == 200, r.text
    p = r.json()
    assert [x["amount"] for x in p["lines"]] == [
        "0.0000",
        "270.0000",
        "600.0000",
        "216.0000",
        "900.0000",
        "420.0000",
        "255.0000",
    ]
    assert p["lines"][3]["term_discount_pct"] == "10"
    assert (p["one_off_total"], p["monthly_total"], p["usage_estimate"], p["contract_total"]) == (
        "1320.00",
        "1086.00",
        "255.00",
        "40416.00",
    )
    assert p["needs_approval"] is True and p["example"] is True

    r = client.post(f"{API}/quotes", json={**QUOTE, "customer_id": cid}, headers=admin_headers)
    assert r.status_code == 201, r.text
    q = r.json()
    qid = q["id"]
    assert q["number"].startswith(f"Q-{dt.date.today().year}-") and q["state"] == "draft"
    assert q["contract_total"] == "40416.00"
    # The organisation doesn't see ExaCarib's drafts.
    assert client.get(f"{API}/quotes/{qid}", headers=people["owner"]).status_code == 404
    assert client.get(f"{API}/quotes", headers=people["owner"]).json()["quotes"] == []

    # 30% off installation is above its 25% limit: it needs approval before it goes out.
    r = client.post(f"{API}/quotes/{qid}/send", headers=admin_headers)
    assert r.status_code == 409 and "approval" in r.json()["detail"]
    assert client.post(f"{API}/quotes/{qid}/submit", headers=admin_headers).json()["state"] == "pending_approval"
    r = client.post(
        f"{API}/quotes/{qid}/approve", json={"approve": True, "note": "Two-site launch customer"}, headers=admin_headers
    )
    assert r.status_code == 200 and r.json()["state"] == "approved" and r.json()["approved_by"]
    r = client.post(f"{API}/quotes/{qid}/send", headers=admin_headers)
    assert r.status_code == 200 and r.json()["state"] == "sent"

    # The organisation reads it and its printable document.
    listed = client.get(f"{API}/quotes", headers=people["member"]).json()["quotes"]
    assert [x["number"] for x in listed] == [q["number"]]
    doc = client.get(f"{API}/quotes/{qid}/document", headers=people["member"])
    assert doc.status_code == 200 and "Example data" in doc.text and "40,416.00" in doc.text
    # A member can't accept; an owner can, with their name.
    assert (
        client.post(f"{API}/quotes/{qid}/accept", json={"name": "Mia Member"}, headers=people["member"]).status_code
        == 403
    )
    r = client.post(f"{API}/quotes/{qid}/accept", json={"name": "Olive Owner"}, headers=people["owner"])
    assert r.status_code == 200, r.text
    order = r.json()["order"]
    assert r.json()["quote"]["state"] == "accepted" and r.json()["quote"]["accepted_name"] == "Olive Owner"
    assert order["number"].startswith("SO-") and order["status"] == "in_progress"
    status = {i["code"]: i["status"] for i in order["items"]}
    assert status == {
        "connect.plan": "done",
        "connect.site": "done",
        "connect.link": "done",
        "connect.managed": "done",
        "connect.engineer-hours": "done",
        "connect.box": "pending",
        "connect.install": "pending",
    }, [(i["code"], i["error"]) for i in order["items"]]
    # Plan first, then sites, then links, then the rest.
    assert [i["code"] for i in order["items"]][:3] == ["connect.plan", "connect.site", "connect.link"]
    # Accepting twice does nothing more.
    assert (
        client.post(f"{API}/quotes/{qid}/accept", json={"name": "Olive Owner"}, headers=people["owner"]).status_code
        == 409
    )

    with db.tx() as conn:
        sub = conn.execute("SELECT * FROM subscriptions WHERE customer_id = %s", (cid,)).fetchone()
        assert sub["starts_on"] == dt.datetime.now(dt.UTC).date()
        sites = conn.execute(
            """SELECT s.name, l.name AS location FROM sites s JOIN org_locations l ON l.id = s.org_location_id
               WHERE s.customer_id = %s ORDER BY s.name""",
            (cid,),
        ).fetchall()
        assert [(s["name"], s["location"]) for s in sites] == [
            ("kingston-hq", "Kingston HQ"),
            ("montego-bay", "Montego Bay"),
        ]
        link = conn.execute(
            """SELECT l.path, l.commit_mbps, c.name AS carrier FROM links l JOIN carriers c ON c.id = l.carrier_id
               JOIN sites s ON s.id = l.site_id WHERE s.name = 'kingston-hq' AND s.customer_id = %s""",
            (cid,),
        ).fetchone()
        assert (link["path"], float(link["commit_mbps"]), link["carrier"]) == ("carrier-a", 100.0, "Flow")

    # The contract: the managed service, a credit per site for its discount, the usage item.
    c = client.get(f"{API}/customers/{cid}/contract", headers=people["member"]).json()
    by = sorted((x["description"], x["monthly_amount"]) for x in c["items"])
    assert by == [
        ("Discount 10%: site kingston-hq", "-15.0000"),
        ("Discount 10%: site montego-bay", "-15.0000"),
        ("Engineer time", "0.0000"),
        ("Managed network service x 2", "216.0000"),
    ]
    assert c["monthly_total"] == "186.0000"

    # Staff finish the manual items: their one-off charges start.
    items = {i["code"]: i for i in order["items"]}
    assert (
        client.post(
            f"{API}/sales-orders/items/{items['connect.box']['id']}/done", json={}, headers=people["owner"]
        ).status_code
        == 403
    )
    client.post(
        f"{API}/sales-orders/items/{items['connect.box']['id']}/done",
        json={"note": "Shipped, DHL 123"},
        headers=admin_headers,
    )
    r = client.post(f"{API}/sales-orders/items/{items['connect.install']['id']}/done", json={}, headers=admin_headers)
    assert r.json()["status"] == "completed" and r.json()["completed_at"]
    assert (
        client.post(
            f"{API}/sales-orders/items/{items['connect.box']['id']}/done", json={}, headers=admin_headers
        ).status_code
        == 409
    )

    # Usage arrives with references; a repeat is skipped.
    now = dt.datetime.now(dt.UTC)
    events = [
        {"meter": "engineer_hours", "quantity": 2, "at": now.isoformat(), "ref": "job-1"},
        {"meter": "engineer_hours", "quantity": 1.5, "at": now.isoformat(), "ref": "job-2"},
        {"meter": "engineer_hours", "quantity": 1.5, "at": now.isoformat(), "ref": "job-3"},
    ]
    r = client.post(f"{API}/usage-events", json={"customer_id": cid, "events": events}, headers=admin_headers)
    assert r.json() == {"received": 3, "added": 3, "duplicates": 0}
    r = client.post(f"{API}/usage-events", json={"customer_id": cid, "events": events[:1]}, headers=admin_headers)
    assert r.json() == {"received": 1, "added": 0, "duplicates": 1}
    r = client.post(
        f"{API}/usage-events",
        json={"customer_id": cid, "events": [{"meter": "satellite_gb", "quantity": 1, "ref": "x"}]},
        headers=admin_headers,
    )
    assert r.status_code == 409
    assert (
        client.post(
            f"{API}/usage-events", json={"customer_id": cid, "events": events}, headers=people["owner"]
        ).status_code
        == 403
    )
    assert len(client.get(f"{API}/usage-events", headers=people["member"]).json()["events"]) == 3
    c = client.get(f"{API}/customers/{cid}/contract", headers=people["member"]).json()
    assert c["usage"][0]["quantity"] == "5.000000" and c["usage"][0]["amount_so_far"] == "255.0000"

    # This month's invoice carries all of it, beside the plan's own site and link lines.
    this_month = now.date().replace(day=1)
    with db.tx() as conn:
        calc = service.compute(conn, cid, this_month)
    (block,) = calc["plans"]
    end = (this_month + dt.timedelta(days=32)).replace(day=1)
    days, dim = (end - now.date()).days, (end - this_month).days
    ours = {
        (x["kind"], x["description"].split(" (")[0]): x["amount"]
        for x in block["lines"]
        if x["kind"] in ("recurring", "credit", "one_off", "rated")
    }
    assert ours == {
        ("recurring", "Managed network service x 2"): Decimal(_money(Decimal(216) * days / dim)),
        ("credit", "Discount 10%: site kingston-hq"): Decimal(_money(Decimal(-15) * days / dim)),
        ("credit", "Discount 10%: site montego-bay"): Decimal(_money(Decimal(-15) * days / dim)),
        ("one_off", "ExaCarib box x 2"): Decimal("900.00"),
        ("one_off", "Site installation x 2"): Decimal("420.00"),
        ("rated", "Engineer time: 5 hours"): Decimal("255.00"),
    }
    sites = [x for x in block["lines"] if x["kind"] == "site"]
    assert len(sites) == 2  # the plan price list still bills the sites themselves
    assert block["total"] == sum((Decimal(x["amount"]) for x in block["lines"]), Decimal(0))

    # And the draft invoice keeps them.
    r = client.post(
        f"{API}/billing/invoices/generate",
        json={"period": f"{this_month:%Y-%m}", "customer_id": cid},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    (draft,) = r.json()["drafts"]
    kinds = {x["kind"] for x in draft["lines"]}
    assert {"recurring", "credit", "one_off", "rated", "site", "commit"} <= kinds

    # Ending the managed service stops it from that day.
    managed = next(x for x in c["items"] if x["code"] == "connect.managed")
    r = client.post(f"{API}/customers/{cid}/contract/{managed['id']}/end", json={}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["status"] == "ended"


def test_self_service_quote(client, admin_headers):
    cid, people = _org(client, admin_headers, "Island Clinic")
    lines = [{"item": "commai.plan"}, {"item": "commai.seat", "quantity": 12}, {"item": "commai.onboarding"}]
    # Only ExaCarib gives discounts; members can't build quotes.
    r = client.post(f"{API}/quotes", json={"lines": [{**lines[1], "discount_pct": 5}]}, headers=people["admin"])
    assert r.status_code == 403
    assert client.post(f"{API}/quotes", json={"lines": lines}, headers=people["member"]).status_code == 403
    r = client.post(f"{API}/quotes", json={"lines": lines, "term_months": 12}, headers=people["admin"])
    assert r.status_code == 201, r.text
    q = r.json()
    # 12 seats on the volume price: all at USD 20, then 5% for 12 months = 228; plan USD 49; onboarding 500 flat.
    assert [x["amount"] for x in q["lines"]] == ["49.0000", "228.0000", "500.0000"]
    assert (q["monthly_total"], q["one_off_total"], q["contract_total"]) == ("277.00", "500.00", "3824.00")
    # The organisation sees its own draft, and can send and accept it.
    assert [x["id"] for x in client.get(f"{API}/quotes", headers=people["member"]).json()["quotes"]] == [q["id"]]
    r = client.put(f"{API}/quotes/{q['id']}", json={"lines": [*lines[:2]]}, headers=people["admin"])
    assert r.status_code == 200 and r.json()["one_off_total"] == "0.00"
    assert client.post(f"{API}/quotes/{q['id']}/send", headers=people["admin"]).json()["state"] == "sent"
    r = client.post(f"{API}/quotes/{q['id']}/accept", json={"name": "Ada Admin"}, headers=people["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["order"]["status"] == "completed"
    with db.tx() as conn:
        products = conn.execute("SELECT products FROM customers WHERE id = %s", (cid,)).fetchone()["products"]
    assert products == ["commai"]
    # Another organisation sees none of it.
    _, other = _org(client, admin_headers, "Other Org")
    assert client.get(f"{API}/quotes/{q['id']}", headers=other["owner"]).status_code == 404
    assert client.get(f"{API}/sales-orders/{r.json()['order']['id']}", headers=other["owner"]).status_code == 404
    assert client.get(f"{API}/sales-orders", headers=other["owner"]).json()["orders"] == []
    assert client.get(f"{API}/customers/{cid}/contract", headers=other["owner"]).status_code == 403


def test_expiry_decline_copy_and_failed_items(client, admin_headers):
    cid, people = _org(client, admin_headers, "Reef Hotels")
    q = client.post(
        f"{API}/quotes",
        json={
            "customer_id": cid,
            "lines": [{"item": "connect.plan"}, {"item": "connect.site"}, {"item": "connect.link", "quantity": 50}],
        },
        headers=admin_headers,
    ).json()
    client.post(f"{API}/quotes/{q['id']}/send", headers=admin_headers)
    with db.tx() as conn:
        conn.execute("UPDATE quotes SET valid_until = current_date - 1 WHERE id = %s", (q["id"],))
    got = client.get(f"{API}/quotes/{q['id']}", headers=people["owner"]).json()
    assert got["state"] == "expired" and got["state_label"] == "Expired"
    r = client.post(f"{API}/quotes/{q['id']}/accept", json={"name": "Olive Owner"}, headers=people["owner"])
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    copied = client.post(f"{API}/quotes/{q['id']}/copy", headers=admin_headers).json()
    assert copied["state"] == "draft" and copied["number"] != q["number"]
    client.post(f"{API}/quotes/{copied['id']}/send", headers=admin_headers)
    # A second quote is declined with a reason.
    other = client.post(f"{API}/quotes/{q['id']}/copy", headers=admin_headers).json()
    client.post(f"{API}/quotes/{other['id']}/send", headers=admin_headers)
    r = client.post(f"{API}/quotes/{other['id']}/decline", json={"reason": "Too dear"}, headers=people["owner"])
    assert r.json()["state"] == "declined" and r.json()["decline_reason"] == "Too dear"
    assert client.post(f"{API}/quotes/{other['id']}/cancel", headers=admin_headers).status_code == 409

    # The link names no carrier: it fails with the reason; the rest is done.
    r = client.post(f"{API}/quotes/{copied['id']}/accept", json={"name": "Olive Owner"}, headers=people["owner"])
    order = r.json()["order"]
    link = next(i for i in order["items"] if i["code"] == "connect.link")
    assert link["status"] == "failed" and "carrier" in link["error"]
    assert order["status"] == "in_progress"
    # Staff add the missing details and try again.
    r = client.post(
        f"{API}/sales-orders/items/{link['id']}/retry",
        json={"config": {"carrier": "Digicel", "underlay_type": "broadband"}},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    with db.tx() as conn:
        row = conn.execute(
            "SELECT l.path, l.underlay_type FROM links l JOIN sites s ON s.id = l.site_id WHERE s.customer_id = %s",
            (cid,),
        ).fetchone()
    assert (row["path"], row["underlay_type"]) == ("carrier-a", "broadband")
    assert client.get(f"{API}/sales-orders", headers=admin_headers, params={"customer_id": cid}).json()["orders"]


def test_currency_rules_and_access(client, admin_headers):
    cid, people = _org(client, admin_headers, "Currency Co")
    # The plan price list is in USD: a JMD quote can't price sites from it.
    r = client.post(
        f"{API}/quotes",
        json={"customer_id": cid, "currency": "JMD", "lines": [{"item": "connect.site"}]},
        headers=admin_headers,
    )
    assert r.status_code == 409 and "USD" in r.json()["detail"]
    r = client.post(
        f"{API}/quotes",
        json={"customer_id": cid, "currency": "JMD", "lines": [{"item": "connect.box"}]},
        headers=admin_headers,
    )
    assert r.status_code == 409 and "no price in JMD" in r.json()["detail"]
    r = client.post(
        f"{API}/quotes",
        json={"customer_id": cid, "lines": [{"item": "connect.plan", "quantity": 2}]},
        headers=admin_headers,
    )
    assert r.status_code == 422
    assert client.post(f"{API}/quotes", json={"lines": []}, headers=admin_headers).status_code == 422
    # A quote with no lines can't be sent.
    q = client.post(f"{API}/quotes", json={"customer_id": cid}, headers=admin_headers).json()
    assert client.post(f"{API}/quotes/{q['id']}/send", headers=admin_headers).status_code == 422
    assert client.get(f"{API}/quotes/not-a-uuid", headers=admin_headers).status_code == 404

    # Carrier accounts get nothing here.
    with db.tx() as conn:
        carrier = conn.execute("INSERT INTO carriers (name) VALUES ('Test Carrier') RETURNING id").fetchone()["id"]
        add_user(conn, "noc@carrier.example", "carrier", None, carrier)
    h = login(client, "noc@carrier.example")
    for path in ("/catalogue/items", "/quotes", "/sales-orders"):
        assert client.get(f"{API}{path}", headers=h).status_code == 403
