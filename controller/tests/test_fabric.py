"""Virtual circuits and the cloud router (ADR 0009)."""

import datetime as dt
from decimal import Decimal

from exaconnect_controller import db, fabric

from .test_flow import _enrol, _seed

PSK = "lab.only.key_123"


def _cloud(**kw):
    body = {
        "name": "AWS us-east-1",
        "kind": "cloud",
        "provider": "aws",
        "region": "us-east-1",
        "peer_address": "100.64.10.2",
        "peer_asn": 64512,
        "psk": PSK,
        "cloud_prefixes": ["10.100.0.0/16"],
        "bandwidth_mbps": 50,
    }
    return {**body, **kw}


def _sites(seed):
    with db.tx() as conn:
        return {
            r["name"]: str(r["id"])
            for r in conn.execute("SELECT id, name FROM sites WHERE customer_id = %s", (seed["customer_id"],))
        }


def test_validation_says_what_is_wrong(client, admin_headers):
    seed = _seed()
    cid, sites = seed["customer_id"], _sites(seed)
    url = f"/api/v1/customers/{cid}/circuits"
    for body, words in [
        (_cloud(provider="nope"), "cloud provider"),
        (_cloud(peer_address="not-an-ip"), "public address"),
        (_cloud(psk=None), "pre-shared key"),
        (_cloud(psk="short"), "8 to 64"),
        (_cloud(a_prefixes=["172.16.0.0/24"]), "not inside any of your sites"),
        (_cloud(inside_cidr="169.254.10.0/29"), "/30"),
        (
            {
                "name": "x",
                "kind": "site",
                "a_site_id": sites["site-a"],
                "b_site_id": sites["site-a"],
                "a_vlan": 100,
                "bandwidth_mbps": 10,
            },
            "two different sites",
        ),
        (
            {
                "name": "x",
                "kind": "site",
                "a_site_id": sites["site-a"],
                "b_site_id": sites["site-b"],
                "bandwidth_mbps": 10,
            },
            "VLAN",
        ),
        (
            {
                "name": "x",
                "kind": "site",
                "a_site_id": sites["pop-miami"],
                "b_site_id": sites["site-b"],
                "a_vlan": 100,
                "bandwidth_mbps": 10,
            },
            "this organisation's sites",
        ),
    ]:
        r = client.post(url, json=body, headers=admin_headers)
        assert r.status_code == 400 and words in r.json()["detail"], (body, r.text)
    assert client.post(url, json=_cloud(bandwidth_mbps=5000), headers=admin_headers).status_code == 422


def test_cloud_circuit_reaches_the_pop_and_never_shows_its_key(client, admin_headers):
    seed = _seed()
    cid, tokens = seed["customer_id"], seed["tokens"]
    _, pop_h = _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")

    r = client.post(f"/api/v1/customers/{cid}/circuits", json=_cloud(class_name="business"), headers=admin_headers)
    assert r.status_code == 201, r.text
    c = r.json()
    assert "psk" not in c and c["has_psk"] is True and c["status"] == "provisioning"
    assert (c["inside_cidr"], c["our_inside"], c["cloud_inside"]) == (
        "169.254.100.0/30",
        "169.254.100.2/30",
        "169.254.100.1",
    )
    listed = client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).text
    audit = client.get("/api/v1/audit", headers=admin_headers).text
    assert PSK not in listed and PSK not in audit and "circuit.create" in audit

    pop = client.get("/api/v1/agent/desired-state", headers=pop_h).json()
    (vc,) = pop["circuits"]
    assert vc["name"] == f"vc{c['id']}" and vc["if_id"] == c["id"] and vc["psk"] == PSK
    assert vc["underlay_interface"] == "eth5" and vc["local_address"] == "100.64.0.2"
    assert vc["inside_address"] == "169.254.100.2/30" and vc["peer_inside"] == "169.254.100.1"
    assert vc["import_prefixes"] == ["10.100.0.0/16"] and vc["peer_asn"] == 64512
    assert vc["export_prefixes"] == ["192.168.10.0/24", "192.168.20.0/24"] and vc["shape_kbit"] == 50000
    assert pop["loopback"] == "10.254.0.1/32"

    # Traffic for the cloud goes in the circuit's class at the sites.
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert {"class": "business", "dst": ["10.100.0.0/16"]} in m["matches"]

    # Switched off: gone from the PoP; the key survives a change that doesn't mention it.
    url = f"/api/v1/customers/{cid}/circuits/{c['id']}"
    assert client.patch(url, json={"enabled": False}, headers=admin_headers).json()["status"] == "off"
    assert "circuits" not in client.get("/api/v1/agent/desired-state", headers=pop_h).json()
    client.patch(url, json={"enabled": True}, headers=admin_headers)
    assert client.get("/api/v1/agent/desired-state", headers=pop_h).json()["circuits"][0]["psk"] == PSK


