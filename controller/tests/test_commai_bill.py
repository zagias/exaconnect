"""The single bill, money budgets and the AI supplier side (ADR 0039)."""

import datetime as dt
from decimal import Decimal

import psycopg
import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import ai_supplier, bill, inbox, usage

from .commai_helpers import base, business

PERIOD = dt.datetime.now(dt.UTC).strftime("%Y-%m")


def _use(b, meter, qty=1, ref="", detail=None):
    with db.tx() as conn:
        return usage.record(conn, b["id"], meter, qty, ref=ref, detail=detail)


def _voice_charge(b, amount="5.0000", ref="one_time:desk"):
    with db.tx() as conn:
        return conn.execute(
            """INSERT INTO voice_charges (customer_id, ref, kind, description, quantity, unit_price, amount)
               VALUES (%s, %s, 'one_time', 'Desk phone', 1, %s, %s) RETURNING id""",
            (b["id"], ref, amount, amount),
        ).fetchone()["id"]


def _events(b, type_):
    with db.tx() as conn:
        return conn.execute(
            "SELECT data FROM commai_events WHERE customer_id = %s AND type = %s ORDER BY seq", (b["id"], type_)
        ).fetchall()


# ---- the single bill -------------------------------------------------------------------------------


def test_one_bill_with_messaging_ai_workflows_and_voice(client, admin_headers):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    for i in range(3):
        _use(b, "ai_reply", ref=f"r{i}")
    _use(b, "ai_tokens", 1000, ref="r0", detail={"model": "m1"})
    _use(b, "message_out:whatsapp", ref="w1")
    _use(b, "message_out:whatsapp", ref="w2")
    _use(b, "workflow_run", ref="run1", detail={"workflow_id": "00000000-0000-0000-0000-000000000001"})
    _use(b, "ai_voice_minute", 2, ref="call:1", detail={"speech": "browser"})
    _use(b, "ai_voice_minute", 3, ref="phone-call")  # phone AI minutes: rated by voice billing, not here
    vc = _voice_charge(b)
    with db.tx() as conn:
        charges = conn.execute("SELECT meter, amount FROM commai_charges WHERE customer_id = %s", (b["id"],)).fetchall()
    assert len(charges) == 8  # every record but the phone AI minutes
    # Businesses see usage priced on the (example) card but can't build or issue bills.
    usage_view = client.get(f"{u}/bill/usage", headers=h).json()
    assert usage_view["example_prices"] is True
    assert client.post(f"{u}/bills/draft", json={"period": PERIOD}, headers=h).status_code == 403
    r = client.post(f"{u}/bills/draft", json={"period": PERIOD}, headers=admin_headers)
    assert r.status_code == 201, r.text
    draft = r.json()
    lines = {(ln["family"], ln["meter"]): ln for ln in draft["lines"]}
    assert lines[("ai", "ai_reply")]["quantity"] == "3" and lines[("ai", "ai_reply")]["amount"] == "0.06"
    assert lines[("messaging", "message_out:whatsapp")]["amount"] == "0.03"
    assert lines[("automation", "workflow_run")]["amount"] == "0.00"  # 0.002 shows as 0.00 per line
    assert lines[("ai", "ai_voice_minute")]["amount"] == "0.12"
    assert lines[("voice", "one_time")]["voice_charge_id"] == str(vc)
    expected = Decimal("0.06") + Decimal("0.002") + Decimal("0.03") + Decimal("0.002") + Decimal("0.12") + Decimal("5")
    assert Decimal(draft["total"]) == bill.q2(expected)
    assert draft["by_family"]["voice"] == "5.00"
    # A line traces to the usage behind it.
    trace = client.get(f"{u}/bills/{draft['id']}/lines/{lines[('ai', 'ai_reply')]['id']}/charges", headers=h).json()
    assert sorted(t["ref"] for t in trace) == ["r0", "r1", "r2"]
    # Issue: frozen, numbered, and the voice charge can't go on a voice-only invoice too.
    issued = client.post(f"{u}/bills/{draft['id']}/issue", headers=admin_headers).json()
    assert issued["status"] == "issued" and issued["number"].startswith("EB-")
    with pytest.raises(psycopg.errors.CheckViolation), db.tx() as conn:
        conn.execute("UPDATE commai_bills SET total = 0 WHERE id = %s", (draft["id"],))
    with pytest.raises(psycopg.errors.CheckViolation), db.tx() as conn:
        conn.execute("DELETE FROM commai_bill_lines WHERE bill_id = %s", (draft["id"],))
    assert client.post(f"{u}/bills/{draft['id']}/issue", headers=admin_headers).status_code == 409
    vinv = client.post(f"{u}/voice/invoices/draft", json={"period": PERIOD}, headers=admin_headers)
    assert vinv.status_code == 201, vinv.text
    assert not any(ln.get("charge_id") == str(vc) for ln in vinv.json()["lines"])
    # New usage after issue goes on the next draft only.
    _use(b, "ai_reply", ref="r9")
    again = client.post(f"{u}/bills/draft", json={"period": PERIOD}, headers=admin_headers).json()
    assert again["id"] != draft["id"] and [ln["meter"] for ln in again["lines"]] == ["ai_reply"]
    # Corrections are credit notes pointing at the line they correct.
    wa = lines[("messaging", "message_out:whatsapp")]
    over = client.post(
        f"{u}/bills/{draft['id']}/credit",
        json={"lines": [{"line_id": wa["id"], "amount": "1.00"}], "reason": "Goodwill"},
        headers=admin_headers,
    )
    assert over.status_code == 422
    cn = client.post(
        f"{u}/bills/{draft['id']}/credit",
        json={"lines": [{"line_id": wa["id"], "amount": "0.01"}], "reason": "Goodwill"},
        headers=admin_headers,
    )
    assert cn.status_code == 201, cn.text
    assert cn.json()["number"].startswith("EC-") and cn.json()["total"] == "-0.01"
    assert cn.json()["lines"][0]["credits_line"] == wa["id"]
    listed = client.get(f"{u}/bills?limit=1", headers=h).json()
    assert len(listed["items"]) == 1 and listed["next"]
    more = client.get(f"{u}/bills", params={"cursor": listed["next"], "limit": 10}, headers=h).json()
    assert len(more["items"]) == 2
    # Another business can't see this bill.
    other = business(client, "Other Co")
    assert client.get(f"{u}/bills/{draft['id']}", headers=other["agent"]["h"]).status_code == 403


