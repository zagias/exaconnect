"""Zero-touch installation (ADR 0050): a box calls home with its serial number and
key, waits until ExaCarib staff assign it, then gets what it needs to join."""

import base64
import datetime as dt

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from exaconnect_controller import db

from .conftest import PROXY_SECRET
from .test_flow import _csr, _seed, _wg
from .test_organisation_setup import _join, _new_org


def _hello(client, key, serial="EXA-0001", at=None):
    at = at or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    sig = base64.b64encode(key.sign(f"exacarib-ztp-v1\n{serial}\n{at}".encode())).decode()
    return client.post(
        "/api/v1/ztp/hello",
        json={"serial": serial, "public_key": pub, "at": at, "signature": sig, "mac": "52:54:00:12:34:56"},
    )


def test_zero_touch(client, admin_headers):
    _seed()
    key = Ed25519PrivateKey.generate()
    with db.tx() as conn:
        site = conn.execute("SELECT id, name FROM sites WHERE name = 'site-b'").fetchone()
    site_id, site_name = str(site["id"]), site["name"]

    # A new box calls home: listed for staff, told to wait.
    r = _hello(client, key)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "waiting"
    boxes = client.get("/api/v1/admin/boxes", headers=admin_headers).json()
    b = next(x for x in boxes if x["serial"] == "EXA-0001")
    assert b["state"] == "waiting" and b["key_id"] and b["mac"] == "52:54:00:12:34:56"

    # A wrong signature, an old time, or another key with the same serial is refused.
    bad = _hello(client, key, at="2020-01-01T00:00:00Z")
    assert bad.status_code == 403 and "clock" in bad.json()["detail"]
    r = client.post("/api/v1/ztp/hello", json={**_hello_body(key), "signature": "A" * 88})
    assert r.status_code == 403
    other = Ed25519PrivateKey.generate()
    r = _hello(client, other)
    assert r.status_code == 403 and "Another box" in r.json()["detail"]
    b = next(x for x in client.get("/api/v1/admin/boxes", headers=admin_headers).json() if x["serial"] == "EXA-0001")
    assert b["state"] == "conflict"
    # Staff release the serial; the next box to call is trusted (here, the real one again).
    assert client.post("/api/v1/admin/boxes/EXA-0001/release", headers=admin_headers).status_code == 200
    assert _hello(client, key).json()["state"] == "waiting"

    # Only staff see or change the list.
    org = _new_org(client, admin_headers)
    _, owner = _join(client, org["invite"]["url"])
    assert client.get("/api/v1/admin/boxes", headers=owner).status_code == 403
    assert client.patch("/api/v1/admin/boxes/EXA-0001", json={"site_id": site_id}, headers=owner).status_code == 403

    # Assigned to a site: the next call carries a token, and the box enrols with it.
    r = client.patch("/api/v1/admin/boxes/exa-0001", json={"site_id": site_id}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["state"] == "assigned" and r.json()["site"] == site_name
    ans = _hello(client, key).json()
    assert ans["state"] == "assigned" and ans["site"] == site_name and ans["token"]
    assert len(ans["ca_fingerprint"]) == 64 and ans["controller"].endswith(":8443")
    # No second token within the minute.
    assert _hello(client, key).json()["state"] == "waiting"
    e = client.post(
        "/api/v1/enrol",
        json={"token": ans["token"], "node_name": site_name, "csr_pem": _csr(site_name), "wg_public_key": _wg()},
    )
    assert e.status_code == 200, e.text

    # Its first telemetry links the box to its node: online in the list.
    cert = x509.load_pem_x509_certificate(e.json()["cert_pem"].encode())
    h = {
        "X-Exa-Proxy": PROXY_SECRET,
        "X-SSL-Client-Verify": "SUCCESS",
        "X-SSL-Client-Serial": format(cert.serial_number, "X"),
    }
    now = dt.datetime.now(dt.UTC).isoformat()
    r = client.post(
        "/api/v1/agent/telemetry", headers=h, json={"at": now, "box": {"console": True, "serial": "EXA-0001"}}
    )
    assert r.status_code == 204, r.text
    b = next(x for x in client.get("/api/v1/admin/boxes", headers=admin_headers).json() if x["serial"] == "EXA-0001")
    assert b["state"] == "online" and b["node_id"] and b["joined_at"]

    # Boxes added by hand before they call home; one waiting box per site.
    r = client.post("/api/v1/admin/boxes", json={"serial": "exa 0002", "note": "Kingston spare"}, headers=admin_headers)
    assert r.status_code == 201 and r.json()["serial"] == "EXA-0002" and r.json()["state"] == "waiting"
    assert client.post("/api/v1/admin/boxes", json={"serial": "EXA-0002"}, headers=admin_headers).status_code == 409
    with db.tx() as conn:
        site_a = str(conn.execute("SELECT id FROM sites WHERE name = 'site-a'").fetchone()["id"])
    assert (
        client.patch("/api/v1/admin/boxes/EXA-0002", json={"site_id": site_a}, headers=admin_headers).status_code == 200
    )
    r = client.post("/api/v1/admin/boxes", json={"serial": "EXA-0003", "site_id": site_a}, headers=admin_headers)
    assert r.status_code == 409
    # Unassigned: told to wait again; deleted: gone.
    client.patch("/api/v1/admin/boxes/EXA-0002", json={"site_id": ""}, headers=admin_headers)
    k2 = Ed25519PrivateKey.generate()
    assert _hello(client, k2, "EXA-0002").json()["state"] == "waiting"
    assert client.delete("/api/v1/admin/boxes/EXA-0002", headers=admin_headers).status_code == 204
    assert all(x["serial"] != "EXA-0002" for x in client.get("/api/v1/admin/boxes", headers=admin_headers).json())
    with db.tx() as conn:
        actions = {
            r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'box.%'").fetchall()
        }
    assert {"box.assign", "box.unassign", "box.release", "box.add", "box.delete", "box.key_conflict"} <= actions


def _hello_body(key, serial="EXA-0001"):
    at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return {"serial": serial, "public_key": pub, "at": at, "signature": ""}


def test_assignable_sites(client, admin_headers):
    _seed()
    rows = client.get("/api/v1/admin/boxes/sites", headers=admin_headers).json()
    assert {"site-a", "site-b"} <= {r["name"] for r in rows} and all("organisation" in r for r in rows)