def test_cloud_router_exports_each_cloud_to_the_others(client, admin_headers):
    seed = _seed()
    cid, tokens = seed["customer_id"], seed["tokens"]
    _, pop_h = _enrol(client, tokens, "pop-miami")
    url = f"/api/v1/customers/{cid}/circuits"
    aws = client.post(url, json=_cloud(), headers=admin_headers).json()
    azure = client.post(
        url,
        json=_cloud(
            name="Azure", provider="azure", peer_address="100.64.11.2", peer_asn=65515, cloud_prefixes=["10.101.0.0/16"]
        ),
        headers=admin_headers,
    ).json()
    assert azure["inside_cidr"] == "169.254.100.4/30"
    circuits = {c["id"]: c for c in client.get("/api/v1/agent/desired-state", headers=pop_h).json()["circuits"]}
    assert circuits[aws["id"]]["export_circuits"] == [azure["id"]]
    assert circuits[azure["id"]]["export_circuits"] == [aws["id"]]
    client.patch(f"/api/v1/customers/{cid}/settings", json={"cloud_to_cloud": False}, headers=admin_headers)
    circuits = client.get("/api/v1/agent/desired-state", headers=pop_h).json()["circuits"]
    assert all(c["export_circuits"] == [] for c in circuits)
    assert client.get(f"/api/v1/customers/{cid}/settings", headers=admin_headers).json()["cloud_to_cloud"] is False


def test_site_circuit_bridges_a_vlan_and_reports_status(client, admin_headers):
    seed = _seed()
    cid, tokens, sites = seed["customer_id"], seed["tokens"], _sites(seed)
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    _, b_h = _enrol(client, tokens, "site-b")
    body = {
        "name": "Kingston to Port of Spain",
        "kind": "site",
        "a_site_id": sites["site-a"],
        "b_site_id": sites["site-b"],
        "a_vlan": 100,
        "b_vlan": 200,
        "bandwidth_mbps": 20,
    }
    c = client.post(f"/api/v1/customers/{cid}/circuits", json=body, headers=admin_headers).json()
    a = client.get("/api/v1/agent/desired-state", headers=a_h).json()
    b = client.get("/api/v1/agent/desired-state", headers=b_h).json()
    (la,), (lb,) = a["l2_circuits"], b["l2_circuits"]
    assert la == {
        "id": c["id"],
        "name": f"vx{c['id']}",
        "vni": 10000 + c["id"],
        "vlan": 100,
        "parent": "eth4",
        "remote": "10.254.0.12",
        "shape_kbit": 20000,
        "mtu": 1370,
        "probe": {"target": "10.254.0.12:7000", "interval_ms": 1000},
    }
    assert lb["vlan"] == 200 and lb["remote"] == "10.254.0.11"
    assert a["reflector"] == {"listen": "10.254.0.11:7000"} and a["loopback"] == "10.254.0.11/32"

    # The agent reports it; another customer's circuit id is ignored.
    now = dt.datetime.now(dt.UTC)
    for i in range(3):
        t = now - dt.timedelta(seconds=20 - 10 * i)
        tel = {
            "at": t.isoformat(),
            "circuits": [
                {
                    "id": c["id"],
                    "name": la["name"],
                    "sent": 10,
                    "received": 9,
                    "rtt_ms": 52.0,
                    "bytes_in": 1_250_000 * i,
                    "bytes_out": 0,
                },
                {"id": 999999, "name": "vx999999"},
            ],
        }
        assert client.post("/api/v1/agent/telemetry", json=tel, headers=a_h).status_code == 204
    (v,) = client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json()
    assert v["status"] == "up" and v["rtt_ms"] == 52.0 and v["loss_pct"] == 10.0
    assert v["mbps_in"] == 1.0  # 2.5 MB over 20 s
    pts = client.get(f"/api/v1/customers/{cid}/circuits/{c['id']}/metrics", headers=admin_headers).json()
    assert sum(p["sent"] for p in pts) == 30


