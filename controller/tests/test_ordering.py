"""Partner directory and plain-English ordering (ADR 0011)."""

import dataclasses
import json

from exaconnect_controller import db, ordering
from exaconnect_controller.ai import ask as ask_mod

from .test_fabric import PSK, _cloud, _sites
from .test_flow import _seed


def _draft(client, h, cid, text, engine="rules"):
    r = client.post(f"/api/v1/customers/{cid}/orders/draft", json={"text": text, "engine": engine}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _ctx(seed):
    with db.tx() as conn:
        return ordering.context(conn, seed["customer_id"])


def test_rules_parser_reads_plain_english(client):
    ctx = _ctx(_seed())
    (a,) = ordering.parse_rules("Connect Kingston to our AWS VPC in us-east-1 at 50 Mbps for 10.100.0.0/16", ctx)
    assert a == {
        "action": "cloud_circuit",
        "provider": "aws",
        "region": "us-east-1",
        "site": "site-a",
        "bandwidth_mbps": 50,
        "cloud_prefixes": ["10.100.0.0/16"],
        "class_name": None,
    }
    acts = ordering.parse_rules(
        "Join Kingston and Port of Spain on VLAN 100 and 200 at 1 Gbps; send Port of Spain's internet straight out",
        ctx,
    )
    assert acts == [
        {
            "action": "site_circuit",
            "a_site": "site-a",
            "b_site": "site-b",
            "a_vlan": 100,
            "b_vlan": 200,
            "bandwidth_mbps": 1000,
        },
        {"action": "internet_mode", "site": "site-b", "mode": "local"},
    ]
    (a,) = ordering.parse_rules("Azure in East US for all our sites, as business traffic", ctx)
    assert (a["provider"], a["region"], a["site"], a["class_name"]) == ("azure", "eastus", None, "business")
    (a,) = ordering.parse_rules("Turn off internet at site-a", ctx)
    assert a == {"action": "internet_mode", "site": "site-a", "mode": "off"}
    (a,) = ordering.parse_rules("Kingston to the Example Payments Network at 10 Mbps", ctx)
    assert (a["action"], a["partner"], a["site"]) == ("partner_connection", "example-payments", "site-a")
    (a,) = ordering.parse_rules('Join Kingston and Port of Spain on VLAN 7 called "lab l2" at 20 Mbps', ctx)
    assert (a["name"], a["a_vlan"], a["b_vlan"], a["bandwidth_mbps"]) == ("lab l2", 7, 7, 20)
    assert ordering.parse_rules("What's the weather like?", ctx) == []


def test_draft_review_and_confirm_a_cloud_circuit(client, admin_headers):
    seed = _seed()
    cid = seed["customer_id"]
    o = _draft(client, admin_headers, cid, "Connect Kingston to AWS us-east-1 at 50 Mbps for 10.100.0.0/16")
    assert o["status"] == "draft" and o["engine"] == "rules" and o["problems"] == []
    assert o["summary"] == ["New circuit from site-a to Amazon Web Services (us-east-1), 50 Mbps, for 10.100.0.0/16."]
    assert o["monthly_estimate"] == 100.0
    assert [(n["field"], n["secret"]) for n in o["needs"]] == [
        ("peer_address", False),
        ("peer_asn", False),
        ("psk", True),
        ("inside_cidr", False),
    ]
    url = f"/api/v1/customers/{cid}/orders/{o['id']}"
    # Missing the key: nothing is created.
    r = client.post(f"{url}/confirm", json={"inputs": [{"peer_address": "100.64.10.2"}]}, headers=admin_headers)
    assert r.status_code == 400 and "Change 1" in r.json()["detail"] and "pre-shared key" in r.json()["detail"]
    assert client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json() == []
    r = client.post(
        f"{url}/confirm", json={"inputs": [{"peer_address": "100.64.10.2", "psk": PSK}]}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "done" and done["confirmed_by"] and done["results"][0]["ok"]
    (c,) = client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json()
    assert (c["name"], c["peer_asn"], c["bandwidth_mbps"]) == ("Amazon Web Services us-east-1", 64512, 50)
    # The key went to the circuit, not into the order or the audit log.
    with db.tx() as conn:
        row = conn.execute("SELECT actions, results FROM orders WHERE id = %s", (o["id"],)).fetchone()
        audit = conn.execute("SELECT detail::text AS d FROM audit_log").fetchall()
    assert PSK not in json.dumps(row) and all(PSK not in a["d"] for a in audit)
    # A confirmed order can't be confirmed or cancelled again.
    assert client.post(f"{url}/confirm", json={"inputs": []}, headers=admin_headers).status_code == 400
    assert client.post(f"{url}/cancel", headers=admin_headers).status_code == 400


def test_bandwidth_and_internet_changes_apply_together_or_not_at_all(client, admin_headers):
    seed = _seed()
    cid, sites = seed["customer_id"], _sites(seed)
    r = client.post(f"/api/v1/customers/{cid}/circuits", json=_cloud(name="AWS prod"), headers=admin_headers)
    assert r.status_code == 201, r.text
    o = _draft(client, admin_headers, cid, "Increase AWS prod to 100 Mbps. Send Kingston's internet straight out")
    assert [a["action"] for a in o["actions"]] == ["bandwidth", "internet_mode"]
    assert o["needs"] == [] and o["problems"] == [] and o["monthly_estimate"] == 100.0
    assert o["summary"][1] == "Send site-a's internet straight out of its own links (now through ExaCarib's PoP)."

    # The third change fails at confirm time (no gateway details): everything rolls back.
    bad = client.post(
        f"/api/v1/customers/{cid}/orders",
        json={
            "actions": [
                {"action": "bandwidth", "circuit": "AWS prod", "bandwidth_mbps": 200},
                {"action": "site_circuit", "a_site": "site-a", "b_site": "site-b", "a_vlan": 300},
                {"action": "cloud_circuit", "provider": "azure", "site": "site-a"},
            ]
        },
        headers=admin_headers,
    ).json()
    assert bad["engine"] == "form" and bad["problems"] == []
    r = client.post(f"/api/v1/customers/{cid}/orders/{bad['id']}/confirm", json={}, headers=admin_headers)
    assert r.status_code == 400 and r.json()["detail"].startswith("Change 3"), r.text
    assert len(client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json()) == 1
    r = client.post(f"/api/v1/customers/{cid}/orders/{bad['id']}/confirm", json={}, headers=admin_headers)
    assert r.status_code == 400 and r.json()["detail"].startswith("Change 3"), r.text
    (c,) = client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json()
    assert c["bandwidth_mbps"] == 50
    assert client.get(f"/api/v1/customers/{cid}/orders/{bad['id']}", headers=admin_headers).json()["status"] == "draft"

    r = client.post(f"/api/v1/customers/{cid}/orders/{o['id']}/confirm", json={}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    (c,) = client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json()
    assert c["bandwidth_mbps"] == 100
    view = client.get(f"/api/v1/customers/{cid}/internet", headers=admin_headers).json()
    assert next(s for s in view["sites"] if s["id"] == sites["site-a"])["mode"] == "local"


def test_problems_block_confirmation(client, admin_headers):
    cid = _seed()["customer_id"]
    o = _draft(client, admin_headers, cid, "Tell me a joke")
    assert o["actions"] == [] and "Try, for example" in o["problems"][0]
    o = client.post(
        f"/api/v1/customers/{cid}/orders",
        json={"actions": [{"action": "site_circuit", "a_site": "Montego Bay", "b_site": "site-b", "a_vlan": 5000}]},
        headers=admin_headers,
    ).json()
    assert "There is no site called Montego Bay." in o["problems"] and "VLAN IDs are 1 to 4094." in o["problems"]
    r = client.post(f"/api/v1/customers/{cid}/orders/{o['id']}/confirm", json={}, headers=admin_headers)
    assert r.status_code == 400 and "Montego Bay" in r.json()["detail"]
    r = client.post(f"/api/v1/customers/{cid}/orders/{o['id']}/cancel", headers=admin_headers)
    assert r.json()["status"] == "cancelled"


def test_drafts_are_checked_against_what_is_already_there(client, admin_headers):
    seed = _seed()
    cid = seed["customer_id"]
    r = client.post(f"/api/v1/customers/{cid}/circuits", json=_cloud(name="AWS prod"), headers=admin_headers)
    assert r.status_code == 201, r.text
    site = client.get(f"/api/v1/customers/{cid}/internet", headers=admin_headers).json()["sites"][0]
    o = client.post(
        f"/api/v1/customers/{cid}/orders",
        json={
            "actions": [
                {"action": "bandwidth", "circuit": "AWS prod", "bandwidth_mbps": 50},
                {"action": "internet_mode", "site": site["name"], "mode": site["mode"]},
                {"action": "cloud_circuit", "provider": "aws", "site": "site-a", "name": "AWS prod"},
            ]
        },
        headers=admin_headers,
    ).json()
    problems = " ".join(o["problems"])
    assert "AWS prod already runs at 50 Mbps." in problems
    assert f"{site['name']} already sends its internet" in problems
    assert "You already have a circuit called AWS prod." in problems


def test_service_partner_waits_for_exacarib(client, admin_headers):
    cid = _seed()["customer_id"]
    partners = client.get("/api/v1/partners", headers=admin_headers).json()
    assert {"aws", "azure", "google-cloud", "oracle-cloud", "example-payments"} <= {p["slug"] for p in partners}
    pay = client.get("/api/v1/partners/example-payments", headers=admin_headers).json()
    assert pay["example"] is True and pay["prefixes"] == ["203.0.113.0/25"]
    assert [p["slug"] for p in client.get("/api/v1/partners?category=payments", headers=admin_headers).json()] == [
        "example-payments"
    ]

    o = client.post(
        f"/api/v1/customers/{cid}/orders",
        json={"actions": [{"action": "partner_connection", "partner": "example-payments", "site": "site-a"}]},
        headers=admin_headers,
    ).json()
    assert o["needs"] == [] and o["monthly_estimate"] == 20.0
    r = client.post(f"/api/v1/customers/{cid}/orders/{o['id']}/confirm", json={}, headers=admin_headers)
    assert r.json()["status"] == "pending_partner"
    (waiting,) = client.get("/api/v1/admin/orders?status=pending_partner", headers=admin_headers).json()
    assert waiting["id"] == o["id"] and waiting["customer"]

    done = {"peer_address": "100.64.20.2", "peer_asn": 64999, "psk": PSK}
    r = client.post(f"/api/v1/admin/orders/{o['id']}/complete", json=done, headers=admin_headers)
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    (c,) = client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json()
    assert c["name"] == "Example Payments Network" and c["cloud_prefixes"] == ["203.0.113.0/25"]

    # A partner that is in use is unlisted, not deleted.
    assert client.delete(f"/api/v1/admin/partners/{pay['id']}", headers=admin_headers).status_code == 204
    assert client.get("/api/v1/partners/example-payments", headers=admin_headers).status_code == 404
    slugs = [p["slug"] for p in client.get("/api/v1/admin/partners", headers=admin_headers).json()]
    assert "example-payments" in slugs


def test_admin_partners(client, admin_headers):
    _seed()
    body = {
        "slug": "carib-erp",
        "name": "Carib ERP",
        "category": "saas",
        "kind": "service",
        "prefixes": ["10.9.0.0/24"],
    }
    r = client.post("/api/v1/admin/partners", json=body, headers=admin_headers)
    assert r.status_code == 201 and r.json()["price_per_mbps_month"] == 2.0, r.text
    pid = r.json()["id"]
    assert client.post("/api/v1/admin/partners", json=body, headers=admin_headers).status_code == 400
    bad = {**body, "slug": "x2", "kind": "cloud"}
    assert client.post("/api/v1/admin/partners", json=bad, headers=admin_headers).status_code == 400
    r = client.patch(f"/api/v1/admin/partners/{pid}", json={"prefixes": ["nope"]}, headers=admin_headers)
    assert r.status_code == 400
    r = client.patch(
        f"/api/v1/admin/partners/{pid}", json={"price_per_mbps_month": 3.5, "listed": False}, headers=admin_headers
    )
    assert r.json()["price_per_mbps_month"] == 3.5 and r.json()["listed"] is False
    assert client.delete(f"/api/v1/admin/partners/{pid}", headers=admin_headers).status_code == 204
    assert pid not in [p["id"] for p in client.get("/api/v1/admin/partners", headers=admin_headers).json()]


def test_ai_drafts_are_checked_and_rate_limited(client, admin_headers, monkeypatch):
    cid = _seed()["customer_id"]
    s = dataclasses.replace(client.app.state.settings, llm_api_key="test-only", llm_questions_per_hour=2)
    monkeypatch.setattr(client.app.state, "settings", s)
    seen = {}

    def fake_chat(system, user, **kw):
        seen["user"] = user
        return (
            'Sure: {"actions": [{"action": "cloud_circuit", "provider": "gcp", "region": "us-east1", '
            '"site": "Port of Spain", "bandwidth_mbps": 30, "cloud_prefixes": ["10.50.0.0/16"]}]}'
        )

    monkeypatch.setattr(ask_mod, "chat", fake_chat)
    o = _draft(client, admin_headers, cid, "Get Port of Spain onto our Google VPC", engine="auto")
    assert o["engine"] == "ai" and o["actions"][0]["site"] == "site-b" and o["actions"][0]["provider"] == "gcp"
    assert '"site-b"' in seen["user"] and "test-only" not in seen["user"]

    def broken(system, user, **kw):
        raise ask_mod.AskError("down")

    monkeypatch.setattr(ask_mod, "chat", broken)
    o = _draft(client, admin_headers, cid, "Connect Kingston to AWS us-east-1", engine="auto")
    assert o["engine"] == "rules" and o["actions"][0]["provider"] == "aws"
    r = client.post(f"/api/v1/customers/{cid}/orders/draft", json={"text": "AWS please"}, headers=admin_headers)
    assert r.status_code == 429
    # The rules engine is never limited.
    assert _draft(client, admin_headers, cid, "AWS please")["engine"] == "rules"


def test_customers_order_for_themselves_only(client, admin_headers):
    seed = _seed()
    r = client.get(f"/api/v1/customers/{seed['customer_id']}/orders")
    assert r.status_code == 401
    other = "00000000-0000-0000-0000-000000000000"
    r = client.post(f"/api/v1/customers/{other}/orders/draft", json={"text": "AWS"}, headers=admin_headers)
    assert r.status_code == 404
