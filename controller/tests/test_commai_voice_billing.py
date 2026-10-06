"""CommAI voice, provisioning and billing (ADR 0021): orders with safe
retries, rating with rate card versions, bundles, fraud limits, invoices
that freeze, credit notes and the supplier side."""

import datetime as dt
from decimal import Decimal

import psycopg
import pytest

from exaconnect_controller import db
from exaconnect_controller.commai.voice import billing
from exaconnect_controller.commai.voice import provider as providers

from .commai_helpers import base, run_jobs
from .test_commai_voice import overview, setup_voice


@pytest.fixture(autouse=True)
def _no_faults():
    providers.FAULTS.clear()
    yield
    providers.FAULTS.clear()


def _events(cid, type_):
    with db.tx() as conn:
        return conn.execute(
            "SELECT data FROM commai_events WHERE customer_id = %s AND type = %s ORDER BY seq", (cid, type_)
        ).fetchall()


def _call(client, b, call_id, to, seconds, *, ended=None, ai=0, ext="201"):
    with db.tx() as conn:
        vu = conn.execute(
            "SELECT id FROM voice_users WHERE customer_id = %s AND extension = %s", (b["id"], ext)
        ).fetchone()
    ended = ended or dt.datetime.now(dt.UTC)
    r = client.post(
        f"{base(b)}/voice/calls",
        json={
            "call_id": call_id,
            "to_number": to,
            "from_number": ext,
            "voice_user_id": str(vu["id"]),
            "started_at": (ended - dt.timedelta(seconds=seconds)).isoformat(),
            "ended_at": ended.isoformat(),
            "seconds": seconds,
            "ai_seconds": ai,
            "provider_ref": f"prov-{call_id}",
        },
        headers=b["boss"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


CARD_V2 = {
    "label": "Example v2",
    "monthly_user": "12.00",
    "monthly_number": "3.00",
    "ai_minute": "0.10",
    "one_time": {"desk_phone": "25.00", "port_number": "10.00"},
    "destinations": [
        {"prefix": "1868", "name": "Trinidad and Tobago", "per_minute": "0.0200"},
        {"prefix": "", "name": "Rest of world", "per_minute": "0.3000"},
    ],
}


def test_rating_keeps_the_rate_card_version(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    cards = client.get(f"{u}/voice/rate-cards", headers=b["boss"]["h"]).json()
    assert len(cards) == 1 and cards[0]["example"] and cards[0]["label"].startswith("Example")
    before_v2 = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    c1 = _call(client, b, "c1", "+1 868 555 0199", 90, ai=60)
    charges = {c["kind"]: c for c in c1["charges"]}
    assert charges["call"]["amount"] == "0.0225" and charges["call"]["rate_card_version"] == 1
    assert charges["ai_minutes"]["amount"] == "0.1200"
    # Customers can't set prices; ExaCarib can, as a new version.
    assert client.post(f"{u}/voice/rate-cards", json=CARD_V2, headers=b["boss"]["h"]).status_code == 403
    r = client.post(f"{u}/voice/rate-cards", json=CARD_V2, headers=admin_headers)
    assert r.status_code == 201 and r.json()["version"] == 2, r.text
    c2 = _call(client, b, "c2", "+1 868 555 0199", 90)
    assert c2["charges"][0]["amount"] == "0.0300" and c2["charges"][0]["rate_card_version"] == 2
    # A call that ended before v2 took effect, reported late, is still rated with v1.
    late = _call(client, b, "c0", "+1 868 555 0199", 60, ended=before_v2)
    assert late["charges"][0]["rate_card_version"] == 1 and late["charges"][0]["amount"] == "0.0150"
    # International falls to the default line.
    assert _call(client, b, "c3", "+44 20 7946 0000", 60)["charges"][0]["amount"] == "0.3000"
    # The same call record again changes nothing.
    again = _call(client, b, "c1", "+1 868 555 0199", 90, ai=60)
    assert {c["id"] for c in again["charges"]} == {c["id"] for c in c1["charges"]}
    with db.tx() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM voice_charges WHERE customer_id = %s AND kind = 'call'", (b["id"],)
        ).fetchone()["n"]
        used = conn.execute(
            "SELECT sum(quantity) AS q FROM usage_records WHERE customer_id = %s AND meter = 'voice_minute'", (b["id"],)
        ).fetchone()["q"]
    assert n == 4 and Decimal(used) == Decimal("5")  # 1.5 + 1.5 + 1 + 1 minutes
    # Version 1 is untouched.
    v1 = client.get(f"{u}/voice/rate-cards", headers=b["boss"]["h"]).json()[-1]
    assert v1["version"] == 1 and v1["ai_minute"] == "0.1200"
    with pytest.raises(TypeError):
        billing.money(0.1)  # floats are refused as money


def test_bundle_pooled_with_alerts(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    r = client.post(
        f"{u}/voice/bundles",
        json={"name": "Local 10", "minutes": "10", "prefixes": ["1868"], "alert_pct": 80},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    first = _call(client, b, "b1", "+1 868 555 0101", 7 * 60)["charges"][0]
    assert first["bundle_minutes"] == "7.0000" and first["amount"] == "0.0000"
    assert _events(b["id"], "voice.bundle_alert") == []
    _call(client, b, "b2", "+1 868 555 0102", 2 * 60, ext="202")  # pooled: another person's call counts too
    alerts = _events(b["id"], "voice.bundle_alert")
    assert len(alerts) == 1 and alerts[0]["data"]["used_minutes"] == "9.00"
    third = _call(client, b, "b3", "+1 868 555 0103", 3 * 60)["charges"][0]
    assert third["bundle_minutes"] == "1.0000" and third["amount"] == "0.0300"  # 2 minutes at 0.015
    assert len(_events(b["id"], "voice.bundle_used_up")) == 1
    # Not covered by the bundle: charged in full.
    assert _call(client, b, "b4", "+1 876 555 0100", 60)["charges"][0]["amount"] == "0.0400"
    spend = client.get(f"{u}/voice/spend", headers=b["boss"]["h"]).json()
    assert spend["bundles"][0]["used"] == "10.00" and spend["example_prices"]
    assert {s["name"] for s in spend["by_user"]} >= {"Ana Lee", "Ben Ali"}
    assert spend["by_site"][0]["name"] == "Port of Spain"


def test_fraud_limits_stop_calls(client):
    b = setup_voice(client)
    u = base(b)

    def sim(to, seconds=60):
        r = client.post(
            f"{u}/voice/calls/simulate", json={"extension": "201", "to": to, "seconds": seconds}, headers=b["boss"]["h"]
        )
        assert r.status_code == 201, r.text
        return r.json()

    blocked = sim("+1 900 555 0100")
    assert not blocked["allowed"] and blocked["status"] == "blocked" and blocked["charges"] == []
    assert "1900" in blocked["reason"] and len(_events(b["id"], "voice.call_blocked")) == 1
    r = client.put(
        f"{u}/voice/fraud-limits",
        json={
            "daily_cap": "0.05",
            "blocked_prefixes": ["1900", "882"],
            "international": False,
            "calls_per_hour_alert": 3,
        },
        headers=b["lead"]["h"],
    )
    assert r.status_code == 200 and r.json()["daily_cap"] == "0.05", r.text
    assert client.put(f"{u}/voice/fraud-limits", json={"daily_cap": "100"}, headers=b["ana"]["h"]).status_code == 403
    assert not sim("+44 20 7946 0000")["allowed"]  # international switched off
    assert sim("+1 868 555 0100", 120)["allowed"]  # 0.03
    assert sim("+1 868 555 0100", 120)["allowed"]  # 0.06: passes the cap
    stopped = sim("+1 868 555 0100", 60)
    assert not stopped["allowed"] and "daily cap" in stopped["reason"]
    assert sim("911")["allowed"]  # emergency calls are never stopped
    assert len(_events(b["id"], "voice.fraud_alert")) == 1  # unusual volume, alerted once an hour


def test_invoice_freezes_and_corrections_are_credit_notes(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    _call(client, b, "i1", "+1 868 555 0199", 600)  # 0.15
    period = dt.date.today().strftime("%Y-%m")
    assert client.post(f"{u}/voice/invoices/draft", json={"period": period}, headers=b["boss"]["h"]).status_code == 403
    r = client.post(f"{u}/voice/invoices/draft", json={"period": period}, headers=admin_headers)
    assert r.status_code == 201, r.text
    draft = r.json()
    kinds = sorted(line["kind"] for line in draft["lines"])
    assert kinds == ["call", "monthly_user", "monthly_user"]
    call_line = next(line for line in draft["lines"] if line["kind"] == "call")
    assert call_line["call_id"] == "i1" and call_line["rate_card_version"] == 1  # traces back to the call
    # Charges start at activation: users added today pay for the days left in the month.
    today = dt.date.today()
    first_next = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    days_in = (first_next - today.replace(day=1)).days
    days = (first_next - today).days
    monthly = (Decimal("12") * days / days_in).quantize(Decimal("0.0001"))
    assert all(Decimal(x["amount"]) == monthly for x in draft["lines"] if x["kind"] == "monthly_user")
    assert Decimal(draft["total"]) == (2 * monthly + Decimal("0.15")).quantize(Decimal("0.01"))

    issued = client.post(f"{u}/voice/invoices/{draft['id']}/issue", headers=admin_headers).json()
    assert issued["status"] == "issued" and issued["number"] == f"VI-{today.year}-0001"
    assert client.post(f"{u}/voice/invoices/{draft['id']}/issue", headers=admin_headers).status_code == 409
    # Frozen: the database refuses any change to an issued invoice or its lines.
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as conn:
            conn.execute("UPDATE voice_invoices SET total = 0 WHERE id = %s", (draft["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as conn:
            conn.execute("DELETE FROM voice_invoice_lines WHERE invoice_id = %s", (draft["id"],))
    # A new draft for the month holds only what is not invoiced yet.
    _call(client, b, "i2", "+1 868 555 0199", 60)
    d2 = client.post(f"{u}/voice/invoices/draft", json={"period": period}, headers=admin_headers).json()
    assert [line["call_id"] for line in d2["lines"]] == ["i2"]

    # Corrections are credit notes pointing at the lines they correct.
    r = client.post(
        f"{u}/voice/invoices/{draft['id']}/credit",
        json={"lines": [{"line_id": call_line["id"], "amount": "0.10"}], "reason": "Dropped call"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["kind"] == "credit_note" and note["number"] == f"VC-{today.year}-0001"
    assert note["total"] == "-0.10" and note["lines"][0]["credits_line"] == call_line["id"]
    r = client.post(
        f"{u}/voice/invoices/{draft['id']}/credit",
        json={"lines": [{"line_id": call_line["id"], "amount": "0.06"}], "reason": "Again"},
        headers=admin_headers,
    )
    assert r.status_code == 422 and "0.05" in r.json()["detail"]
    same = client.get(f"{u}/voice/invoices/{draft['id']}", headers=b["boss"]["h"]).json()
    assert same["total"] == issued["total"] and same["status"] == "issued"
    listed = client.get(f"{u}/voice/invoices", headers=b["boss"]["h"]).json()
    assert {i["kind"] for i in listed} == {"invoice", "credit_note"}


def test_removal_stops_charges(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    with db.tx() as conn:  # Ben has been billed since the 1st; he leaves today
        conn.execute(
            "UPDATE voice_users SET billing_from = date_trunc('month', now()) WHERE customer_id = %s", (b["id"],)
        )
    r = client.post(
        f"{u}/voice/changes",
        json={
            "ops": [{"op": "remove_user", "user": "202"}],
            "accepted_price": {"monthly_delta": "-12.00", "one_time": "0.00"},
        },
        headers=b["boss"]["h"],
    )
    assert r.status_code == 201, r.text
    today = dt.date.today()
    if today.day == 1:
        pytest.skip("on the 1st Ben has no billable days this month")
    period = today.strftime("%Y-%m")
    draft = client.post(f"{u}/voice/invoices/draft", json={"period": period}, headers=admin_headers).json()
    ben = next(x for x in draft["lines"] if "Ben Ali" in x["description"])
    assert f"{today.day - 1} of" in ben["description"]


def _order_items():
    return {
        "site": "Port of Spain",
        "users": [
            {"name": "Gail Ross", "extension": "220", "number": True, "desk_phone": {"mac": "00:15:65:00:00:01"}},
            {"name": "Hal King", "extension": "221", "softphone": True, "team": "Cards"},
        ],
        "numbers": {
            "new": 1,
            "ported": [{"e164": "+1 868 555 0177", "losing_carrier": "Old Telco", "switch_date": "2026-11-30"}],
        },
        "features": {"main_ring_group": True, "main_extension": "100", "ai_after_hours": True},
    }


def test_order_fails_at_a_step_and_retries_without_duplicates(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    r = client.post(f"{u}/voice/orders", json={"items": _order_items()}, headers=b["lead"]["h"])
    assert r.status_code == 201, r.text
    order = r.json()
    price = order["price"]
    assert price["monthly_delta"] == "33.00"  # 2 users x 12 + 3 numbers x 3
    assert price["one_time"] == "35.00"  # desk phone 25 + port 10
    oid = order["id"]
    accepted = {"monthly_delta": "33.00", "one_time": "35.00"}
    r = client.post(f"{u}/voice/orders/{oid}/approve", json={"accepted_price": accepted}, headers=b["lead"]["h"])
    assert r.status_code == 403  # no spend permission
    r = client.post(
        f"{u}/voice/orders/{oid}/approve",
        json={"accepted_price": {**accepted, "one_time": "0.00"}},
        headers=b["boss"]["h"],
    )
    assert r.status_code == 409
    providers.FAULTS["order_number"] = 1  # the provider fails once, mid-step
    r = client.post(f"{u}/voice/orders/{oid}/approve", json={"accepted_price": accepted}, headers=b["boss"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    run_jobs()
    o = client.get(f"{u}/voice/orders/{oid}", headers=b["boss"]["h"]).json()
    assert o["status"] == "failed" and o["failed_step"] == "numbers" and "Simulated provider failure" in o["error"]
    steps = {s["step"]: s["status"] for s in o["steps"]}
    assert steps == {
        "tenant": "done",
        "numbers": "failed",
        "devices": "pending",
        "confirm": "pending",
        "billing": "pending",
    }
    with db.tx() as conn:  # nothing billed yet
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM voice_users WHERE order_ref IS NOT NULL AND billing_from IS NOT NULL"
            ).fetchone()["n"]
            == 0
        )
    r = client.post(f"{u}/voice/orders/{oid}/retry", headers=b["boss"]["h"])
    assert r.status_code == 200, r.text
    run_jobs()
    # A second, stray run of the job (a worker that died after finishing) changes nothing.
    r2 = client.post(f"{u}/voice/orders/{oid}/retry", headers=b["boss"]["h"])
    assert r2.status_code == 409
    o = client.get(f"{u}/voice/orders/{oid}", headers=b["boss"]["h"]).json()
    assert o["status"] == "active", o
    assert all(s["status"] == "done" for s in o["steps"])
    assert [s["attempts"] for s in o["steps"]] == [1, 2, 1, 1, 1]
    assert len(o["test_calls"]) == 2 and all(t["ok"] for t in o["test_calls"])
    with db.tx() as conn:
        users = conn.execute("SELECT count(*) AS n FROM voice_users WHERE order_ref IS NOT NULL").fetchone()["n"]
        nums = conn.execute(
            "SELECT source, status, billing_from IS NOT NULL AS billed FROM voice_numbers"
            " WHERE customer_id = %s ORDER BY source, e164",
            (b["id"],),
        ).fetchall()
        sim = conn.execute(
            "SELECT count(*) AS n FROM voice_sim_provider_numbers WHERE customer_id = %s", (b["id"],)
        ).fetchone()["n"]
        fees = conn.execute(
            "SELECT ref, amount FROM voice_charges WHERE customer_id = %s AND kind = 'one_time' ORDER BY ref",
            (b["id"],),
        ).fetchall()
    assert users == 2 and sim == 2  # no second number from the retried step
    assert [(n["source"], n["status"], n["billed"]) for n in nums] == [
        ("new", "active", True),
        ("new", "active", True),
        ("ported", "porting", False),
    ]
    assert sorted(str(f["amount"]) for f in fees) == ["0.0000", "0.0000", "0.0000", "10.0000", "25.0000"]
    ov = overview(client, b)
    assert any(g["name"] == "Main line" and len(g["members"]) == 2 for g in ov["ring_groups"])
    assert [r["name"] for r in ov["ai_rules"]] == ["After hours to the AI agent"]
    assert len(_events(b["id"], "voice.order_failed")) == 1 and len(_events(b["id"], "voice.order_active")) == 1

    # The port moves on separately and goes live only after its test call.
    port = client.get(f"{u}/voice/ports", headers=b["boss"]["h"]).json()[0]
    assert port["status"] == "submitted" and port["switch_date"] == "2026-11-30"
    assert (
        client.post(f"{u}/voice/ports/{port['id']}", json={"status": "completed"}, headers=b["boss"]["h"]).status_code
        == 403
    )
    r = client.post(f"{u}/voice/ports/{port['id']}", json={"status": "completed"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    assert next(n for n in overview(client, b)["numbers"] if n["source"] == "ported")["status"] == "active"

    # Softphone: a sign-in link (the QR code text) that works once.
    soft = next(d for d in ov["devices"] if d["kind"] == "softphone")
    link = client.post(f"{u}/voice/devices/{soft['id']}/link", headers=b["boss"]["h"]).json()["join_link"]
    token = link.split("token=")[1]
    j = client.post("/api/v1/commai/voice/softphone/join", json={"token": token})
    assert j.status_code == 200 and j.json()["extension"] == "221" and j.json()["password"]
    assert client.post("/api/v1/commai/voice/softphone/join", json={"token": token}).status_code == 404


def test_order_is_active_only_when_test_calls_work(client):
    b = setup_voice(client)
    u = base(b)
    items = {"site": "Port of Spain", "users": [{"name": "Ivy Dean", "number": True}]}
    o = client.post(f"{u}/voice/orders", json={"items": items}, headers=b["boss"]["h"]).json()
    providers.FAULTS["test_call"] = 1
    client.post(
        f"{u}/voice/orders/{o['id']}/approve",
        json={"accepted_price": {"monthly_delta": o["price"]["monthly_delta"], "one_time": o["price"]["one_time"]}},
        headers=b["boss"]["h"],
    )
    run_jobs()
    o = client.get(f"{u}/voice/orders/{o['id']}", headers=b["boss"]["h"]).json()
    assert o["status"] == "failed" and o["failed_step"] == "confirm"
    assert [t["ok"] for t in o["test_calls"]] == [False]  # the failed call is kept on the order
    with db.tx() as conn:
        row = conn.execute(
            "SELECT status, billing_from FROM voice_numbers WHERE order_ref LIKE %s", (f"order:{o['id']}:%",)
        ).fetchone()
        ivy = conn.execute("SELECT billing_from FROM voice_users WHERE name = 'Ivy Dean'").fetchone()
    assert row["status"] == "pending" and row["billing_from"] is None and ivy["billing_from"] is None
    client.post(f"{u}/voice/orders/{o['id']}/retry", headers=b["boss"]["h"])
    run_jobs()
    o = client.get(f"{u}/voice/orders/{o['id']}", headers=b["boss"]["h"]).json()
    assert o["status"] == "active" and [t["ok"] for t in o["test_calls"]] == [False, True]


def test_supplier_reconciliation(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    _call(client, b, "s1", "+1 868 555 0199", 600)  # billed 0.15
    _call(client, b, "s2", "+1 876 555 0100", 120)  # billed 0.08
    day = dt.date.today().isoformat()
    csv_text = (
        "call_ref,started_at,destination,seconds,cost\n"
        f"prov-s1,{day}T10:00:00,18685550199,600,0.08\n"
        f"prov-s2,{day}T10:05:00,18765550100,180,0.05\n"
        f"prov-zz,{day}T10:06:00,18685550100,60,0.01\n"
        "prov-bad,yesterday,1,1,abc\n"
    )
    assert client.post(f"{u}/voice/supplier/import", json={"csv": csv_text}, headers=b["boss"]["h"]).status_code == 403
    r = client.post(f"{u}/voice/supplier/import", json={"csv": csv_text, "filename": "oct.csv"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    assert r.json()["added"] == 3 and [x["row"] for x in r.json()["rejected"]] == [5]
    again = client.post(f"{u}/voice/supplier/import", json={"csv": csv_text}, headers=admin_headers).json()
    assert again["added"] == 0 and again["duplicates"] == 3  # never counted twice
    rec = client.get(f"{u}/voice/supplier/reconcile", headers=admin_headers).json()
    assert rec["billed_calls"] == "0.23" and rec["supplier_cost"] == "0.13" and rec["call_margin"] == "0.10"
    assert [i["call_id"] for i in rec["issues"]] == ["s2"] and "Duration differs" in rec["issues"][0]["issue"]
    assert rec["unmatched_supplier_rows"] >= 1
    assert client.get(f"{u}/voice/supplier/reconcile", headers=b["boss"]["h"]).status_code == 403
