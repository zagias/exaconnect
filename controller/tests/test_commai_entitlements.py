"""Module entitlements (ADR 0039): messaging, voice, AI agents and automation
are sold separately and checked on the server for each module's routes."""

from exaconnect_controller import db
from exaconnect_controller.commai import entitlements

from .commai_helpers import base, business


def test_existing_businesses_keep_every_module(client):
    b = business(client)
    r = client.get(f"{base(b)}/entitlements", headers=b["agent"]["h"])
    assert r.status_code == 200
    assert all(m["enabled"] for m in r.json()["modules"])
    assert {m["module"] for m in r.json()["modules"]} == {"messaging", "voice", "ai_agents", "automation"}
    for path in ("/channels/outbox", "/workflows", "/voice", "/ai/profile"):
        assert client.get(base(b) + path, headers=b["agent"]["h"]).status_code != 403, path


def test_only_exacarib_sets_modules_and_routes_are_enforced(client, admin_headers):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    r = client.put(f"{u}/entitlements", json={"modules": {"voice": False}}, headers=h)
    assert r.status_code == 403
    r = client.put(f"{u}/entitlements", json={"modules": {"nope": False}}, headers=admin_headers)
    assert r.status_code == 422
    r = client.put(
        f"{u}/entitlements",
        json={"modules": {"voice": False, "automation": False}, "note": "messaging only"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert {m["module"]: m["enabled"] for m in r.json()["modules"]} == {
        "messaging": True,
        "voice": False,
        "ai_agents": True,
        "automation": False,
    }
    blocked = client.get(f"{u}/workflows", headers=h)
    assert blocked.status_code == 403
    assert "not part of this business's plan" in blocked.json()["detail"]
    assert client.get(f"{u}/voice", headers=h).status_code == 403
    assert client.post(f"{u}/workflows", json={"definition": {}}, headers=h).status_code == 403
    # Even an ExaCarib admin hits the plan on the business's module routes.
    assert client.get(f"{u}/workflows", headers=admin_headers).status_code == 403
    # Core features stay: inbox, the assistant, support cases, reports.
    assert client.get(f"{u}/conversations", headers=h).status_code == 200
    assert client.get(f"{u}/support-cases", headers=h).status_code == 200
    assert client.get(f"{u}/reports/usage", headers=h).status_code == 200
    assert client.get(f"{u}/channels/outbox", headers=h).status_code == 200
    # Signed-out callers learn nothing about the plan.
    assert client.get(f"{u}/workflows").status_code == 401
    with db.tx() as conn:
        assert not entitlements.enabled(conn, b["id"], "voice")
        ev = conn.execute(
            "SELECT data FROM commai_events WHERE customer_id = %s AND type = 'entitlement.changed'", (b["id"],)
        ).fetchone()
        assert ev["data"]["modules"] == {"voice": False, "automation": False}
        assert conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'commai.entitlements.set' AND customer_id = %s", (b["id"],)
        ).fetchone()
    # Switching it back on restores the routes.
    client.put(f"{u}/entitlements", json={"modules": {"automation": True}}, headers=admin_headers)
    assert client.get(f"{u}/workflows", headers=h).status_code == 200


def test_entitlements_are_per_business(client, admin_headers):
    a = business(client, "Alpha Shop")
    b = business(client, "Beta Shop")
    client.put(f"{base(a)}/entitlements", json={"modules": {"ai_agents": False}}, headers=admin_headers)
    assert client.get(f"{base(a)}/ai/profile", headers=a["agent"]["h"]).status_code == 403
    assert client.get(f"{base(b)}/ai/profile", headers=b["agent"]["h"]).status_code == 200
    # A business can't read another's plan.
    assert client.get(f"{base(a)}/entitlements", headers=b["agent"]["h"]).status_code == 403
