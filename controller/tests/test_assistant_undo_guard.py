"""An old inverse must never overwrite a later operator's configuration."""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from exaconnect_controller import db
from exaconnect_controller.ai import act
from exaconnect_controller.security import hash_password

from .test_flow import _seed


def _applied_rule(client, headers, cid):
    with db.tx() as conn:
        proposal = act.create(
            conn,
            cid,
            "Prioritise Zoom calls",
            "Add a voice traffic rule.",
            [{"action": "traffic_rule", "name": "Calls", "class": "voice", "apps": ["zoom"]}],
            "user:admin@example.org",
        )
    assert proposal["problems"] == []
    response = client.post(
        f"/api/v1/assistant/plans/{proposal['id']}/approve", headers=headers, json={"revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/v1/assistant/plans/{proposal['id']}/execute",
        headers=headers,
        json={"revision": proposal["revision"], "idempotency_key": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    with db.tx() as conn:
        rule = conn.execute("SELECT * FROM traffic_rules WHERE customer_id = %s AND name = 'Calls'", (cid,)).fetchone()
        stored = act.get(conn, proposal["id"])
    assert len(stored["post_apply_revision"]) == 64
    return proposal, rule


def test_unchanged_applied_configuration_can_be_undone(client, admin_headers):
    cid = _seed()["customer_id"]
    proposal, rule = _applied_rule(client, admin_headers, cid)
    response = client.post(f"/api/v1/assistant/plans/{proposal['id']}/undo", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "undone"
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM traffic_rules WHERE id = %s", (rule["id"],)).fetchone() is None


def test_a_later_operators_edit_is_retained_when_undo_is_refused(client, admin_headers):
    cid = _seed()["customer_id"]
    proposal, rule = _applied_rule(client, admin_headers, cid)
    email = "operator@example.invalid"
    password = "fictional operator test password"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, customer_id) VALUES (%s, %s, 'customer', %s)",
            (email, hash_password(password), cid),
        )
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    other = {"Authorization": f"Bearer {response.json()['token']}"}
    # Changing only ordinal is a material edit which the model's compact
    # network projection omits. The undo fingerprint must still detect it.
    body = {key: value for key, value in rule.items() if key in ("name", "class_name", "apps")}
    body["ordinal"] = 700
    response = client.put(f"/api/v1/customers/{cid}/rules/{rule['id']}", headers=other, json=body)
    assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/assistant/plans/{proposal['id']}/undo", headers=admin_headers)
    assert response.status_code == 409, response.text
    assert "Configuration changed" in response.json()["detail"]
    with db.tx() as conn:
        current = conn.execute("SELECT ordinal FROM traffic_rules WHERE id = %s", (rule["id"],)).fetchone()
        assert current["ordinal"] == 700
        assert act.get(conn, proposal["id"])["status"] == "applied"
        assert (
            conn.execute(
                "SELECT 1 FROM audit_log WHERE action = 'assistant.undo' AND customer_id = %s", (cid,)
            ).fetchone()
            is None
        )


def test_legacy_applied_plan_without_snapshot_fails_closed(client, admin_headers):
    cid = _seed()["customer_id"]
    proposal, rule = _applied_rule(client, admin_headers, cid)
    with db.tx() as conn:
        conn.execute("UPDATE assistant_plans SET post_apply_revision = NULL WHERE id = %s", (proposal["id"],))
    response = client.post(f"/api/v1/assistant/plans/{proposal['id']}/undo", headers=admin_headers)
    assert response.status_code == 409, response.text
    assert "no verified undo snapshot" in response.json()["detail"]
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM traffic_rules WHERE id = %s", (rule["id"],)).fetchone()
        assert act.get(conn, proposal["id"])["status"] == "applied"


@pytest.mark.parametrize("saved", [None, "older-configuration"])
def test_missing_or_changed_snapshot_never_runs_an_inverse(monkeypatch, saved):
    inverse_calls = []
    monkeypatch.setattr(act, "_lock_configuration", lambda _conn: None)
    monkeypatch.setattr(act, "_post_apply_revision", lambda *_: "current-configuration")
    monkeypatch.setattr(act, "_undo_one", lambda *_: inverse_calls.append("undone"))
    plan = {
        "id": 1,
        "status": "applied",
        "customer_id": "org",
        "post_apply_revision": saved,
        "results": [{"action": 0, "undo": {"delete_rule": {"id": 9, "name": "Calls"}}}],
    }
    with pytest.raises(act.ApprovalError, match="Review it manually"):
        act.undo(None, plan, "user:operator")
    assert inverse_calls == []


def test_unchanged_inverse_is_checked_inside_configuration_lock(monkeypatch):
    operations = []
    monkeypatch.setattr(act, "_lock_configuration", lambda _conn: operations.append("lock"))
    monkeypatch.setattr(act, "_post_apply_revision", lambda *_: operations.append("digest") or "same")
    monkeypatch.setattr(act, "_undo_one", lambda *_: operations.append("inverse") or "Rule deleted")
    monkeypatch.setattr(act.audit, "record", lambda *_: operations.append("audit"))
    conn = SimpleNamespace(execute=lambda *_: operations.append("record"))
    plan = {
        "id": 1,
        "status": "applied",
        "customer_id": "org",
        "post_apply_revision": "same",
        "results": [{"action": 0, "undo": {"delete_rule": {"id": 9, "name": "Calls"}}}],
    }
    act.undo(conn, plan, "user:operator")
    assert operations == ["lock", "digest", "inverse", "record", "audit"]


def test_digest_includes_configuration_fields_outside_the_model_projection():
    rule = {"id": 9, "name": "Calls", "ordinal": 100, "enabled": True}

    class Connection:
        def execute(self, sql, _params):
            rows = [copy.deepcopy(rule)] if "FROM traffic_rules" in sql else []
            return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: {"shadow_mode": False})

    plan = {"id": 1, "customer_id": "org", "actions": [], "results": []}
    conn = Connection()
    original = act._post_apply_revision(conn, plan)
    assert act._post_apply_revision(conn, plan) == original
    for field, changed in (("ordinal", 700), ("enabled", False), ("id", 10)):
        before = rule[field]
        rule[field] = changed
        assert act._post_apply_revision(conn, plan) != original
        rule[field] = before


def test_apply_records_the_snapshot_after_configuration_changes(monkeypatch):
    operations = []
    results = [{"action": 0, "message": "Applied", "undo": {"shadow": False}}]
    monkeypatch.setattr(act, "review", lambda *_: {"problems": [], "actions": [{"action": "shadow_mode"}]})
    monkeypatch.setattr(act, "_apply_all", lambda *_: operations.append("apply") or results)

    def digest(_conn, _plan, resulting):
        assert resulting == results
        operations.append("digest")
        return "post-change-snapshot"

    monkeypatch.setattr(act, "_post_apply_revision", digest)
    monkeypatch.setattr(act.audit, "record", lambda *_: None)
    stored = []
    conn = SimpleNamespace(execute=lambda _sql, params: stored.append(params))
    act.apply(
        conn,
        {"id": 1, "status": "draft", "customer_id": "org", "actions": [], "question": "Change it"},
        "user:operator",
    )
    assert operations == ["apply", "digest"]
    assert stored[0][2] == "post-change-snapshot"