def test_cloud_status_follows_ike_and_bgp(client, admin_headers):
    seed = _seed()
    cid, tokens = seed["customer_id"], seed["tokens"]
    _, pop_h = _enrol(client, tokens, "pop-miami")
    c = client.post(f"/api/v1/customers/{cid}/circuits", json=_cloud(), headers=admin_headers).json()
    at = dt.datetime.now(dt.UTC).isoformat()

    def report(ike, bgp):
        tel = {
            "at": at,
            "circuits": [{"id": c["id"], "ike": ike, "bgp": bgp, "prefixes_received": 1, "routes": ["10.100.0.0/16"]}],
        }
        client.post("/api/v1/agent/telemetry", json=tel, headers=pop_h)
        return client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json()[0]

    assert report("connecting", "")["status"] == "provisioning"
    assert report("up", "Active")["status"] == "down"
    v = report("up", "Established")
    assert v["status"] == "up" and v["routes"] == ["10.100.0.0/16"] and v["prefixes_received"] == 1


def test_elastic_bandwidth_is_billed_by_the_hour(client, admin_headers):
    # Hand-worked: 100 Mbps for 10 h, then 200 Mbps for 5 h, at 2.00 per Mbps per
    # 730-hour month: 100 x 10 x 2 / 730 = 2.74, 200 x 5 x 2 / 730 = 2.74; total 5.48.
    seed = _seed()
    cid = seed["customer_id"]
    c = client.post(f"/api/v1/customers/{cid}/circuits", json=_cloud(bandwidth_mbps=100), headers=admin_headers)
    c = c.json()
    t0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    with db.tx() as conn:
        conn.execute("DELETE FROM circuit_bandwidth WHERE circuit_id = %s", (c["id"],))
        conn.execute(
            """INSERT INTO circuit_bandwidth (circuit_id, mbps, valid_from, valid_to, changed_by)
               VALUES (%s, 100, %s, %s, 't'), (%s, 200, %s, %s, 't')""",
            (
                c["id"],
                t0,
                t0 + dt.timedelta(hours=10),
                c["id"],
                t0 + dt.timedelta(hours=10),
                t0 + dt.timedelta(hours=15),
            ),
        )
        row = conn.execute("SELECT * FROM circuits WHERE id = %s", (c["id"],)).fetchone()
        ch = fabric.charges(conn, row, t0, t0 + dt.timedelta(days=30))
    assert [s["amount"] for s in ch["segments"]] == [2.74, 2.74] and ch["total"] == 5.48
    assert ch["price_per_mbps_month"] == float(Decimal("2.0"))
    r = client.get(f"/api/v1/customers/{cid}/circuits/{c['id']}/charges?month=2026-09", headers=admin_headers)
    assert r.json()["total"] == 5.48

    # A change through the API closes the old speed and opens the new one.
    url = f"/api/v1/customers/{cid}/circuits/{c['id']}"
    assert client.patch(url, json={"bandwidth_mbps": 300}, headers=admin_headers).json()["bandwidth_mbps"] == 300
    with db.tx() as conn:
        open_rows = conn.execute(
            "SELECT mbps FROM circuit_bandwidth WHERE circuit_id = %s AND valid_to IS NULL", (c["id"],)
        ).fetchall()
    assert [r["mbps"] for r in open_rows] == [300]

    # Deleted: off the list, billing stops, the history stays.
    assert client.delete(url, headers=admin_headers).status_code == 204
    assert client.get(f"/api/v1/customers/{cid}/circuits", headers=admin_headers).json() == []
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM circuit_bandwidth WHERE circuit_id = %s AND valid_to IS NULL", (c["id"],)
            ).fetchone()["n"]
            == 0
        )


def test_empty_optional_fields_mean_none(client, admin_headers):
    # Tools that send "" for "not set" (Terraform, forms) get a circuit, not a 500.
    cid = _seed()["customer_id"]
    r = client.post(
        f"/api/v1/customers/{cid}/circuits",
        json=_cloud(a_site_id="", class_name="", inside_cidr=""),
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["inside_cidr"] == "169.254.100.0/30"
    r = client.patch(f"/api/v1/customers/{cid}/circuits/{c['id']}", json={"class_name": ""}, headers=admin_headers)
    assert r.status_code == 200, r.text
    with db.tx() as conn:
        row = conn.execute("SELECT a_site_id, class_name FROM circuits WHERE id = %s", (c["id"],)).fetchone()
    assert row["a_site_id"] is None and row["class_name"] is None
