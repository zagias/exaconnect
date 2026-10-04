"""API keys for automation (ADR 0013)."""

from exaconnect_controller import db

from .test_flow import _seed


def test_api_keys_act_as_their_owner(client, admin_headers):
    seed = _seed()
    r = client.post("/api/v1/auth/api-keys", json={"name": "terraform", "days": 30}, headers=admin_headers)
    assert r.status_code == 201, r.text
    key = r.json()
    token = key["token"]
    assert token.startswith("exa_") and key["prefix"] == token[:12] and key["expires_at"]
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/me", headers=h).json()["role"] == "admin"
    assert client.get(f"/api/v1/customers/{seed['customer_id']}/circuits", headers=h).status_code == 200

    (listed,) = client.get("/api/v1/auth/api-keys", headers=admin_headers).json()
    assert "token" not in listed and listed["last_used_at"] is not None
    with db.tx() as conn:
        stored = conn.execute("SELECT token_hash FROM api_keys").fetchone()["token_hash"]
        audit = conn.execute("SELECT string_agg(detail::text || target, ' ') AS a FROM audit_log").fetchone()["a"]
    assert token not in stored and token not in audit

    assert client.delete(f"/api/v1/auth/api-keys/{key['id']}", headers=admin_headers).status_code == 204
    r = client.get("/api/v1/auth/me", headers=h)
    assert r.status_code == 401 and "revoked" in r.json()["detail"]
    assert client.get("/api/v1/auth/api-keys", headers=admin_headers).json() == []
    assert client.delete(f"/api/v1/auth/api-keys/{key['id']}", headers=admin_headers).status_code == 404


def test_expired_and_unknown_keys(client, admin_headers):
    _seed()
    token = client.post("/api/v1/auth/api-keys", json={"name": "old"}, headers=admin_headers).json()["token"]
    with db.tx() as conn:
        conn.execute("UPDATE api_keys SET expires_at = now() - interval '1 second'")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer exa_nope"})
    assert r.status_code == 401
    r = client.post("/api/v1/auth/api-keys", json={"name": ""}, headers=admin_headers)
    assert r.status_code == 422
