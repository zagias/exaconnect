"""Renaming customers, deleting links and sites, and open enrolment tokens (admin only)."""

from __future__ import annotations

from exaconnect_controller import db
from exaconnect_controller.security import hash_password

from .test_flow import _csr, _enrol, _seed, _wg


def _site(client, h, name):
    return next(s for s in client.get("/api/v1/sites", headers=h).json() if s["name"] == name)


def _paths(site_id):
    with db.tx() as conn:
        return {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM links WHERE site_id = %s", (site_id,))}


def _customer_headers(client, cid):
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, customer_id) VALUES ('it@demo.example', %s, 'customer', %s)",
            (hash_password("a long customer password"), cid),
        )
    r = client.post("/api/v1/auth/login", json={"email": "it@demo.example", "password": "a long customer password"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_tokens_are_listed_and_cancelled(client, admin_headers):
    _seed()
    site = _site(client, admin_headers, "site-a")
    made = client.post("/api/v1/enrolment-tokens", headers=admin_headers, json={"site_id": site["id"]}).json()
    listed = client.get("/api/v1/enrolment-tokens", headers=admin_headers).json()
    mine = [t for t in listed if t["site"] == "site-a"]
    assert mine and "token" not in mine[0] and "token_hash" not in mine[0]
    newest = mine[0]["id"]
    assert client.delete(f"/api/v1/enrolment-tokens/{newest}", headers=admin_headers).status_code == 204
    assert newest not in {t["id"] for t in client.get("/api/v1/enrolment-tokens", headers=admin_headers).json()}
    # A cancelled token no longer enrols.
    r = client.post(
        "/api/v1/enrol",
        json={"token": made["token"], "node_name": "site-a", "csr_pem": _csr("site-a"), "wg_public_key": _wg()},
    )
    assert r.status_code == 401
    assert client.delete(f"/api/v1/enrolment-tokens/{newest}", headers=admin_headers).status_code == 404


def test_delete_link_and_site_refresh_state_and_protect_billing(client, admin_headers):
    seeded = _seed()
    cid = seeded["customer_id"]
    _, headers = _enrol(client, seeded["tokens"], "site-b")
    site = _site(client, admin_headers, "site-b")
    sat = {"id": str(_paths(site["id"])["sat"])}
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO usage_5m (link_id, bucket, customer_id, carrier_id, in_mbps, out_mbps, seconds)
               SELECT id, date_trunc('hour', now()), customer_id, carrier_id, 5, 5, 300 FROM links WHERE id = %s""",
            (sat["id"],),
        )
        before = conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"]

    # Samples from this billing month protect the link unless forced.
    r = client.delete(f"/api/v1/sites/{site['id']}/links/{sat['id']}", headers=admin_headers)
    assert r.status_code == 409 and "billing month" in r.json()["detail"]
    r = client.delete(f"/api/v1/sites/{site['id']}/links/{sat['id']}?force=true", headers=admin_headers)
    assert r.status_code == 204
    with db.tx() as conn:
        assert conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"] > before
    assert set(_paths(site["id"])) == {"carrier-a", "carrier-b"}

    # Customers can't delete; a missing link is 404.
    cust = _customer_headers(client, cid)
    assert client.delete(f"/api/v1/sites/{site['id']}", headers=cust).status_code == 403
    assert client.delete(f"/api/v1/sites/{site['id']}/links/{sat['id']}", headers=admin_headers).status_code == 404

    assert client.delete(f"/api/v1/sites/{site['id']}", headers=admin_headers).status_code == 204
    assert "site-b" not in {s["name"] for s in client.get("/api/v1/sites", headers=admin_headers).json()}
    assert client.get("/api/v1/agent/desired-state", headers=headers).status_code == 401
    actions = {a["action"] for a in client.get("/api/v1/audit", headers=admin_headers).json()}
    assert {"link.delete", "site.delete"} <= actions


def test_rename_customer(client, admin_headers):
    cid = _seed()["customer_id"]
    r = client.patch(f"/api/v1/customers/{cid}", headers=admin_headers, json={"name": "Kingston Port Authority"})
    assert r.status_code == 200 and r.json()["name"] == "Kingston Port Authority"
    other = client.post("/api/v1/customers", headers=admin_headers, json={"name": "Other"}).json()["id"]
    assert (
        client.patch(
            f"/api/v1/customers/{other}", headers=admin_headers, json={"name": "Kingston Port Authority"}
        ).status_code
        == 409
    )
    assert (
        client.patch(f"/api/v1/customers/{cid}", headers=_customer_headers(client, cid), json={"name": "x"}).status_code
        == 403
    )
