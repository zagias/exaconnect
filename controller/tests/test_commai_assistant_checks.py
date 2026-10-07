# ruff: noqa: F811  (pytest fixtures imported from test_commai_automation_helpers)
"""Assistant diagnostics for voice, AI agents and sign-in, and phone changes
typed to the assistant (ADR 0039)."""

import datetime as dt
import uuid

from exaconnect_controller import audit, db
from exaconnect_controller.commai import diagnostics

from .commai_helpers import base, business
from .test_commai_automation_helpers import secrets_env  # noqa: F401
from .test_commai_voice import overview, setup_voice


def _areas(cid):
    with db.tx() as conn:
        return {f["area"]: f for f in diagnostics.run(conn, cid) if f["status"] == "problem"}


def test_voice_checks_find_failed_orders_stale_pbx_blocked_calls_and_ports(client):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    cid = b["id"]
    with db.tx() as conn:
        order = conn.execute(
            """INSERT INTO voice_orders (customer_id, status, items, failed_step, error, created_by)
               VALUES (%s, 'failed', '{}', 'numbers', 'provider timed out', 'test') RETURNING id""",
            (cid,),
        ).fetchone()
        now = dt.datetime.now(dt.UTC)
        for i in range(3):
            conn.execute(
                """INSERT INTO voice_cdrs (customer_id, call_id, direction, to_number, started_at, ended_at, status,
                                           block_reason)
                   VALUES (%s, %s, 'outbound', '+1900555000', %s, %s, 'blocked', 'premium-rate prefix 1900')""",
                (cid, f"c{i}", now, now),
            )
        conn.execute(
            """INSERT INTO voice_port_orders (customer_id, e164, status, note)
               VALUES (%s, '+18685550999', 'rejected', 'name does not match the account')""",
            (cid,),
        )
    a = _areas(cid)
    assert a["voice:orders"]["fix"]["params"]["order_id"] == str(order["id"])
    assert "numbers step" in a["voice:orders"]["summary"]
    assert "3 call(s) were blocked" in a["voice:fraud"]["summary"]
    assert "rejected" in a["voice:porting"]["summary"]
    assert a["voice:pbx"]["confidence"] == "likely"  # never built in this test
    # The assistant answers a voice question from these findings and offers the fixes.
    r = client.post(f"{u}/assistant/ask", json={"question": "Why are some calls not going through?"}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert {f["area"] for f in out["findings"]} >= {"voice:orders", "voice:fraud", "voice:pbx"}
    fixes = {f["fix_id"]: f for f in out["fixes"]}
    assert set(fixes) >= {"retry_voice_order", "render_pbx"}
    done = client.post(f"{u}/assistant/fixes/{fixes['render_pbx']['id']}/apply", headers=h).json()
    assert done["resolved"], done
    retry = client.post(f"{u}/assistant/fixes/{fixes['retry_voice_order']['id']}/apply", headers=h).json()
    assert retry["status"] == "applied"
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM jobs WHERE kind = 'voice.provision' AND customer_id = %s", (cid,)).fetchone()


def test_ai_agent_checks(client):
    b = business(client)
    cid = b["id"]
    with db.tx() as conn:
        for i in range(12):
            outcome = "escalated" if i < 8 else "replied"
            reason = "The customer agent is not allowed to create a lead." if i < 2 else "No approved knowledge"
            conn.execute(
                "INSERT INTO ai_runs (customer_id, role, outcome, reason) VALUES (%s, 'customer_agent', %s, %s)",
                (cid, outcome, reason if outcome == "escalated" else ""),
            )
        conn.execute(
            "INSERT INTO ai_runs (customer_id, role, outcome, reason) VALUES (%s, 'customer_agent', 'failed', %s)",
            (cid, "ModelError: the model timed out"),
        )
        conn.execute(
            """INSERT INTO knowledge_gaps (customer_id, question_key, question, reason, times)
               VALUES (%s, 'parking', 'Is there parking?', 'missing', 4)""",
            (cid,),
        )
    a = _areas(cid)
    assert "failed 1 time" in a["ai_agents:model"]["summary"]
    assert "8 of 12" in a["ai_agents:escalation"]["summary"]
    assert "Is there parking?" in a["ai_agents:knowledge"]["evidence"][0]
    assert "not allowed" in a["ai_agents:tools"]["evidence"][0]
    r = client.post(
        f"{base(b)}/assistant/ask",
        json={"question": "Why does the AI agent hand over so much?"},
        headers=b["agent"]["h"],
    ).json()
    assert {f["area"].split(":")[0] for f in r["findings"]} == {"ai_agents"}


def test_signin_checks(client):
    b = business(client)
    cid = b["id"]
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO sso_connections (customer_id, alias, protocol, display_name, status, require_sso, last_test)
               VALUES (%s, %s, 'oidc', 'Entra ID', 'tested', true, '{"ok": false, "detail": "issuer mismatch"}')""",
            (cid, f"t-{uuid.uuid4().hex[:8]}"),
        )
        conn.execute("INSERT INTO scim_groups (customer_id, display_name) VALUES (%s, 'Sales')", (cid,))
        for _ in range(6):
            audit.record(
                conn, f"user:{b['agent']['email']}", "login_failed", b["agent"]["email"], detail={"reason": "password"}
            )
        # Another business's failures don't count.
        audit.record(conn, "user:x@elsewhere.org", "login_failed", "x@elsewhere.org", detail={"reason": "password"})
    with db.tx() as conn:
        found = [f for f in diagnostics.run(conn, cid, ["signin"]) if f["status"] == "problem"]
    summaries = " | ".join(f["summary"] for f in found)
    assert "required, but the connection is tested" in summaries
    assert "last test of Entra ID failed" in summaries
    assert "no live directory sync token" in summaries
    fails = next(f for f in found if f["area"] == "signin:failures")
    assert fails["summary"].startswith("6 sign-ins failed for 1 of your people")
    assert b["agent"]["email"] not in str(found)
    r = client.post(
        f"{base(b)}/assistant/ask", json={"question": "People can't sign in with SSO"}, headers=b["agent"]["h"]
    ).json()
    assert r["findings"] and all(f["area"].startswith("signin") for f in r["findings"])
    assert r["confidence"] == "confirmed"


def test_voice_change_through_the_assistant_needs_the_same_confirm(client):
    b = setup_voice(client)
    u = base(b)
    h = b["lead"]["h"]  # a voice admin with business admin rights
    r = client.post(f"{u}/assistant/ask", json={"question": "move Ben to San Fernando"}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    vc = out["voice_change"]
    assert vc["understood"] and vc["scope"] == "admin" and "emergency address" in vc["summary"]
    assert "Nothing changes until you confirm" in out["answer"]
    assert "price_impact" in vc
    assert next(x for x in overview(client, b)["users"] if x["extension"] == "202")["site"] == "Port of Spain"
    # Someone else can't confirm it; the person who asked can, once.
    assert client.post(f"{u}/voice/say/{vc['id']}/confirm", json={}, headers=b["boss"]["h"]).status_code == 404
    c = client.post(f"{u}/voice/say/{vc['id']}/confirm", json={}, headers=h)
    assert c.status_code == 200, c.text
    assert next(x for x in overview(client, b)["users"] if x["extension"] == "202")["site"] == "San Fernando"
    # A question still goes to the diagnostics, not the change parser.
    q = client.post(f"{u}/assistant/ask", json={"question": "Why are calls to Ben failing?"}, headers=h).json()
    assert "voice_change" not in q and q["source"] == "rules"
    # Not a phone change at all: diagnostics as before.
    q = client.post(f"{u}/assistant/ask", json={"question": "WhatsApp stopped sending"}, headers=h).json()
    assert "voice_change" not in q
    # Without the voice module, the assistant does not make phone changes.
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO commai_entitlements (customer_id, module, enabled, updated_by)
               VALUES (%s, 'voice', false, 'test')""",
            (b["id"],),
        )
    q = client.post(f"{u}/assistant/ask", json={"question": "move Ben to Port of Spain"}, headers=h).json()
    assert "voice_change" not in q