def test_rate_card_versions_price_usage_when_it_happened(client, admin_headers):
    b = business(client)
    u = base(b)
    _use(b, "ai_reply", ref="before")
    assert (
        client.post(f"{u}/bill/rate-cards", json={"prices": {"ai_reply": "1"}}, headers=b["agent"]["h"]).status_code
        == 403
    )
    bad = client.post(f"{u}/bill/rate-cards", json={"prices": {"voice_minute": "1"}}, headers=admin_headers)
    assert bad.status_code == 422
    r = client.post(
        f"{u}/bill/rate-cards", json={"prices": {"ai_reply": "0.05"}, "label": "2027 prices"}, headers=admin_headers
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    _use(b, "ai_reply", ref="after")
    _use(b, "message_out:email", ref="e1")  # not on card v2: not priced
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT meter, rate_card_version, unit_price FROM commai_charges WHERE customer_id = %s ORDER BY at",
            (b["id"],),
        ).fetchall()
    assert [(r["rate_card_version"], str(r["unit_price"].normalize())) for r in rows] == [(1, "0.02"), (2, "0.05")]
    cards = client.get(f"{u}/bill/rate-cards", headers=b["agent"]["h"]).json()
    assert [c["version"] for c in cards] == [2, 1] and cards[1]["example"] is True


# ---- money budgets ----------------------------------------------------------------------------------


