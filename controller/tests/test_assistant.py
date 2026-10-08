"""The assistant: Ask proposes changes, a person applies them, and they can be undone (ADR 0015)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from exaconnect_controller import db
from exaconnect_controller.ai import act
from exaconnect_controller.security import hash_password

from .test_flow import _seed


class _Model(BaseHTTPRequestHandler):
    reply = ""
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Model.seen.append(body)
        data = json.dumps({"choices": [{"message": {"content": _Model.reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def model(client):
    srv = HTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Model.seen = []
    client.app.state.settings = replace(
        client.app.state.settings,
        llm_api_key="test-key-not-real",
        llm_base_url=f"http://127.0.0.1:{srv.server_port}",
        llm_model="test-model",
    )

    def say(answer: str, actions: list[dict]) -> None:
        _Model.reply = "```json\n" + json.dumps({"answer": answer, "actions": actions}) + "\n```"

    yield say
    srv.shutdown()


def _ask(client, headers, cid, question, history=None):
    r = client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={"question": question, "customer_id": cid, "history": history or []},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _state(cid):
    with db.tx() as conn:
        return {
            "rules": [r["name"] for r in conn.execute("SELECT name FROM traffic_rules WHERE customer_id = %s", (cid,))],
            "voice_loss": float(
                conn.execute(
                    "SELECT max_loss_pct FROM sla_policies WHERE customer_id = %s AND class_name = 'voice'", (cid,)
                ).fetchone()["max_loss_pct"]
            ),
            "storm": {
                r["name"]: r["storm_mode"]
                for r in conn.execute(
                    "SELECT name, storm_mode FROM sites WHERE customer_id = %s AND kind = 'site'", (cid,)
                )
            },
            "firewall": conn.execute(
                "SELECT count(*) AS n FROM firewall_rules WHERE customer_id = %s", (cid,)
            ).fetchone()["n"],
            "forwards": conn.execute(
                "SELECT count(*) AS n FROM port_forwards WHERE customer_id = %s", (cid,)
            ).fetchone()["n"],
            "orders": [
                r["status"]
                for r in conn.execute("SELECT status FROM orders WHERE customer_id = %s ORDER BY id", (cid,))
            ],
        }


def test_parse_takes_json_or_plain_text():
    assert act.parse('{"answer": "Done?", "actions": [{"action": "shadow_mode", "on": true}, 3]}') == (
        "Done?",
        [{"action": "shadow_mode", "on": True}],
    )
    assert act.parse("<b>Voice moved at 04:31.</b>") == ("<b>Voice moved at 04:31.</b>", [])
    assert act.parse('{"actions": []}') == ('{"actions": []}', [])


def test_a_question_gets_an_answer_and_no_plan(client, admin_headers, model):
    cid = _seed()["customer_id"]
    model("Voice is on Carrier A at both sites.", [])
    out = _ask(client, admin_headers, cid, "Where is voice now?")
    assert out["answer"] == "Voice is on Carrier A at both sites." and out["plan"] is None
    msg = _Model.seen[0]["messages"][1]["content"]
    snap = json.loads(msg.split("(JSON):\n", 1)[1].split("\n\nPerson's message:", 1)[0])
    assert {"sites", "configuration"} <= set(snap)
    assert {"traffic_rules", "firewall_rules", "port_forwards", "app_suggestions", "apps"} <= set(snap["configuration"])


def test_propose_apply_and_undo(client, admin_headers, model):
    cid = _seed()["customer_id"]
    before = _state(cid)
    model(
        "Zoom isn't in a class yet, and voice loss on Carrier A is rising at Kingston. I propose these changes.",
        [
            {"action": "traffic_rule", "name": "All calls", "class": "voice", "sites": [], "apps": ["zoom", "teams"]},
            {"action": "sla", "class": "voice", "max_loss_pct": 0.5, "max_latency_ms": None},
            {"action": "storm_mode", "on": True, "sites": ["Kingston"]},
            {"action": "firewall_rule", "site": None, "rule": "deny", "protocol": "tcp", "ports": "25"},
            {
                "action": "port_forward",
                "protocol": "tcp",
                "port": 8443,
                "site": "site-a",
                "to_address": "192.168.10.10",
                "to_port": 443,
            },
            {"action": "order", "changes": [{"action": "internet_mode", "site": "site-b", "mode": "local"}]},
        ],
    )
    out = _ask(client, admin_headers, cid, "Make sure calls stay good in Kingston")
    plan = out["plan"]
    assert plan["status"] == "draft" and plan["problems"] == [], plan
    assert (
        plan["summary"][0]
        == 'Add the traffic rule "All calls": put Zoom meetings, Microsoft Teams calls in voice at all sites.'
    )
    assert plan["summary"][1] == "Change voice's SLA: loss 1% to 0.5%."
    assert plan["summary"][2] == "Switch Storm Mode on at site-a."
    assert plan["summary"][5].startswith("Draft an order for you to review")
    # Proposing changes nothing.
    assert _state(cid) == before

    r = client.post(f"/api/v1/ai/plans/{plan['id']}/apply", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"
    after = _state(cid)
    assert after["rules"] == ["All calls"] and after["voice_loss"] == 0.5 and after["storm"]["site-a"] is True
    assert after["firewall"] == 1 and after["forwards"] == 1 and after["orders"] == ["draft"]
    # Applied once only.
    assert client.post(f"/api/v1/ai/plans/{plan['id']}/apply", headers=admin_headers).status_code == 400

    r = client.post(f"/api/v1/ai/plans/{plan['id']}/undo", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "undone"
    assert _state(cid) == {**before, "orders": ["cancelled"]}
    audit = [a["action"] for a in client.get("/api/v1/audit", headers=admin_headers).json()]
    assert {"assistant.propose", "assistant.apply", "assistant.undo"} <= set(audit)
    listed = client.get(f"/api/v1/customers/{cid}/assistant/plans", headers=admin_headers).json()
    assert [p["status"] for p in listed] == ["undone"] and '"undo":' not in json.dumps(listed[0]["results"])


def test_problems_are_shown_before_anything_applies(client, admin_headers, model):
    cid = _seed()["customer_id"]
    model(
        "Proposed.",
        [
            {"action": "traffic_rule", "name": "Odd", "class": "gold", "apps": ["zoom"]},
            {"action": "storm_mode", "on": False, "sites": []},
            {"action": "reboot_router"},
        ],
    )
    plan = _ask(client, admin_headers, cid, "Do odd things")["plan"]
    assert "There is no class called gold." in plan["problems"]
    assert "Storm Mode is already off at site-a, site-b." in plan["problems"]
    assert "The assistant can't do 'reboot_router' yet." in plan["problems"]
    r = client.post(f"/api/v1/ai/plans/{plan['id']}/apply", headers=admin_headers)
    assert r.status_code == 400

    # A problem only applying would find (an address outside the site) shows in the draft, and nothing applies.
    model(
        "Proposed.",
        [
            {"action": "sla", "class": "voice", "max_loss_pct": 2},
            {"action": "port_forward", "protocol": "tcp", "port": 2222, "site": "site-a", "to_address": "10.9.9.9"},
        ],
    )
    plan = _ask(client, admin_headers, cid, "Open SSH")["plan"]
    assert plan["problems"] and "is not inside site-a's networks" in plan["problems"][0]
    assert client.post(f"/api/v1/ai/plans/{plan['id']}/apply", headers=admin_headers).status_code == 400
    assert _state(cid)["voice_loss"] == 1.0
    r = client.post(f"/api/v1/ai/plans/{plan['id']}/cancel", headers=admin_headers)
    assert r.json()["status"] == "cancelled"


def test_suggestions_and_deletes_undo_cleanly(client, admin_headers, model):
    cid = _seed()["customer_id"]
    with db.tx() as conn:
        site = conn.execute("SELECT id FROM sites WHERE customer_id = %s AND name = 'site-a'", (cid,)).fetchone()["id"]
        det = conn.execute(
            """INSERT INTO app_detections (customer_id, site_id, key, app_id, label, proto, dport, current_class,
                                          suggested_class, profile, confidence, reason)
               VALUES (%s, %s, 'zoom', 'zoom', 'Zoom meetings', 'udp', 8801, '', 'voice', 'realtime', 0.9, 'Seen')
               RETURNING id""",
            (cid, site),
        ).fetchone()["id"]
    model("Proposed.", [{"action": "apply_suggestion", "id": det, "class": None}])
    plan = _ask(client, admin_headers, cid, "Apply the Zoom suggestion")["plan"]
    assert plan["summary"] == ["Apply the suggestion for Zoom meetings at site-a: put it in voice."]
    assert client.post(f"/api/v1/ai/plans/{plan['id']}/apply", headers=admin_headers).status_code == 200
    assert _state(cid)["rules"] == ["Zoom meetings at site-a"]

    # Deleting that rule through the assistant, then undoing, brings it back.
    model("Proposed.", [{"action": "delete_traffic_rule", "rule": "Zoom meetings at site-a"}])
    plan2 = _ask(client, admin_headers, cid, "Remove it")["plan"]
    assert client.post(f"/api/v1/ai/plans/{plan2['id']}/apply", headers=admin_headers).status_code == 200
    assert _state(cid)["rules"] == []
    assert client.post(f"/api/v1/ai/plans/{plan2['id']}/undo", headers=admin_headers).status_code == 200
    assert _state(cid)["rules"] == ["Zoom meetings at site-a"]

    # Undoing the first plan removes the rule and reopens the suggestion.
    assert client.post(f"/api/v1/ai/plans/{plan['id']}/undo", headers=admin_headers).status_code == 200
    with db.tx() as conn:
        assert (
            conn.execute("SELECT status FROM app_detections WHERE id = %s", (det,)).fetchone()["status"] == "suggested"
        )
    assert _state(cid)["rules"] == []


def test_the_conversation_carries_on_and_others_cannot_act(client, admin_headers, model):
    cid = _seed()["customer_id"]
    model("Shadow mode would log decisions without acting.", [{"action": "shadow_mode", "on": True}])
    plan = _ask(
        client,
        admin_headers,
        cid,
        "Yes, do that",
        history=[{"q": "Can I try routing without it acting?", "a": "Yes, with shadow mode."}],
    )["plan"]
    assert "Person: Can I try routing without it acting?" in _Model.seen[-1]["messages"][1]["content"]

    with db.tx() as conn:
        other = conn.execute("INSERT INTO customers (name) VALUES ('Other Org') RETURNING id").fetchone()["id"]
        conn.execute(
            "INSERT INTO users (email, password_hash, role, customer_id)"
            " VALUES ('it@other.example', %s, 'customer', %s)",
            (hash_password("a long customer password"), other),
        )
    r = client.post("/api/v1/auth/login", json={"email": "it@other.example", "password": "a long customer password"})
    other_h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post(f"/api/v1/ai/plans/{plan['id']}/apply", headers=other_h).status_code == 403
    assert client.get(f"/api/v1/customers/{cid}/assistant/plans", headers=other_h).status_code == 403
    assert client.post(f"/api/v1/ai/plans/{plan['id']}/apply", headers=admin_headers).json()["status"] == "applied"
