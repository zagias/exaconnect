"""Resilient circuits, DDoS protection and the encryption report (ADR 0012)."""

import datetime as dt

from exaconnect_controller import db, encryption
from exaconnect_controller.routing import runner

from .test_fabric import PSK, _cloud
from .test_flow import _enrol, _seed


def _desired(client, h):
    return client.get("/api/v1/agent/desired-state", headers=h).json()


def test_a_resilient_pair_is_two_tunnels_at_the_pop(client, admin_headers):
    seed = _seed()
    cid = seed["customer_id"]
    _, pop_h = _enrol(client, seed["tokens"], "pop-miami")
    url = f"/api/v1/customers/{cid}/circuits"
    r = client.post(url, json=_cloud(secondary_peer_address="100.64.10.2"), headers=admin_headers)
    assert r.status_code == 400 and "differ" in r.json()["detail"]
    r = client.post(url, json=_cloud(secondary_peer_address="100.64.10.3"), headers=admin_headers)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["resilient"] and c["secondary_inside_cidr"] and c["secondary_inside_cidr"] != c["inside_cidr"]
    assert [t["which"] for t in c["tunnels"]] == ["primary", "secondary"]
    azure = client.post(
        url,
        json=_cloud(name="Azure", provider="azure", peer_address="100.64.11.2", cloud_prefixes=["10.101.0.0/16"]),
        headers=admin_headers,
    ).json()
    client.patch(f"/api/v1/customers/{cid}/settings", json={"cloud_to_cloud": True}, headers=admin_headers)

    circuits = {x["id"]: x for x in _desired(client, pop_h)["circuits"]}
    one, two = circuits[c["id"]], circuits[1_000_000 + c["id"]]
    assert (two["name"], two["if_id"], two["remote_address"]) == (
        f"vc{1_000_000 + c['id']}",
        1_000_000 + c["id"],
        "100.64.10.3",
    )
    assert one["psk"] == two["psk"] == PSK and one["inside_address"] != two["inside_address"]
    # Neither twin exports the other; Azure exports both.
    assert one["export_circuits"] == two["export_circuits"] == [azure["id"]]
    assert sorted(circuits[azure["id"]]["export_circuits"]) == [c["id"], 1_000_000 + c["id"]]

    # Telemetry: the secondary tunnel reports under its own id.
    now = dt.datetime.now(dt.UTC).isoformat()
    tel = {
        "at": now,
        "circuits": [
            {"id": c["id"], "name": one["name"], "ike": "down", "bgp": "Active"},
            {
                "id": two["id"],
                "name": two["name"],
                "ike": "up",
                "bgp": "Established",
                "prefixes_received": 1,
                "ike_cipher": "AES_CBC-256/HMAC_SHA1_96/PRF_HMAC_SHA1/MODP_1024",
                "esp_cipher": "AES_CBC-256/HMAC_SHA1_96",
                "established_s": 30,
            },
        ],
    }
    assert client.post("/api/v1/agent/telemetry", json=tel, headers=pop_h).status_code == 204
    view = next(x for x in client.get(url, headers=admin_headers).json() if x["id"] == c["id"])
    assert view["status"] == "up"  # either tunnel
    assert [(t["which"], t["status"]) for t in view["tunnels"]] == [("primary", "down"), ("secondary", "up")]

    rep = client.get(f"/api/v1/customers/{cid}/encryption", headers=admin_headers).json()
    rows = [r for r in rep["circuits"] if r["id"] == c["id"]]
    assert [(r["tunnel"], r["status"]) for r in rows] == [("primary", "down"), ("secondary", "encrypted")]
    assert rows[1]["notes"] == ["Uses SHA-1", "Weak key exchange"] and rows[1]["established_s"] == 30

    # Removing the second tunnel frees its inside addresses.
    r = client.patch(f"{url}/{c['id']}", json={"secondary_peer_address": None}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["resilient"] is False and r.json()["secondary_inside_cidr"] is None
    assert 1_000_000 + c["id"] not in {x["id"] for x in _desired(client, pop_h)["circuits"]}


def test_ddos_protection_and_the_block_list(client, admin_headers):
    seed = _seed()
    cid = seed["customer_id"]
    _, pop_h = _enrol(client, seed["tokens"], "pop-miami")
    prot = _desired(client, pop_h)["internet"]["protection"]
    assert prot == {"enabled": True, "new_per_source": 50, "syn_per_s": 2000, "block_minutes": 10, "blocklist": []}

    r = client.patch("/api/v1/admin/protection", json={"new_per_source": 20, "block_minutes": 5}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["settings"]["new_per_source"] == 20
    for body, code in [({"prefix": "10.0.0.0/8"}, 400), ({"prefix": "nope"}, 400), ({"prefix": "1.0.0.0/4"}, 400)]:
        assert client.post("/api/v1/admin/protection/blocklist", json=body, headers=admin_headers).status_code == code
    r = client.post(
        "/api/v1/admin/protection/blocklist",
        json={"prefix": "203.0.113.66", "reason": "lab", "hours": 1},
        headers=admin_headers,
    )
    assert r.status_code == 201 and r.json()["prefix"] == "203.0.113.66/32"
    bid = r.json()["id"]
    prot = _desired(client, pop_h)["internet"]["protection"]
    assert (prot["new_per_source"], prot["block_minutes"], prot["blocklist"]) == (20, 5, ["203.0.113.66/32"])

    # An expired entry drops out of desired state on the next sweep.
    with db.tx() as conn:
        conn.execute("UPDATE blocked_sources SET expires_at = now() - interval '1 second' WHERE id = %s", (bid,))
    assert runner.lift_expired_blocks() == 1
    assert _desired(client, pop_h)["internet"]["protection"]["blocklist"] == []
    assert runner.lift_expired_blocks() == 0

    r = client.post("/api/v1/admin/protection/blocklist", json={"prefix": "198.51.100.0/25"}, headers=admin_headers)
    assert (
        client.delete(f"/api/v1/admin/protection/blocklist/{r.json()['id']}", headers=admin_headers).status_code == 204
    )

    now = dt.datetime.now(dt.UTC).isoformat()
    counters = [
        {"kind": "flood", "id": 0, "packets": 900, "bytes": 54000},
        {"kind": "syn", "id": 0, "packets": 12, "bytes": 720},
    ]
    tel = {
        "at": now,
        "internet": {
            "mode": "gateway",
            "via": "eth5",
            "counters": counters,
            "auto_blocked": [{"address": "203.0.113.9", "expires_s": 290}],
        },
    }
    assert client.post("/api/v1/agent/telemetry", json=tel, headers=pop_h).status_code == 204
    mine = client.get(f"/api/v1/customers/{cid}/internet", headers=admin_headers).json()["protection"]
    assert mine["dropped"] == {"blocked": 0, "auto": 0, "flood": 900, "syn": 12} and mine["auto_blocked"] == 1
    assert "203.0.113.9" not in str(mine)  # customers see counts only
    admin = client.get("/api/v1/admin/protection", headers=admin_headers).json()
    assert admin["auto_blocked"][0]["address"] == "203.0.113.9"
    # A PoP that stops reporting: its blocks count down from the last report and then go.
    with db.tx() as conn:
        conn.execute("UPDATE internet_state SET updated_at = now() - interval '100 seconds'")
    left = client.get("/api/v1/admin/protection", headers=admin_headers).json()["auto_blocked"][0]["expires_s"]
    assert 185 <= left <= 190
    with db.tx() as conn:
        conn.execute("UPDATE internet_state SET updated_at = now() - interval '10 minutes'")
    assert client.get("/api/v1/admin/protection", headers=admin_headers).json()["auto_blocked"] == []

    client.patch("/api/v1/admin/protection", json={"enabled": False}, headers=admin_headers)
    assert _desired(client, pop_h)["internet"]["protection"]["enabled"] is False
    audit = client.get("/api/v1/audit", headers=admin_headers).text
    assert "protection.block" in audit and "protection.update" in audit


def test_encryption_report_paths(client, admin_headers):
    seed = _seed()
    cid = seed["customer_id"]
    _, a_h = _enrol(client, seed["tokens"], "site-a")
    now = dt.datetime.now(dt.UTC).isoformat()
    tunnels = [
        {"name": "wg-a", "path": "carrier-a", "handshake_age_s": 40, "bfd": "up"},
        {"name": "wg-b", "path": "carrier-b", "handshake_age_s": 400, "bfd": "up"},
        {"name": "wg-sat", "path": "sat", "handshake_age_s": 900, "bfd": "down"},
    ]
    assert client.post("/api/v1/agent/telemetry", json={"at": now, "tunnels": tunnels}, headers=a_h).status_code == 204
    rep = client.get(f"/api/v1/customers/{cid}/encryption", headers=admin_headers).json()
    assert [(p["path"], p["status"]) for p in rep["paths"]] == [
        ("carrier-a", "encrypted"),
        ("carrier-b", "idle"),
        ("sat", "down"),
    ]
    assert rep["summary"] == {"encrypted": 2, "total": 3} and rep["paths"][0]["protocol"] == "WireGuard"
    assert client.get(f"/api/v1/customers/{cid}/encryption").status_code == 401


def test_weak_cipher_notes():
    assert encryption.notes("AES_GCM_16-256/PRF_HMAC_SHA2_256/MODP_2048", "AES_GCM_16-256") == []
    assert encryption.notes("3DES_CBC/HMAC_MD5_96/PRF_HMAC_MD5/MODP_768") == [
        "Uses MD5",
        "Uses DES",
        "Weak key exchange",
    ]