def test_money_budgets_alert_and_stop_ai_and_messages(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    r = client.put(f"{u}/budgets/ai", json={"monthly_alert": "0.03", "monthly_hard": "0.05"}, headers=h)
    assert r.status_code == 200, r.text
    assert client.put(f"{u}/budgets/ai", json={"monthly_alert": "2", "monthly_hard": "1"}, headers=h).status_code == 422
    assert client.put(f"{u}/budgets/channel", json={"key": "fax", "monthly_hard": "1"}, headers=h).status_code == 422
    assert client.put(f"{u}/budgets/ai", json={"monthly_hard": "1"}, headers=b["internal"]["h"]).status_code == 403
    with db.tx() as conn:
        assert usage.allowed(conn, b["id"], "ai_reply")
    _use(b, "ai_reply", ref="a1")
    _use(b, "ai_reply", ref="a2")
    assert len(_events(b, "usage.budget_alert")) == 1
    with db.tx() as conn:
        assert not usage.allowed(conn, b["id"], "ai_reply")  # 0.04 + 0.02 would pass 0.05
        assert usage.allowed(conn, b["id"], "message_out:sms")  # not AI
    _use(b, "ai_reply", ref="a3")
    reached = _events(b, "usage.budget_reached")
    assert len(reached) == 1 and reached[0]["data"]["scope"] == "ai"
    with db.tx() as conn:
        assert not usage.allowed(conn, b["id"], "ai_reply")
        assert "budget for AI" in usage.blocked_by(conn, b["id"], "copilot")
    listed = {x["scope"]: x for x in client.get(f"{u}/budgets", headers=h).json()}
    assert listed["ai"]["state"] == "stopped" and listed["ai"]["spent_this_month"] == "0.06"
    # A channel budget stops replies on that channel, for people, the AI and workflows alike.
    with db.tx() as conn:
        conv = inbox.receive(conn, b["id"], "web", "+18685550142", "Hi", name="Al")["conversation"]
    client.put(f"{u}/budgets/channel", json={"key": "web", "monthly_hard": "0"}, headers=h)
    rep = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Hello"}, headers=h)
    assert rep.status_code == 422 and "budget for Website chat" in rep.json()["detail"]
    assert _events(b, "message.blocked")
    assert client.delete(f"{u}/budgets/channel?key=web", headers=h).status_code == 204
    assert client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Hello"}, headers=h).status_code == 201


def test_quantity_hard_limit_now_stops_outbound_messages(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    with db.tx() as conn:
        conv = inbox.receive(conn, b["id"], "web", "+18685550143", "Hi", name="Al")["conversation"]
        conn.execute(
            "INSERT INTO usage_limits (customer_id, meter, monthly_hard) VALUES (%s, 'message_out:web', 0)", (b["id"],)
        )
    rep = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Hello"}, headers=h)
    assert rep.status_code == 422 and "limit" in rep.json()["detail"]


def test_workflow_and_total_budgets(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    wf = client.post(
        f"{u}/workflows",
        json={
            "definition": {
                "name": "Ping",
                "trigger": {"type": "manual"},
                "steps": [{"type": "condition", "if": [{"field": "event.type", "op": "exists"}]}],
            }
        },
        headers=h,
    ).json()["workflow"]
    assert client.post(f"{u}/workflows/{wf['id']}/publish", json={}, headers=h).status_code == 200
    assert client.put(f"{u}/budgets/workflow", json={"key": "nope", "monthly_hard": "1"}, headers=h).status_code == 422
    r = client.put(f"{u}/budgets/workflow", json={"key": wf["id"], "monthly_hard": "0.003"}, headers=h)
    assert r.status_code == 200 and r.json()["label"] == 'the workflow "Ping"'
    first = client.post(f"{u}/workflows/{wf['id']}/trigger", json={}, headers=h)
    assert first.status_code == 201, first.text
    second = client.post(f"{u}/workflows/{wf['id']}/trigger", json={}, headers=h)
    assert second.status_code == 409 and "budget" in second.json()["detail"]
    # Voice spend counts towards the total and the voice channel budget.
    client.put(f"{u}/budgets/channel", json={"key": "voice", "monthly_alert": "1"}, headers=h)
    _voice_charge(b, "2.0000", ref="call:x")
    _use(b, "voice_minute", 3, ref="x", detail={"amount": "2.0000"})
    alerts = _events(b, "usage.budget_alert")
    assert alerts and alerts[-1]["data"]["key"] == "voice"
    with db.tx() as conn:
        assert bill.spend(conn, b["id"], "total") == Decimal("2.002")


# ---- AI supplier side ---------------------------------------------------------------------------------


def test_ai_supplier_costs_reconciled_with_margin(client, admin_headers, monkeypatch):
    a = business(client, "Alpha Shop")
    b = business(client, "Beta Shop")
    _use(a, "ai_reply", ref="a1")
    _use(a, "ai_tokens", 3000, ref="a1", detail={"model": "m1"})
    _use(b, "ai_tokens", 1000, ref="b1", detail={"model": "m1"})
    _use(a, "ai_voice_minute", 2, ref="call:a", detail={"speech": "browser"})
    assert (
        client.post(
            "/api/v1/commai/exacarib/ai-supplier/import", json={"period": PERIOD}, headers=a["agent"]["h"]
        ).status_code
        == 403
    )
    monkeypatch.delenv("EXA_DEEPINFRA_API_KEY", raising=False)
    no_key = client.post(
        "/api/v1/commai/exacarib/ai-supplier/import",
        json={"period": PERIOD, "source": "deepinfra"},
        headers=admin_headers,
    )
    assert no_key.status_code == 409 and "EXA_DEEPINFRA_API_KEY" in no_key.json()["detail"]
    imp = client.post("/api/v1/commai/exacarib/ai-supplier/import", json={"period": PERIOD}, headers=admin_headers)
    assert imp.status_code == 201 and imp.json()["simulated"] is True and imp.json()["rows"] == 2
    r = client.get(f"{base(a)}/ai-supplier/reconcile", params={"period": PERIOD}, headers=admin_headers).json()
    assert r["simulated"] is True and r["issues"] == []
    # Alpha used 3000 of 4000 tokens on m1: three quarters of the 4000-token cost, plus all its speech.
    token_cost = Decimal(4000) * ai_supplier.SIMULATED_TOKEN_PRICE["default"] * 3 / 4
    speech_cost = Decimal(120) * ai_supplier.SIMULATED_SPEECH_PER_SECOND
    assert Decimal(r["supplier_cost"]) == bill.q2(token_cost + speech_cost)
    billed = Decimal("0.02") + Decimal(3000) * Decimal("0.000002") + Decimal(2) * Decimal("0.06")
    assert Decimal(r["billed_ai"]) == bill.q2(billed)
    assert abs(Decimal(r["margin"]) - (billed - token_cost - speech_cost)) <= Decimal("0.01")
    # The supplier counts differently: the difference is listed.
    with db.tx() as conn:
        conn.execute("UPDATE ai_supplier_costs SET quantity = quantity + 50 WHERE model = 'm1'")
    r = client.get(f"{base(a)}/ai-supplier/reconcile", params={"period": PERIOD}, headers=admin_headers).json()
    assert len(r["issues"]) == 1 and "Supplier counted 4050 tokens, CommAI recorded 4000" in r["issues"][0]["issue"]
    assert client.get(f"{base(a)}/ai-supplier/reconcile", headers=a["agent"]["h"]).status_code == 403
