"""CommAI voice stage 5 (ADR 0027): numbers by country behind the go-live
registry, porting with documents, rejection, rescheduling, cut-over and roll
back, emergency addresses and island rules, carrier routing with failover and
trunk health, revenue share fraud protection, LiveKit wiring and tenant isolation."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import golive
from exaconnect_controller.commai.voice import provider as providers

from .commai_helpers import base, business, run_jobs
from .test_commai_voice import overview, save, setup_voice

PDF = b"%PDF-1.4\n% example letter of authorisation\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _sim(monkeypatch):
    providers.FAULTS.clear()
    monkeypatch.setitem(providers.SIM_DELAYS, "port", 0)
    monkeypatch.setitem(providers.SIM_DELAYS, "emergency", 0)
    yield
    providers.FAULTS.clear()


def switch_on(kind: str, key: str) -> None:
    with db.tx() as conn:
        golive.sync(conn)
        for c in golive.get(conn, kind, key)["criteria"]:
            golive.check(conn, kind, key, c["criterion"], True, "test run", "admin@example.org")
        golive.set_status(conn, kind, key, "on", "admin@example.org")


def _price(p):
    return {"monthly_delta": p["monthly_delta"], "one_time": p["one_time"]}


def _events(cid, type_):
    with db.tx() as conn:
        return conn.execute(
            "SELECT data FROM commai_events WHERE customer_id = %s AND type = %s ORDER BY seq", (cid, type_)
        ).fetchall()


def _number(e164):
    with db.tx() as conn:
        return conn.execute("SELECT * FROM voice_numbers WHERE e164 = %s AND status <> 'removed'", (e164,)).fetchone()


def sim_call(client, b, to, ext="201", who="boss", **extra):
    r = client.post(
        f"{base(b)}/voice/calls/simulate",
        json={"extension": ext, "to": to, "seconds": 60, **extra},
        headers=b[who]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---- numbers by country ---------------------------------------------------------------------


def test_numbers_by_country_need_go_live_and_a_test_call_each(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    r = client.get(f"{u}/voice/numbers/search", params={"country": "TT"}, headers=b["boss"]["h"])
    assert r.status_code == 409 and "not switched on" in r.json()["detail"]
    tt = next(c for c in client.get(f"{u}/voice/countries", headers=b["ana"]["h"]).json() if c["code"] == "TT")
    assert tt["numbers"] is False and tt["areas"] == ["868"]

    switch_on("feature", "voice-numbers-TT")
    found = client.get(f"{u}/voice/numbers/search", params={"country": "TT"}, headers=b["boss"]["h"]).json()
    assert found["example"] and all(n["e164"].startswith("+186855501") for n in found["numbers"])
    pick = found["numbers"][3]["e164"]
    # Staff can't search; a wrong area is refused.
    assert client.get(f"{u}/voice/numbers/search", params={"country": "TT"}, headers=b["ana"]["h"]).status_code == 403
    r = client.get(f"{u}/voice/numbers/search", params={"country": "TT", "area": "876"}, headers=b["boss"]["h"])
    assert r.status_code == 422

    items = {
        "site": "Port of Spain",
        "users": [{"name": "Joy Ng", "extension": "230", "number": True}],
        "numbers": {"new": 1, "country": "TT", "choose": [pick]},
    }
    o = client.post(f"{u}/voice/orders", json={"items": items}, headers=b["boss"]["h"]).json()
    providers.FAULTS["test_call"] = 1  # the first test call fails: not active
    client.post(
        f"{u}/voice/orders/{o['id']}/approve", json={"accepted_price": _price(o["price"])}, headers=b["boss"]["h"]
    )
    run_jobs()
    o = client.get(f"{u}/voice/orders/{o['id']}", headers=b["boss"]["h"]).json()
    assert o["status"] == "failed" and o["failed_step"] == "confirm"
    client.post(f"{u}/voice/orders/{o['id']}/retry", headers=b["boss"]["h"])
    run_jobs()
    o = client.get(f"{u}/voice/orders/{o['id']}", headers=b["boss"]["h"]).json()
    assert o["status"] == "active"
    assert {t["e164"] for t in o["test_calls"] if t["ok"]} == set(o["steps"][1]["result"]["numbers"])
    assert pick in o["steps"][1]["result"]["numbers"]
    n = _number(pick)
    assert n["country"] == "TT" and n["status"] == "active" and n["outbound_enabled"]  # TT: no address rule

    # Jamaica: numbers switched on, but the countries module has the country itself off.
    golive.declare("country", "JM", "Jamaica")
    switch_on("feature", "voice-numbers-JM")
    r = client.get(f"{u}/voice/numbers/search", params={"country": "JM"}, headers=b["boss"]["h"])
    assert r.status_code == 409 and "CommAI is not switched on in Jamaica" in r.json()["detail"]
    switch_on("country", "JM")
    found = client.get(f"{u}/voice/numbers/search", params={"country": "JM", "area": "658"}, headers=b["boss"]["h"])
    assert found.json()["numbers"][0]["e164"].startswith("+165855501")
    # Ordering in a country that is off is refused before anything is made.
    r = client.post(
        f"{u}/voice/orders", json={"items": {"numbers": {"new": 1, "country": "BB"}}}, headers=b["boss"]["h"]
    )
    assert r.status_code == 409


def test_real_provider_refuses_until_configured(monkeypatch):
    for n in providers.HttpSipProvider.SETTINGS:
        monkeypatch.delenv(n, raising=False)
    p = providers.HttpSipProvider()
    with pytest.raises(providers.NotConfigured) as e:
        p.search_numbers(None, "TT")
    assert "EXA_SIP_PROVIDER_URL" in str(e.value)
    monkeypatch.setenv("EXA_SIP_PROVIDER", "sip")
    assert providers.get().live


# ---- emergency addresses ------------------------------------------------------------------------


def _us_site(client, b, postcode=""):
    save(
        client,
        b,
        [
            {
                "op": "add_site",
                "name": "Miami",
                "address_line1": "1 Brickell Avenue",
                "city": "Miami",
                "country": "US",
                "timezone": "America/New_York",
                "postcode": postcode,
            }
        ],
    )


def test_outbound_needs_a_validated_address_where_the_rules_say(client):
    b = setup_voice(client)
    u = base(b)
    switch_on("feature", "voice-numbers-US")
    _us_site(client, b)  # no ZIP code: the provider will reject it
    save(client, b, [{"op": "add_user", "name": "Kim Fox", "extension": "240", "site": "Miami"}])
    out = save(
        client, b, [{"op": "add_number", "country": "US", "area": "305", "target_type": "user", "target": "240"}]
    )
    e164 = out["results"][0]["e164"]
    assert e164.startswith("+130555501")
    n = _number(e164)
    assert n["status"] == "active" and not n["outbound_enabled"] and "emergency address" in n["outbound_reason"]

    # Outbound calls from it are stopped; emergency calls never are.
    blocked = sim_call(client, b, "+1 305 555 0142", ext="240", from_number=e164)
    assert not blocked["allowed"] and "emergency address" in blocked["reason"]
    assert sim_call(client, b, "911", ext="240", from_number=e164)["allowed"]

    run_jobs()  # the provider answers: rejected (no ZIP)
    em = client.get(f"{u}/voice/emergency", headers=b["boss"]["h"]).json()
    miami = next(s for s in em["sites"] if s["name"] == "Miami")
    assert miami["emergency_status"] == "rejected" and "postcode" in miami["emergency_reason"]
    assert miami["rules"]["requires_validated_address"] and miami["rules"]["location_delivery"]
    notes = client.get(f"{u}/voice/notifications", headers=b["boss"]["h"]).json()
    assert any("not accepted: Miami" in x["title"] for x in notes)

    save(client, b, [{"op": "update_site", "site": "Miami", "postcode": "33131"}])
    assert not _number(e164)["outbound_enabled"]  # pending again
    run_jobs()
    assert _number(e164)["outbound_enabled"]
    assert sim_call(client, b, "+1 305 555 0142", ext="240", from_number=e164)["allowed"]
    assert _events(b["id"], "voice.outbound_enabled")[-1]["data"]["e164"] == e164

    # Trinidad has no such rule: its sites don't block outbound calls.
    tt = next(s for s in em["sites"] if s["name"] == "Port of Spain")
    assert not tt["rules"]["requires_validated_address"] and "999" in tt["rules"]["numbers"]
    assert "does not receive your address" in tt["rules"]["notice"]


def test_a_move_updates_the_address_and_rechecks_outbound(client):
    b = setup_voice(client)
    u = base(b)
    switch_on("feature", "voice-numbers-US")
    _us_site(client, b)
    out = save(client, b, [{"op": "add_number", "target_type": "user", "target": "201"}])  # Ana, in Trinidad
    e164 = out["results"][0]["e164"]
    assert _number(e164)["outbound_enabled"]

    # Ana reads and accepts the emergency notice for her island.
    notice = client.get(f"{u}/voice/me/emergency-notice", headers=b["ana"]["h"]).json()
    assert notice["country"] == "TT" and notice["acknowledged_at"] is None
    r = client.post(f"{u}/voice/me/emergency-notice", json={"key": "TT:wrong"}, headers=b["ana"]["h"])
    assert r.status_code == 409
    r = client.post(f"{u}/voice/me/emergency-notice", json={"key": notice["key"]}, headers=b["ana"]["h"])
    assert r.json()["acknowledged_at"]

    save(client, b, [{"op": "move_user", "user": "201", "site": "Miami"}])
    n = _number(e164)
    assert n["emergency_address"]["city"] == "Miami" and not n["outbound_enabled"]
    # Her new island has a different notice: she is told to read it.
    mine = client.get(f"{u}/voice/notifications", headers=b["ana"]["h"]).json()
    assert any("emergency calling notice" in x["title"] for x in mine)
    notice = client.get(f"{u}/voice/me/emergency-notice", headers=b["ana"]["h"]).json()
    assert notice["country"] == "US" and notice["acknowledged_at"] is None and notice["location_delivery"]

    # A home worker gets her own address, validated on its own.
    with db.tx() as conn:
        ana = conn.execute("SELECT id FROM voice_users WHERE extension = '201'").fetchone()["id"]
    r = client.put(
        f"{u}/voice/emergency/users/{ana}",
        json={"address": {"address_line1": "9 Ocean Drive", "city": "Miami", "country": "US", "postcode": "33139"}},
        headers=b["boss"]["h"],
    )
    assert r.status_code == 200 and r.json()["status"] == "pending"
    run_jobs()
    assert _number(e164)["outbound_enabled"]
    assert (
        client.put(f"{u}/voice/emergency/users/{ana}", json={"address": None}, headers=b["ana"]["h"]).status_code == 403
    )


# ---- porting --------------------------------------------------------------------------------


def _port(client, b, e164="+1 868 555 0150", account="ACC-0000"):
    r = client.post(
        f"{base(b)}/voice/port-orders",
        json={
            "e164": e164,
            "losing_carrier": "Old Telco",
            "account_name": "Voice Bank",
            "account_number": account,
            "site": "Port of Spain",
        },
        headers=b["boss"]["h"],
    )
    return r


def _upload(client, b, pid, doc_type, data, name, ctype):
    return client.post(
        f"{base(b)}/voice/port-orders/{pid}/documents",
        data={"doc_type": doc_type},
        files={"file": (name, data, ctype)},
        headers=b["boss"]["h"],
    )


def test_port_lifecycle_rejection_reschedule_rollback_and_cut_over(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    assert _port(client, b).status_code == 409  # porting in Trinidad is off
    switch_on("feature", "voice-porting-TT")
    p = _port(client, b).json()
    pid = p["id"]
    assert p["status"] == "draft" and p["missing_documents"] == ["Letter of authorisation", "Recent bill"]
    assert _port(client, b).status_code == 409  # the same number twice

    price = _price(p["price"])
    r = client.post(f"{u}/voice/port-orders/{pid}/submit", json={"accepted_price": price}, headers=b["boss"]["h"])
    assert r.status_code == 422 and "letter of authorisation" in r.json()["detail"]

    # Only PDF, PNG and JPEG, checked by content; declared type must agree.
    assert _upload(client, b, pid, "loa", b"hello", "loa.pdf", "application/pdf").status_code == 415
    assert _upload(client, b, pid, "loa", PDF, "loa.pdf", "image/png").status_code == 415
    assert (
        _upload(client, b, pid, "loa", b"%PDF" + b"x" * (10 * 1024 * 1024), "big.pdf", "application/pdf").status_code
        == 413
    )
    assert _upload(client, b, pid, "passport", PDF, "x.pdf", "application/pdf").status_code == 422
    r = _upload(client, b, pid, "loa", PDF, "../../etc/LOA signed.pdf", "application/pdf")
    assert r.status_code == 201 and r.json()["filename"] == "LOA signed.pdf"
    loa = r.json()
    assert _upload(client, b, pid, "bill", PNG, "bill", "image/png").json()["filename"] == "bill.png"

    d = client.get(f"{u}/voice/port-orders/{pid}/documents/{loa['id']}", headers=b["boss"]["h"])
    assert d.content == PDF and d.headers["content-disposition"].startswith("attachment")
    assert d.headers["x-content-type-options"] == "nosniff"
    assert hashlib.sha256(d.content).hexdigest() == loa["sha256"]

    r = client.post(f"{u}/voice/port-orders/{pid}/submit", json={"accepted_price": price}, headers=b["lead"]["h"])
    assert r.status_code == 403  # no spend permission
    r = client.post(
        f"{u}/voice/port-orders/{pid}/submit",
        json={"accepted_price": {**price, "one_time": "0.00"}},
        headers=b["boss"]["h"],
    )
    assert r.status_code == 409
    r = client.post(f"{u}/voice/port-orders/{pid}/submit", json={"accepted_price": price}, headers=b["boss"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "submitted"

    run_jobs()  # the losing carrier rejects it: the account number is wrong
    p = client.get(f"{u}/voice/port-orders/{pid}", headers=b["boss"]["h"]).json()
    assert p["status"] == "rejected" and p["rejection_code"] == "ACCOUNT_MISMATCH"
    assert "account number" in p["rejection_reason"]

    client.patch(f"{u}/voice/port-orders/{pid}", json={"account_number": "ACC-1234"}, headers=b["boss"]["h"])
    r = client.post(f"{u}/voice/port-orders/{pid}/submit", json={"accepted_price": price}, headers=b["boss"]["h"])
    assert r.json()["status"] == "submitted"
    run_jobs()
    p = client.get(f"{u}/voice/port-orders/{pid}", headers=b["boss"]["h"]).json()
    assert p["status"] == "scheduled" and p["foc_date"]
    assert dt.date.fromisoformat(p["foc_date"]) >= providers.add_business_days(dt.date.today(), 5)
    # Documents can't be removed once the losing carrier has them.
    r = client.delete(f"{u}/voice/port-orders/{pid}/documents/{loa['id']}", headers=b["boss"]["h"])
    assert r.status_code == 409

    # Reschedule: too soon is refused, later is fine.
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    r = client.post(f"{u}/voice/port-orders/{pid}/reschedule", json={"date": tomorrow}, headers=b["boss"]["h"])
    assert r.status_code == 422 and "working days" in r.json()["detail"]
    later = providers.add_business_days(dt.date.today(), 8).isoformat()
    r = client.post(f"{u}/voice/port-orders/{pid}/reschedule", json={"date": later}, headers=b["boss"]["h"])
    assert r.json()["foc_date"] == later and r.json()["status"] == "scheduled"

    run_jobs()  # not the date yet: nothing happens
    assert client.get(f"{u}/voice/port-orders/{pid}", headers=b["boss"]["h"]).json()["status"] == "scheduled"

    # The date comes; the test calls fail; the number goes back to the losing carrier.
    with db.tx() as conn:
        conn.execute("UPDATE voice_port_orders SET cutover_at = now() - interval '1 minute' WHERE id = %s", (pid,))
    providers.FAULTS["test_call"] = 2
    run_jobs()
    p = client.get(f"{u}/voice/port-orders/{pid}", headers=b["boss"]["h"]).json()
    assert p["status"] == "rolled_back" and _number(p["e164"])["status"] == "porting"
    assert "handed back to Old Telco" in p["timeline"][-1]["detail"]
    assert [t["ok"] for t in p["test_calls"]] == [False, False]

    # A new date, and this time it works: live, billed, outbound allowed.
    r = client.post(f"{u}/voice/port-orders/{pid}/reschedule", json={"date": later}, headers=b["boss"]["h"])
    assert r.status_code == 200
    with db.tx() as conn:
        conn.execute("UPDATE voice_port_orders SET cutover_at = now() - interval '1 minute' WHERE id = %s", (pid,))
    run_jobs()
    p = client.get(f"{u}/voice/port-orders/{pid}", headers=b["boss"]["h"]).json()
    assert p["status"] == "completed"
    n = _number(p["e164"])
    assert n["status"] == "active" and n["billing_from"] is not None and n["outbound_enabled"]
    statuses = [t["status"] for t in p["timeline"]]
    for s in ("draft", "submitted", "rejected", "scheduled", "cutting_over", "rolled_back", "completed"):
        assert s in statuses
    with db.tx() as conn:
        fee = conn.execute("SELECT amount FROM voice_charges WHERE ref = %s", (f"port:{pid}",)).fetchone()
    assert fee is not None
    assert {e["data"]["status"] for e in _events(b["id"], "voice.port_rolled_back")} == {"rolled_back"}
    notes = client.get(f"{u}/voice/notifications", headers=b["boss"]["h"]).json()
    assert any("completed" in x["title"] for x in notes) and any("rolled back" in x["title"] for x in notes)
    # A finished port can't be cancelled.
    assert client.post(f"{u}/voice/port-orders/{pid}/cancel", headers=b["boss"]["h"]).status_code == 409


def test_port_in_an_order_asks_for_documents_then_can_be_cancelled(client):
    b = setup_voice(client)
    u = base(b)
    items = {"numbers": {"ported": [{"e164": "+1 868 555 0160", "losing_carrier": "Old Telco"}]}}
    o = client.post(f"{u}/voice/orders", json={"items": items}, headers=b["boss"]["h"]).json()
    client.post(
        f"{u}/voice/orders/{o['id']}/approve", json={"accepted_price": _price(o["price"])}, headers=b["boss"]["h"]
    )
    run_jobs()
    port = client.get(f"{u}/voice/port-orders", headers=b["boss"]["h"]).json()[0]
    assert port["status"] == "documents_needed"
    p = client.get(f"{u}/voice/port-orders/{port['id']}", headers=b["boss"]["h"]).json()
    assert "letter of authorisation" in p["timeline"][-1]["detail"]
    _upload(client, b, port["id"], "loa", PDF, "loa.pdf", "application/pdf")
    _upload(client, b, port["id"], "bill", PDF, "bill.pdf", "application/pdf")
    assert client.get(f"{u}/voice/port-orders/{port['id']}", headers=b["boss"]["h"]).json()["status"] == "submitted"
    r = client.post(f"{u}/voice/port-orders/{port['id']}/cancel", headers=b["boss"]["h"])
    assert r.json()["status"] == "cancelled" and _number("+18685550160") is None


# ---- carriers ----------------------------------------------------------------------------------


def test_routing_least_cost_quality_failover_and_trunk_health(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    A = "/api/v1/commai/voice-admin"
    assert client.get(f"{A}/carriers", headers=b["boss"]["h"]).status_code == 403
    cs = client.get(f"{A}/carriers", headers=admin_headers).json()
    assert {c["key"] for c in cs} == {"sim-carrier-1", "sim-carrier-2"}
    assert all(c["golive"]["status"] == "off" for c in cs)

    # With no carrier switched on, calls use the single provider as before.
    first = sim_call(client, b, "+1 876 555 0100")
    assert first["allowed"] and first["route"] == [] and first["carrier"] == ""

    switch_on("carrier", "sim-carrier-1")
    switch_on("carrier", "sim-carrier-2")
    plan = client.get(f"{A}/route", params={"customer_id": b["id"], "to": "+18765550100"}, headers=admin_headers).json()
    assert plan["mode"] == "lcr" and plan["route"] == ["sim-carrier-2", "sim-carrier-1"]  # 0.021 before 0.026
    plan = client.get(f"{A}/route", params={"customer_id": b["id"], "to": "+18685550100"}, headers=admin_headers).json()
    assert plan["route"] == ["sim-carrier-1", "sim-carrier-2"]

    # Failover: the cheaper carrier refuses, the next one takes the call.
    providers.FAULTS["call:sim-carrier-2"] = 1
    call = sim_call(client, b, "+1 876 555 0100")
    assert call["allowed"] and call["route"] == [
        {"carrier": "sim-carrier-2", "ok": False},
        {"carrier": "sim-carrier-1", "ok": True},
    ]
    with db.tx() as conn:
        cdr = conn.execute(
            "SELECT carrier, carrier_cost FROM voice_cdrs WHERE call_id = %s", (call["call_id"],)
        ).fetchone()
    assert cdr["carrier"] == "sim-carrier-1" and str(cdr["carrier_cost"]) == "0.0260"
    tried = client.get(f"{u}/voice/route-attempts", params={"call_id": call["call_id"]}, headers=b["boss"]["h"]).json()
    assert [t["carrier"] for t in tried] == ["Simulated carrier B", "Simulated carrier A"]

    # Quality routing for Jamaica: the healthier trunk first.
    client.put(f"{A}/routing", json={"prefix": "1876", "mode": "quality"}, headers=admin_headers)
    for _ in range(3):
        providers.FAULTS["options:sim-carrier-1"] = 1
        client.post(f"{A}/carriers/sim-carrier-1/options", headers=admin_headers)
    c1 = next(c for c in client.get(f"{A}/carriers", headers=admin_headers).json() if c["key"] == "sim-carrier-1")
    assert c1["health"] == "down" and c1["consecutive_failures"] == 3
    plan = client.get(f"{A}/route", params={"customer_id": b["id"], "to": "+18765550100"}, headers=admin_headers).json()
    assert plan["mode"] == "quality" and plan["route"] == ["sim-carrier-2"]
    assert "down" in next(c for c in plan["carriers"] if c["key"] == "sim-carrier-1")["reason"]
    # Both failing: the call fails cleanly with the reason.
    providers.FAULTS["call:sim-carrier-2"] = 1
    failed = sim_call(client, b, "+1 876 555 0101")
    assert not failed["allowed"] and failed["status"] == "failed" and "No carrier could take" in failed["reason"]
    # One OPTIONS answer brings the trunk back.
    client.post(f"{A}/carriers/sim-carrier-1/options", headers=admin_headers)
    plan = client.get(f"{A}/route", params={"customer_id": b["id"], "to": "+18765550100"}, headers=admin_headers).json()
    assert set(plan["route"]) == {"sim-carrier-1", "sim-carrier-2"}

    # The carrier's records are imported and reconciled against its own rate sheet.
    imp = client.post(f"{A}/carriers/sim-carrier-1/import", headers=admin_headers).json()
    assert imp["added"] == 1 and imp["rejected"] == []
    rec = client.get(f"{A}/carriers/sim-carrier-1/reconcile", headers=admin_headers).json()
    assert rec["rows"] == 1 and rec["difference"] == "0.00" and rec["issues"] == []
    # New rates are a new version; the old call is now off the sheet.
    r = client.put(
        f"{A}/carriers/sim-carrier-1/rates",
        json={"lines": [{"prefix": "", "per_minute": "0.5"}]},
        headers=admin_headers,
    )
    assert r.json()["rate_version"] == 2
    rec = client.get(f"{A}/carriers/sim-carrier-1/reconcile", headers=admin_headers).json()
    assert rec["issues"] and "rate sheet gives" in rec["issues"][0]["issue"]

    # A new carrier starts off; Kamailio files carry the trunks and allow-list.
    r = client.post(
        f"{A}/carriers",
        json={
            "key": "carib-sip",
            "name": "Carib SIP",
            "outbound": {"host": "sbc.carib.example", "port": 5061, "transport": "tls"},
            "inbound": {"allow_ips": ["198.51.100.0/28"]},
        },
        headers=admin_headers,
    )
    assert r.status_code == 201 and r.json()["golive"]["status"] == "off"
    bad = client.post(
        f"{A}/carriers", json={"key": "x1", "name": "X", "inbound": {"allow_ips": ["nope"]}}, headers=admin_headers
    )
    assert bad.status_code == 422
    k = client.get(f"{A}/kamailio", headers=admin_headers).json()
    assert "sip:sbc.carib.example:5061;transport=tls" in k["files"]["dispatcher.list"]
    assert "1 198.51.100.0 28 0 carib-sip" in k["files"]["address.list"]


# ---- fraud ---------------------------------------------------------------------------------


def test_revenue_share_fraud_rules_suspend_and_restore(client):
    b = setup_voice(client)
    u = base(b)
    st = client.get(f"{u}/voice/fraud", headers=b["boss"]["h"]).json()
    assert st["settings"]["origin"] == "TT" and any(h["prefix"] == "53" for h in st["high_risk"])
    # Caribbean numbers are not on Trinidad's list (they are on the US one).
    assert not any(h["prefix"] == "1876" for h in st["high_risk"])

    blocked = sim_call(client, b, "+53 7 555 0100")
    assert not blocked["allowed"] and "high-risk" in blocked["reason"]
    r = client.put(f"{u}/voice/fraud", json={"allowed_high_risk": ["53"]}, headers=b["lead"]["h"])
    assert r.status_code == 403  # allowing one needs spend permission
    client.put(f"{u}/voice/fraud", json={"allowed_high_risk": ["53"]}, headers=b["boss"]["h"])
    assert sim_call(client, b, "+53 7 555 0100")["allowed"]

    # A sudden spike suspends international calling.
    # Attempts count, blocked ones included: two +53 calls so far, so the 6th international try trips it.
    client.put(f"{u}/voice/fraud", json={"spike_min_calls": 6, "spike_factor": "3"}, headers=b["boss"]["h"])
    assert sim_call(client, b, "+44 20 7946 0000")["allowed"]
    assert sim_call(client, b, "+44 20 7946 0001")["allowed"]
    assert sim_call(client, b, "+44 20 7946 0002")["allowed"]
    stopped = sim_call(client, b, "+44 20 7946 0003")
    assert not stopped["allowed"] and "suspended" in stopped["reason"]
    assert not sim_call(client, b, "+1 876 555 0100")["allowed"]  # Jamaica is international from Trinidad
    assert sim_call(client, b, "+1 868 555 0100")["allowed"]  # calls at home carry on
    assert sim_call(client, b, "999")["allowed"]
    assert _events(b["id"], "voice.fraud_suspended")
    notes = client.get(f"{u}/voice/notifications", headers=b["boss"]["h"]).json()
    assert any(x["title"] == "International calling suspended" for x in notes)

    assert client.post(f"{u}/voice/fraud/restore", json={}, headers=b["lead"]["h"]).status_code == 403
    r = client.post(f"{u}/voice/fraud/restore", json={"note": "Checked: a sales campaign"}, headers=b["boss"]["h"])
    assert r.status_code == 200 and not r.json()["intl_suspended"] and r.json()["restored_by"]
    assert sim_call(client, b, "+44 20 7946 0004")["allowed"]
    assert client.post(f"{u}/voice/fraud/restore", json={}, headers=b["boss"]["h"]).status_code == 409

    # Daily cap on international calls.
    client.put(f"{u}/voice/fraud", json={"intl_daily_calls": 5, "spike_min_calls": 1000}, headers=b["boss"]["h"])
    capped = sim_call(client, b, "+44 20 7946 0005")
    assert not capped["allowed"] and "cap of 5" in capped["reason"]

    # After hours: block international calls outside the business's hours.
    save(client, b, [{"op": "save_hours", "name": "Never open", "schedule": {}}])
    hid = next(h["id"] for h in overview(client, b)["hours"] if h["name"] == "Never open")
    client.put(
        f"{u}/voice/fraud",
        json={"after_hours_international": "block", "hours_id": hid, "intl_daily_calls": 1000},
        headers=b["boss"]["h"],
    )
    late = sim_call(client, b, "+44 20 7946 0006")
    assert not late["allowed"] and "outside business hours" in late["reason"]
    assert sim_call(client, b, "+1 868 555 0101")["allowed"]


# ---- tenant isolation and the LiveKit wiring -------------------------------------------------------


def test_tenant_isolation(client, admin_headers):
    a = setup_voice(client)
    other = business(client, "Other Shop", ("owner",))
    switch_on("feature", "voice-porting-TT")
    pid = _port(client, a).json()["id"]
    doc = _upload(client, a, pid, "loa", PDF, "loa.pdf", "application/pdf").json()
    ua, uo, h = base(a), base(other), other["owner"]["h"]
    assert client.get(f"{ua}/voice/port-orders", headers=h).status_code == 403
    assert client.get(f"{ua}/voice/port-orders/{pid}/documents/{doc['id']}", headers=h).status_code == 403
    assert client.get(f"{ua}/voice/emergency", headers=h).status_code == 403
    assert client.get(f"{ua}/voice/fraud", headers=h).status_code == 403
    # Through its own business, the other tenant still can't reach A's port or document.
    assert client.get(f"{uo}/voice/port-orders/{pid}", headers=h).status_code == 404
    assert client.get(f"{uo}/voice/port-orders/{pid}/documents/{doc['id']}", headers=h).status_code == 404
    assert client.post(f"{uo}/voice/port-orders/{pid}/cancel", headers=h).status_code == 404
    assert client.get(f"{uo}/voice/port-orders", headers=h).json() == []
    assert client.get(f"{uo}/voice/notifications", headers=h).json() == []
    assert client.get("/api/v1/commai/voice-admin/high-risk", headers=h).status_code == 403
    assert client.get(f"{ua}/voice/port-orders/{pid}", headers=admin_headers).status_code == 200


def test_livekit_agent_answers_a_phone_call(client, monkeypatch):
    b = setup_voice(client)
    out = save(client, b, [{"op": "add_number", "target_type": "user", "target": "201"}])
    e164 = out["results"][0]["e164"]
    url = "/api/v1/commai/voice/livekit/calls"
    assert client.post(url, json={"dialled": e164}).status_code == 503  # not set up
    secret = "agent-" + "x" * 20
    for k, v in (
        ("EXA_LIVEKIT_URL", "wss://livekit.example"),
        ("EXA_LIVEKIT_API_KEY", "devkey"),
        ("EXA_LIVEKIT_API_SECRET", "s" * 32),
        ("EXA_LIVEKIT_AGENT_SECRET", secret),
    ):
        monkeypatch.setenv(k, v)
    assert client.post(url, json={"dialled": e164}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    h = {"Authorization": f"Bearer {secret}"}
    assert client.post(url, json={"dialled": "+1 868 555 0000"}, headers=h).status_code == 404
    r = client.post(url, json={"dialled": e164, "caller": "+1 868 555 0111"}, headers=h)
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["customer_id"] == b["id"] and call["room"] == f"call-{call['conversation_id']}" and call["greeting"]
    end = client.post(f"{url}/{b['id']}/{call['conversation_id']}/end", headers=h)
    assert end.status_code == 200 and end.json()["ended_at"]

    from exaconnect_controller.commai.voice import livekit

    tok = livekit.token("agent", call["room"], agent=True)
    head, body, sig = tok.split(".")
    want = hmac.new(("s" * 32).encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(sig + "==") == want
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    assert claims["video"]["room"] == call["room"] and claims["iss"] == "devkey"
