"""End-to-end controller flow against Postgres: seed, enrol, desired state,
agent auth through the proxy headers, telemetry, portal views."""

import base64
import datetime as dt

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from cryptography.x509.oid import NameOID

from exaconnect_controller import db, pki
from exaconnect_controller.seed import seed_lab

from .conftest import PROXY_SECRET


def _csr(name: str) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def _wg() -> str:
    k = x25519.X25519PrivateKey.generate().public_key()
    return base64.b64encode(k.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _seed(sat: str = "leo") -> dict:
    with db.tx() as conn:
        return seed_lab(conn, sat)


def _enrol(client, tokens, name):
    r = client.post(
        "/api/v1/enrol",
        json={"token": tokens[name], "node_name": name, "csr_pem": _csr(name), "wg_public_key": _wg()},
    )
    assert r.status_code == 200, r.text
    cert = x509.load_pem_x509_certificate(r.json()["cert_pem"].encode())
    return r.json(), {
        "X-Exa-Proxy": PROXY_SECRET,
        "X-SSL-Client-Verify": "SUCCESS",
        "X-SSL-Client-Serial": format(cert.serial_number, "X"),
    }


def test_login(client):
    assert client.post("/api/v1/auth/login", json={"email": "admin@example.org", "password": "nope"}).status_code == 401
    assert client.get("/api/v1/overview").status_code == 401


def test_enrolment_and_desired_state(client, admin_headers):
    tokens = _seed()["tokens"]

    # Tokens are bound to a site and single use.
    bad = client.post(
        "/api/v1/enrol",
        json={"token": tokens["site-a"], "node_name": "site-b", "csr_pem": _csr("site-b"), "wg_public_key": _wg()},
    )
    assert bad.status_code == 400
    pop, pop_h = _enrol(client, tokens, "pop-miami")
    site_a, a_h = _enrol(client, tokens, "site-a")
    again = client.post(
        "/api/v1/enrol",
        json={"token": tokens["site-a"], "node_name": "site-a", "csr_pem": _csr("site-a"), "wg_public_key": _wg()},
    )
    assert again.status_code == 401

    # The certificate is signed by the controller CA for the node name.
    ca = x509.load_pem_x509_certificate(pop["ca_pem"].encode())
    cert = x509.load_pem_x509_certificate(site_a["cert_pem"].encode())
    cert.verify_directly_issued_by(ca)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "site-a"

    # Agent endpoints need the proxy secret and a verified client certificate.
    assert client.get("/api/v1/agent/desired-state").status_code == 403
    assert (
        client.get("/api/v1/agent/desired-state", headers={**a_h, "X-SSL-Client-Verify": "FAILED"}).status_code == 401
    )
    assert client.get("/api/v1/agent/desired-state", headers={**a_h, "X-SSL-Client-Serial": "DEAD"}).status_code == 401

    ds = client.get("/api/v1/agent/desired-state", headers=a_h).json()
    assert ds["role"] == "site" and ds["asn"] == 65001 and ds["router_id"] == "100.64.1.11"
    wg_a = next(t for t in ds["tunnels"] if t["name"] == "wg-a")
    assert wg_a["address"] == "100.64.1.11/24"
    assert wg_a["underlay_interface"] == "eth1"
    assert wg_a["peers"][0]["endpoint"] == "10.11.0.2:51820"
    assert wg_a["bgp_neighbors"][0] == {
        "address": "100.64.1.1",
        "asn": 65000,
        "bfd_profile": "terrestrial",
        "local_pref": 200,
    }
    assert wg_a["probe"]["target"] == "100.64.1.1:7000"
    sat = next(t for t in ds["tunnels"] if t["name"] == "wg-sat")
    assert sat["bgp_neighbors"][0]["bfd_profile"] == "satellite"
    assert sat["bgp_neighbors"][0]["local_pref"] < wg_a["bgp_neighbors"][0]["local_pref"]

    # Unchanged version -> 204.
    assert client.get(f"/api/v1/agent/desired-state?have={ds['version']}", headers=a_h).status_code == 204

    # The PoP sees site-a as a peer on every path; site-b enrolling bumps the PoP, not site-a.
    pop_ds = client.get("/api/v1/agent/desired-state", headers=pop_h).json()
    pop_a = next(t for t in pop_ds["tunnels"] if t["name"] == "wg-a")
    assert pop_a["listen_port"] == 51820
    assert pop_a["peers"][0]["allowed_ips"] == [
        "100.64.1.11/32",
        "10.254.0.11/32",
        "192.168.10.0/24",
    ]  # overlay, loopback, LAN
    assert pop_ds["reflector"] == {"listen": ":7000"}
    _enrol(client, tokens, "site-b")
    assert client.get(f"/api/v1/agent/desired-state?have={ds['version']}", headers=a_h).status_code == 204
    pop_v2 = client.get(f"/api/v1/agent/desired-state?have={pop_ds['version']}", headers=pop_h).json()
    assert pop_v2["version"] == pop_ds["version"] + 1
    assert len(next(t for t in pop_v2["tunnels"] if t["name"] == "wg-a")["peers"]) == 2

    # Status is recorded and audited.
    r = client.post(
        "/api/v1/agent/status", headers=a_h, json={"applied_version": ds["version"], "ok": True, "agent_version": "t"}
    )
    assert r.status_code == 204
    nodes = {n["name"]: n for n in client.get("/api/v1/nodes", headers=admin_headers).json()}
    assert nodes["site-a"]["applied_version"] == ds["version"]
    actions = {a["action"] for a in client.get("/api/v1/audit", headers=admin_headers).json()}
    assert {"enrol", "desired_state.applied", "site.upsert", "enrolment_token.issue"} <= actions


def test_telemetry_reaches_portal_views(client, admin_headers):
    tokens = _seed()["tokens"]
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    now = dt.datetime.now(dt.UTC)

    def window(path, rtt, loss, sent=10):
        rec = round(sent * (1 - loss / 100))
        return {
            "path": path,
            "start": (now - dt.timedelta(seconds=10)).isoformat(),
            "end": now.isoformat(),
            "sent": sent,
            "received": rec,
            "loss_pct": loss,
            "rtt_avg_ms": rtt,
            "rtt_min_ms": rtt,
            "rtt_max_ms": rtt,
            "jitter_ms": 2.0,
        }

    body = {
        "at": now.isoformat(),
        "probes": [window("wg-a", 25.0, 0), window("wg-b", 140.0, 0), window("wg-sat", 0, 100)],
        "counters": [{"at": now.isoformat(), "ifname": "eth1", "rx_bytes": 1000, "tx_bytes": 2000}],
        "events": [{"at": now.isoformat(), "kind": "bfd_down", "detail": {"peer": "100.64.3.1", "tunnel": "wg-sat"}}],
        "tunnels": [{"name": "wg-a", "path": "carrier-a", "handshake_age_s": 4, "bfd": "up"}],
    }
    r = client.post("/api/v1/agent/telemetry", headers=a_h, json=body)
    assert r.status_code == 204, r.text

    ov = client.get("/api/v1/overview", headers=admin_headers).json()
    site_a = next(s for s in ov["sites"] if s["name"] == "site-a")
    health = {p["path"]: p["health"] for p in site_a["paths"]}
    assert health == {"carrier-a": "ok", "carrier-b": "warn", "sat": "bad"}
    assert "Carrier B at site-a" in ov["attention"]
    # The PoP has no probes of its own, so its paths never raise attention.
    assert all("at pop-miami" not in a for a in ov["attention"])

    detail = client.get(f"/api/v1/sites/{site_a['id']}", headers=admin_headers).json()
    assert detail["online"] is True
    assert detail["tunnels"][0]["bfd"] == "up"
    assert {s["class_name"] for s in detail["slas"]} == {"voice", "business", "bulk"}

    pts = client.get(f"/api/v1/sites/{site_a['id']}/metrics?minutes=5", headers=admin_headers).json()["points"]
    assert {p["path"] for p in pts} == {"carrier-a", "carrier-b", "sat"}

    ev = client.get("/api/v1/events", headers=admin_headers).json()
    assert ev[0]["kind"] == "bfd_down" and ev[0]["node"] == "site-a"


def test_serial_helper_matches_issued_cert(tmp_path):
    ca = pki.load_or_create(str(tmp_path), ["controller"])
    pem, serial = ca.sign_client(_csr("x").encode(), "x")
    cert = x509.load_pem_x509_certificate(pem)
    assert pki.normalise_serial(format(cert.serial_number, "x")) == serial
