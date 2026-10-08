"""Admin screens' API: classes and SLA policies, users, password change,
sign-in throttling."""

from .test_flow import _enrol, _seed


def test_classes_and_sla(client, admin_headers):
    seed = _seed()
    cid = seed["customer_id"]
    _, a_h = _enrol(client, seed["tokens"], "site-a")
    m0 = client.get("/api/v1/agent/steering", headers=a_h).json()
    body = {
        "description": "Video conferencing",
        "dscp": [34],
        "ports": "udp:3478-3481",
        "subnets": ["52.112.0.0/14"],
        "ordinal": 2,
        "sla": {"max_latency_ms": 200, "max_jitter_ms": 40, "max_loss_pct": 1.5, "allow_satellite": True},
    }
    r = client.put(f"/api/v1/customers/{cid}/classes/video", headers=admin_headers, json=body)
    assert r.status_code == 200, r.text
    classes = {c["name"]: c for c in client.get("/api/v1/classes", headers=admin_headers).json()}
    assert classes["video"]["subnets"] == ["52.112.0.0/14"] and float(classes["video"]["max_loss_pct"]) == 1.5
    m = client.get(f"/api/v1/agent/steering?have={m0['version']}", headers=a_h).json()
    assert "video" in [c["name"] for c in m["classes"]]

    bad = {**body, "ports": "3478"}
    assert client.put(f"/api/v1/customers/{cid}/classes/video", headers=admin_headers, json=bad).status_code == 422
    assert client.put(f"/api/v1/customers/{cid}/classes/Bad_Name", headers=admin_headers, json=body).status_code == 400
    assert client.delete(f"/api/v1/customers/{cid}/classes/video", headers=admin_headers).status_code == 204
    names = {c["name"] for c in client.get("/api/v1/classes", headers=admin_headers).json()}
    assert names == {"voice", "business", "bulk"}

    inv = client.get(f"/api/v1/inventory?customer_id={cid}", headers=admin_headers).json()
    site_a = next(s for s in inv["sites"] if s["name"] == "site-a")
    assert [lk["path"] for lk in site_a["links"]] == ["carrier-a", "carrier-b", "sat"]
    assert site_a["links"][0]["underlay_ip"] == "10.11.1.2/24"

    # Bulk on satellite in Storm Mode is an admin setting.
    r = client.patch(f"/api/v1/customers/{cid}/settings", headers=admin_headers, json={"storm_allow_bulk_sat": True})
    assert r.status_code == 200 and r.json()["storm_allow_bulk_sat"] is True


def test_users_passwords_and_throttle(client, admin_headers):
    seed = _seed()
    r = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"email": "ops@demo.example", "role": "customer", "customer_id": seed["customer_id"]},
    )
    assert r.status_code == 201, r.text
    one_time = r.json()["password"]
    assert len(one_time) >= 16
    dup = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"email": "OPS@demo.example", "role": "customer", "customer_id": seed["customer_id"]},
    )
    assert dup.status_code == 409
    assert (
        client.post("/api/v1/users", headers=admin_headers, json={"email": "x@y.z", "role": "carrier"}).status_code
        == 400
    )

    login = client.post("/api/v1/auth/login", json={"email": "ops@demo.example", "password": one_time})
    c_h = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/api/v1/users", headers=c_h).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/password", headers=c_h, json={"current": "wrong", "new": "a much longer password"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/v1/auth/password", headers=c_h, json={"current": one_time, "new": "short"}).status_code == 422
    )
    assert (
        client.post(
            "/api/v1/auth/password", headers=c_h, json={"current": one_time, "new": "a much longer password"}
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "ops@demo.example", "password": "a much longer password"}
        ).status_code
        == 200
    )

    users = {u["email"]: u for u in client.get("/api/v1/users", headers=admin_headers).json()}
    uid = users["ops@demo.example"]["id"]
    reset = client.post(f"/api/v1/users/{uid}/reset-password", headers=admin_headers).json()
    assert (
        client.post("/api/v1/auth/login", json={"email": "ops@demo.example", "password": reset["password"]}).status_code
        == 200
    )
    admin_id = users["admin@example.org"]["id"]
    assert client.delete(f"/api/v1/users/{admin_id}", headers=admin_headers).status_code == 400
    assert client.delete(f"/api/v1/users/{uid}", headers=admin_headers).status_code == 204

    # Ten failures for one email lock further attempts, even with the right password.
    for _ in range(10):
        assert (
            client.post("/api/v1/auth/login", json={"email": "admin@example.org", "password": "nope"}).status_code
            == 401
        )
    locked = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.org", "password": "correct horse battery staple"}
    )
    assert locked.status_code == 429
