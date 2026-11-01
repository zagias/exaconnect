"""Policy writes stay tenant-bound, read-only for viewers, and serial under contention."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from exaconnect_controller import db, internet
from exaconnect_controller.security import hash_password

from .test_flow import _seed


def test_firewall_and_forward_writes_respect_real_authentication(client, admin_headers):
    cid = _seed()["customer_id"]
    url = f"/api/v1/customers/{cid}"
    rule = client.post(f"{url}/firewall/rules", json={"action": "deny"}, headers=admin_headers).json()
    with db.tx() as conn:
        site = conn.execute(
            "SELECT id FROM sites WHERE customer_id = %s AND kind = 'site' ORDER BY name LIMIT 1", (cid,)
        ).fetchone()["id"]
        other = conn.execute("INSERT INTO customers (name) VALUES ('Other firewall tenant') RETURNING id").fetchone()[
            "id"
        ]
        for name, role, scope in (("viewer", "viewer", None), ("monitor", "owner", ["connect:read"])):
            user = conn.execute(
                "INSERT INTO users (email, password_hash, role, customer_id, access_scopes)"
                " VALUES (%s, %s, 'customer', %s, %s) RETURNING id",
                (f"{name}@firewall.example", hash_password("firewall boundary password"), cid, scope),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO org_memberships (customer_id, user_id, role) VALUES (%s, %s, %s)"
                " ON CONFLICT (customer_id, user_id) DO UPDATE SET role = EXCLUDED.role",
                (cid, user, role),
            )
    for name in ("viewer", "monitor"):
        client.cookies.clear()
        login = client.post(
            "/api/v1/auth/login", json={"email": f"{name}@firewall.example", "password": "firewall boundary password"}
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        assert client.get(f"{url}/internet", headers=headers).status_code == 200
        assert client.get(f"/api/v1/customers/{other}/internet", headers=headers).status_code == 403
        attempts = [
            ("post", f"{url}/firewall/rules", {"action": "allow"}),
            ("patch", f"{url}/firewall/rules/{rule['id']}", {"enabled": False}),
            ("delete", f"{url}/firewall/rules/{rule['id']}", None),
            ("post", f"{url}/firewall/order", {"ids": [rule["id"]]}),
            ("patch", f"{url}/internet/sites/{site}", {"mode": "local"}),
            (
                "post",
                f"{url}/port-forwards",
                {"protocol": "tcp", "port": 8080, "to_site_id": str(site), "to_address": "192.168.10.10"},
            ),
            ("patch", f"{url}/port-forwards/1", {"enabled": False}),
            ("delete", f"{url}/port-forwards/1", None),
        ]
        for method, path, body in attempts:
            kwargs = {"headers": headers}
            if body is not None:
                kwargs["json"] = body
            response = client.request(method, path, **kwargs)
            assert response.status_code == 403, (name, method, path, response.text)
    with db.tx() as conn:
        assert conn.execute("SELECT enabled FROM firewall_rules WHERE id = %s", (rule["id"],)).fetchone()["enabled"]
        assert conn.execute("SELECT count(*) AS n FROM port_forwards").fetchone()["n"] == 0


def test_concurrent_rule_creation_cannot_exceed_limit_or_duplicate_positions(client, monkeypatch):
    cid = _seed()["customer_id"]
    monkeypatch.setattr(internet, "_refresh", lambda *_: None)
    monkeypatch.setattr(internet, "MAX_RULES", 2)
    with db.tx() as conn:
        internet.create_rule(conn, cid, {"action": "deny"}, "test")
    original = internet._rule_ids

    def slow_read(conn, customer):
        ids = original(conn, customer)
        time.sleep(0.05)  # Expose the count/insert race without transaction serialization.
        return ids

    monkeypatch.setattr(internet, "_rule_ids", slow_read)
    start = Barrier(2)

    def create():
        start.wait(timeout=5)
        try:
            with db.tx() as conn:
                internet.create_rule(conn, cid, {"action": "allow"}, "test")
            return "created"
        except internet.InternetError:
            return "limit"

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(create) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ["created", "limit"]
    with db.tx() as conn:
        rows = conn.execute("SELECT position FROM firewall_rules WHERE customer_id = %s ORDER BY position", (cid,))
        assert [r["position"] for r in rows] == [1, 2]


def test_concurrent_public_port_claim_returns_validation_error_not_database_failure(client, monkeypatch):
    cid = _seed()["customer_id"]
    monkeypatch.setattr(internet, "_refresh", lambda *_: None)
    with db.tx() as conn:
        site = conn.execute("SELECT id FROM sites WHERE customer_id = %s AND name = 'site-a'", (cid,)).fetchone()["id"]
    original = internet.validate_forward

    def slow_validate(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.05)  # Both pre-insert uniqueness checks would otherwise succeed.
        return result

    monkeypatch.setattr(internet, "validate_forward", slow_validate)
    start = Barrier(2)

    def create():
        start.wait(timeout=5)
        try:
            with db.tx() as conn:
                internet.create_forward(
                    conn,
                    cid,
                    {"protocol": "tcp", "port": 8080, "to_site_id": str(site), "to_address": "192.168.10.10"},
                    "test",
                )
            return "created"
        except internet.InternetError:
            return "taken"

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(create) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ["created", "taken"]
