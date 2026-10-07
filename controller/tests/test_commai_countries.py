"""Countries and SMS carriers (ADR 0029): the capability matrix, per-country SMS
rules enforced by the gateway, and routing with failover that never sends twice."""

from __future__ import annotations

import datetime as dt

from exaconnect_controller import db
from exaconnect_controller.commai import channels
from exaconnect_controller.commai.channels import countries, sms_routing

from .commai_helpers import base, business, run_jobs, switch_on

TT_FROM = "+18685550100"


def _sms_account(client, b, number=TT_FROM):
    r = client.post(
        f"{base(b)}/channel-accounts",
        json={"channel": "sms", "provider": "simulated", "address": number, "name": "SMS"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _inbound(client, b, acct, frm, body="Hello", mid=None):
    r = client.post(
        f"{base(b)}/channel-accounts/{acct['id']}/simulate-inbound",
        json={"from": frm, "body": body, "id": mid or ""},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text


def _conv(b, address):
    with db.tx() as conn:
        return conn.execute(
            """SELECT c.* FROM conversations c JOIN contact_identities ci ON ci.id = c.identity_id
               WHERE c.customer_id = %s AND ci.address = %s ORDER BY c.created_at DESC LIMIT 1""",
            (b["id"], address),
        ).fetchone()


def _reply(client, b, conv, body="Thanks for your message"):
    return client.post(
        f"{base(b)}/conversations/{conv['id']}/messages",
        json={"body": body, "take_over": True},
        headers=b["agent"]["h"],
    )


def _outbox(b):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM sim_channel_outbox WHERE customer_id = %s AND channel = 'sms' ORDER BY id", (b["id"],)
        ).fetchall()


def _admin_rules(client, admin_headers, code, rules):
    r = client.put(f"/api/v1/commai/countries/{code}/sms-rules", json=rules, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the matrix -------------------------------------------------------------------------------------------


def test_matrix_covers_the_markets_and_is_marked_unverified(client):
    b = business(client, people=("agent",))
    rows = client.get(f"{base(b)}/countries", headers=b["agent"]["h"]).json()
    codes = {r["code"] for r in rows}
    assert codes == set("TT JM BB BS GY LC VC GD AG KN DM BZ SR CW AW DO PR KY VG TC BM HT US CA GB".split())
    for r in rows:
        assert r["available"] is False  # every country starts off
        assert r["research"]["status"] == "Unverified research"
        for area in ("numbers", "porting", "sms", "whatsapp", "calling", "emergency", "restrictions"):
            assert r[area]["verified"] is False and r[area]["summary"]
    us = next(r for r in rows if r["code"] == "US")
    assert "10DLC" in us["sms"]["registration"]["long_code"] and "alphanumeric" not in us["sms"]["sender_id_types"]
    switch_on("country", "JM", b["id"])
    rows = client.get(f"{base(b)}/countries", headers=b["agent"]["h"]).json()
    assert next(r for r in rows if r["code"] == "JM")["available"] is True
    # The registry carries the same matrix, with the country criteria.
    with db.tx() as conn:
        crit = conn.execute(
            "SELECT criterion FROM commai_capability_criteria WHERE kind = 'country' AND key = 'TT'"
        ).fetchall()
    assert {"matrix-reviewed", "regulatory", "sms-rules", "carrier-route", "tested"} <= {c["criterion"] for c in crit}

    assert countries.country_of("+14165550100") == "CA"
    assert countries.country_of("+12125550100") == "US"
    assert countries.country_of("+18765550100") == "JM"
    assert countries.sender_kind("+18005550100") == "toll_free"
    assert countries.sender_kind("+18685550100") == "long_code"
    assert countries.sender_kind("ExampleBk") == "alphanumeric"
    assert countries.sender_kind("72345") == "short_code"


# ---- gateway rules ----------------------------------------------------------------------------------------


def test_sms_to_a_country_not_switched_on_is_refused_with_a_reason(client):
    b = business(client)
    sms = _sms_account(client, b)
    _inbound(client, b, sms, "+18685550101")
    r = _reply(client, b, _conv(b, "+18685550101"))
    assert r.status_code == 422
    assert "Trinidad and Tobago is not switched on" in r.json()["detail"]
    switch_on("country", "TT", b["id"])
    assert _reply(client, b, _conv(b, "+18685550101")).status_code == 201

    # A country outside the matrix (Sint Maarten, +1 721).
    _inbound(client, b, sms, "+17215550100")
    r = _reply(client, b, _conv(b, "+17215550100"))
    assert r.status_code == 422 and "not in CommAI's country list" in r.json()["detail"]
    # Switched on for b only: another business is still refused.
    other = business(client, "Other Bank", ("agent",))
    osms = _sms_account(client, other, "+18685550200")
    _inbound(client, other, osms, "+18685550101")
    assert _reply(client, other, _conv(other, "+18685550101")).status_code == 422


def test_quiet_hours_apply_to_messages_the_business_starts(client):
    b = business(client, people=("agent",))
    switch_on("country", "US", b["id"])
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO sms_sender_registrations (customer_id, sender, country, kind, status)
               VALUES (%s, '+12125550100', 'US', 'long_code', 'approved')""",
            (b["id"],),
        )
        old = {"customer_id": b["id"], "last_inbound_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC)}
        # 03:00 in New York (07:00 UTC): business-initiated SMS is refused.
        night = dt.datetime(2026, 10, 7, 7, 0, tzinfo=dt.UTC)
        try:
            countries.check_sms(conn, old, "+13055550100", "+12125550100", now=night)
            raise AssertionError("quiet hours not enforced")
        except channels.SendBlocked as e:
            assert "Quiet hours in United States" in str(e) and "recipient's time zone" in str(e)
        # A reply within 24 hours of the person's own message is not affected.
        fresh = {**old, "last_inbound_at": night - dt.timedelta(hours=1)}
        assert countries.check_sms(conn, fresh, "+13055550100", "+12125550100", now=night) == "US"
        # 19:30 UTC is 15:30 in New York and 09:30 in Honolulu: inside the window everywhere.
        afternoon = dt.datetime(2026, 10, 7, 19, 30, tzinfo=dt.UTC)
        assert countries.check_sms(conn, old, "+13055550100", "+12125550100", now=afternoon) == "US"
    # Trinidad and Tobago has no researched quiet-hours rule: no refusal at night.
    switch_on("country", "TT", b["id"])
    with db.tx() as conn:
        tt_night = dt.datetime(2026, 10, 7, 7, 0, tzinfo=dt.UTC)  # 03:00 in Port of Spain
        assert countries.check_sms(conn, old, "+18685550101", TT_FROM, now=tt_night) == "TT"


def test_sender_types_registration_and_rate_limits(client, admin_headers):
    b = business(client)
    u = base(b)
    switch_on("country", "US", b["id"])
    switch_on("country", "TT", b["id"])
    us = _sms_account(client, b, "+12125550100")
    _inbound(client, b, us, "+13055550199")
    conv = _conv(b, "+13055550199")
    r = _reply(client, b, conv)
    assert r.status_code == 422 and "10DLC" in r.json()["detail"]

    reg = client.post(f"{u}/sms-senders", json={"sender": "+12125550100", "country": "US"}, headers=b["agent"]["h"])
    assert reg.status_code == 201 and reg.json()["status"] == "pending" and "10DLC" in reg.json()["requirement"]
    assert _reply(client, b, conv).status_code == 422  # still pending
    r = client.put(
        f"/api/v1/commai/sms-senders/{reg.json()['id']}",
        json={"status": "approved", "reference": "TCR-CAMPAIGN-1"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 403  # only ExaCarib records the outcome
    r = client.put(
        f"/api/v1/commai/sms-senders/{reg.json()['id']}",
        json={"status": "approved", "reference": "TCR-CAMPAIGN-1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert _reply(client, b, conv).status_code == 201
    # No registration is needed for a long code to Trinidad and Tobago.
    r = client.post(f"{u}/sms-senders", json={"sender": TT_FROM, "country": "TT"}, headers=b["agent"]["h"])
    assert r.status_code == 422

    # Sender type: Trinidad and Tobago set to names only refuses an ordinary number.
    tt = _sms_account(client, b)
    _inbound(client, b, tt, "+18685550101")
    tconv = _conv(b, "+18685550101")
    _admin_rules(client, admin_headers, "TT", {"sender_id_types": ["alphanumeric"]})
    r = _reply(client, b, tconv)
    assert r.status_code == 422 and "does not accept SMS from an ordinary phone number" in r.json()["detail"]
    rules = _admin_rules(client, admin_headers, "TT", {"sender_id_types": ["long_code"], "rate_per_minute": 2})
    assert rules["rate_per_minute"] == 2
    assert _reply(client, b, tconv, "One").status_code == 201
    assert _reply(client, b, tconv, "Two").status_code == 201
    r = _reply(client, b, tconv, "Three")
    assert r.status_code == 422 and "a minute" in r.json()["detail"]
    # Queued messages are not counted twice when they go out.
    run_jobs()
    assert {m["body"] for m in _outbox(b)} >= {"One", "Two"}
    bad = client.put("/api/v1/commai/countries/TT/sms-rules", json={"rate_per_minute": "lots"}, headers=admin_headers)
    assert bad.status_code == 422
    bad = client.put(
        "/api/v1/commai/countries/TT/sms-rules", json={"quiet_hours": {"from": "21:00", "until": "08:00"}},
        headers=admin_headers,
    )  # fmt: skip
    assert bad.status_code == 422


# ---- routing and failover --------------------------------------------------------------------------------


def _route(client, admin_headers, primary="sms-sim-a", fallback="sms-sim-b"):
    r = client.put(
        "/api/v1/commai/sms-routes/TT",
        json={"primary_carrier": primary, "fallback_carrier": fallback, "primary_cost": 0.01, "fallback_cost": 0.025},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text


def _fault(client, admin_headers, carrier, mode):
    r = client.put(f"/api/v1/commai/sms-carriers/{carrier}/fault", json={"mode": mode}, headers=admin_headers)
    assert r.status_code == 200, r.text


def _attempts(msg_id):
    with db.tx() as conn:
        return [
            (r["carrier"], r["outcome"])
            for r in conn.execute(
                "SELECT carrier, outcome FROM sms_route_attempts WHERE message_id = %s ORDER BY id", (msg_id,)
            ).fetchall()
        ]


def _msg(msg_id):
    with db.tx() as conn:
        return conn.execute("SELECT * FROM messages WHERE id = %s", (msg_id,)).fetchone()


def test_route_admin_validation(client, admin_headers):
    b = business(client, people=("agent",))
    u = "/api/v1/commai/sms-routes/TT"
    assert client.put(u, json={"primary_carrier": "nope"}, headers=admin_headers).status_code == 422
    r = client.put(u, json={"primary_carrier": "sms-sim-a", "fallback_carrier": "sms-sim-a"}, headers=admin_headers)
    assert r.status_code == 422
    assert (
        client.put(
            "/api/v1/commai/sms-routes/ZZ", json={"primary_carrier": "sms-sim-a"}, headers=admin_headers
        ).status_code
        == 422
    )
    assert client.put(u, json={"primary_carrier": "sms-sim-a"}, headers=b["agent"]["h"]).status_code == 403
    r = client.put("/api/v1/commai/sms-carriers/sms-twilio/fault", json={"mode": "refuse"}, headers=admin_headers)
    assert r.status_code == 422  # faults only on simulators
    carriers = client.get("/api/v1/commai/sms-carriers", headers=admin_headers).json()
    assert {c["key"] for c in carriers} == {"sms-sim-a", "sms-sim-b", "sms-twilio"}
    assert all(c["status"] == "off" for c in carriers)
    assert next(c for c in carriers if c["key"] == "sms-twilio")["missing_env"]


def test_failover_on_refusal_never_sends_twice(client, admin_headers):
    b = business(client)
    switch_on("country", "TT", b["id"])
    sms = _sms_account(client, b)
    _inbound(client, b, sms, "+18685550101")
    conv = _conv(b, "+18685550101")
    _route(client, admin_headers)

    # A route whose carriers are all off refuses up front.
    r = _reply(client, b, conv)
    assert r.status_code == 422 and "No SMS carrier" in r.json()["detail"]
    switch_on("carrier", "sms-sim-a")
    switch_on("carrier", "sms-sim-b")

    # 1. The primary refuses: the fallback sends at once; one message out.
    _fault(client, admin_headers, "sms-sim-a", "refuse")
    m1 = _reply(client, b, conv, "Failover test").json()
    run_jobs()
    run_jobs()
    assert _attempts(m1["id"]) == [("sms-sim-a", "refused"), ("sms-sim-b", "accepted")]
    assert [(o["body"], o["headers"]["carrier"]) for o in _outbox(b)] == [("Failover test", "sms-sim-b")]
    assert _msg(m1["id"])["status"] == "sent"
    with db.tx() as conn:
        cost = conn.execute(
            "SELECT quantity, detail FROM usage_records WHERE customer_id = %s AND meter = 'sms_route_cost'"
            " AND ref = %s",
            (b["id"], m1["id"]),
        ).fetchone()
    assert float(cost["quantity"]) == 0.025 and cost["detail"]["carrier"] == "sms-sim-b"

    # 2. The primary takes it but the answer is lost: no failover; the retry reuses the key.
    _fault(client, admin_headers, "sms-sim-a", "timeout_after_send")
    m2 = _reply(client, b, conv, "Lost answer").json()
    run_jobs()
    assert _msg(m2["id"])["status"] == "queued"
    assert _attempts(m2["id"]) == [("sms-sim-a", "unknown")]
    _fault(client, admin_headers, "sms-sim-a", None)
    run_jobs()
    assert _msg(m2["id"])["status"] == "sent"
    assert _attempts(m2["id"]) == [("sms-sim-a", "unknown"), ("sms-sim-a", "accepted")]
    lost = [o for o in _outbox(b) if o["body"] == "Lost answer"]
    assert len(lost) == 1 and lost[0]["provider_ref"] == _msg(m2["id"])["provider_ref"]

    # 3. The primary times out before taking it: still no failover (it can't be known).
    _fault(client, admin_headers, "sms-sim-a", "timeout")
    m3 = _reply(client, b, conv, "Slow carrier").json()
    run_jobs()
    run_jobs()
    assert _attempts(m3["id"]) == [("sms-sim-a", "unknown"), ("sms-sim-a", "unknown")]
    _fault(client, admin_headers, "sms-sim-a", None)
    run_jobs()
    assert [o["headers"]["carrier"] for o in _outbox(b) if o["body"] == "Slow carrier"] == ["sms-sim-a"]

    # 4. A sent message is never sent again, even if its job runs again.
    from exaconnect_controller.commai import jobs

    with db.tx() as conn:
        jobs.enqueue(conn, "message.send", {"message_id": str(m1["id"])}, customer_id=b["id"])
    run_jobs()
    assert len(_outbox(b)) == 3

    # 5. Both refuse: the job retries later and nothing is sent.
    _fault(client, admin_headers, "sms-sim-a", "refuse")
    _fault(client, admin_headers, "sms-sim-b", "refuse")
    m5 = _reply(client, b, conv, "Nobody home").json()
    run_jobs()
    assert _msg(m5["id"])["status"] == "queued" and "refused" in _msg(m5["id"])["error"]
    assert len(_outbox(b)) == 3
    customer_view = client.get(f"{base(b)}/sms-route-attempts", headers=b["agent"]["h"]).json()
    assert {a["carrier"] for a in customer_view} == {"sms-sim-a", "sms-sim-b"}


def test_segments():
    assert sms_routing.segments("a" * 160) == 1
    assert sms_routing.segments("a" * 161) == 2
    assert sms_routing.segments("é" * 70) == 1
    assert sms_routing.segments("é" * 71) == 2
