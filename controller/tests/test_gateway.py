"""The agent gateway on the internet (ADR 0024): the public name on the certificate, and in enrolment details."""

from __future__ import annotations

from dataclasses import replace

from cryptography import x509
from cryptography.x509.oid import NameOID

from exaconnect_controller import pki
from exaconnect_controller.settings import Settings

from .test_flow import _csr, _enrol, _seed


def _names(data_dir) -> set[str]:
    cert = x509.load_pem_x509_certificate((data_dir / "tls" / "server.crt").read_bytes())
    return {str(n.value) for n in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value}


def test_public_name_joins_the_certificate_and_reissues_only_when_new(tmp_path):
    lab = Settings(data_dir=str(tmp_path), tls_sans="controller,172.30.0.5")
    ca = pki.load_or_create(lab.data_dir, lab.tls_names())
    assert _names(tmp_path) == {"controller", "172.30.0.5"}
    first = (tmp_path / "tls" / "server.crt").read_bytes()

    # Same names: the certificate is kept.
    pki.load_or_create(lab.data_dir, lab.tls_names())
    assert (tmp_path / "tls" / "server.crt").read_bytes() == first

    # A public gateway name: reissued by the same CA, so enrolled agents keep trusting it.
    public = replace(lab, agent_public_host="connect.example.org")
    assert public.agent_public_url == "https://connect.example.org:8443"
    again = pki.load_or_create(public.data_dir, public.tls_names())
    assert again.fingerprint == ca.fingerprint
    assert _names(tmp_path) == {"controller", "172.30.0.5", "connect.example.org"}


def test_enrolment_token_carries_the_public_gateway(client, admin_headers):
    _seed()
    sites = client.get("/api/v1/sites", headers=admin_headers).json()
    site_id = next(s["id"] for s in sites if s["name"] == "site-a")

    r = client.post("/api/v1/enrolment-tokens", headers=admin_headers, json={"site_id": site_id})
    assert r.status_code == 201 and r.json()["public_agent_url"] == ""

    client.app.state.settings = replace(client.app.state.settings, agent_public_host="connect.example.org")
    r = client.post("/api/v1/enrolment-tokens", headers=admin_headers, json={"site_id": site_id})
    assert r.json()["public_agent_url"] == "https://connect.example.org:8443"


def test_a_revoked_node_is_refused_and_can_enrol_again(client, admin_headers):
    tokens = _seed()["tokens"]
    _, headers = _enrol(client, tokens, "site-a")
    assert client.get("/api/v1/agent/desired-state", headers=headers).status_code in (200, 204, 404)
    node = next(n for n in client.get("/api/v1/nodes", headers=admin_headers).json() if n["name"] == "site-a")
    assert node["revoked"] is False

    assert client.post(f"/api/v1/nodes/{node['id']}/revoke", headers=admin_headers).status_code == 200
    assert client.get("/api/v1/agent/desired-state", headers=headers).status_code == 401
    node = next(n for n in client.get("/api/v1/nodes", headers=admin_headers).json() if n["name"] == "site-a")
    assert node["revoked"] is True
    assert "node.revoke" in {a["action"] for a in client.get("/api/v1/audit", headers=admin_headers).json()}

    # A new token brings it back with a fresh certificate.
    sites = client.get("/api/v1/sites", headers=admin_headers).json()
    site_id = next(s["id"] for s in sites if s["name"] == "site-a")
    token = client.post("/api/v1/enrolment-tokens", headers=admin_headers, json={"site_id": site_id}).json()["token"]
    _, again = _enrol(client, {"site-a": token}, "site-a")
    assert client.get("/api/v1/agent/desired-state", headers=again).status_code in (200, 204, 404)


def _renew(client, headers, csr):
    return client.post("/api/v1/agent/renew", headers=headers, json={"csr_pem": csr})


def test_renewal_issues_a_new_certificate_and_retires_the_old_one(client, admin_headers):
    tokens = _seed()["tokens"]
    first, old = _enrol(client, tokens, "site-a")
    old_cert = x509.load_pem_x509_certificate(first["cert_pem"].encode())

    # Only over mutual TLS.
    assert client.post("/api/v1/agent/renew", json={"csr_pem": _csr("site-a")}).status_code == 403

    r = _renew(client, old, _csr("site-a"))
    assert r.status_code == 200, r.text
    cert = x509.load_pem_x509_certificate(r.json()["cert_pem"].encode())
    ca = x509.load_pem_x509_certificate(r.json()["ca_pem"].encode())
    cert.verify_directly_issued_by(ca)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "site-a"
    assert cert.serial_number != old_cert.serial_number
    assert cert.not_valid_after_utc >= old_cert.not_valid_after_utc

    # The old certificate stops working at once; the new one works.
    new = {**old, "X-SSL-Client-Serial": format(cert.serial_number, "X")}
    assert client.get("/api/v1/agent/desired-state", headers=old).status_code == 401
    assert _renew(client, old, _csr("site-a")).status_code == 401
    assert client.get("/api/v1/agent/desired-state", headers=new).status_code in (200, 204, 404)

    audit = [a for a in client.get("/api/v1/audit", headers=admin_headers).json() if a["action"] == "node.cert_renewed"]
    assert len(audit) == 1 and audit[0]["target"] == "site-a"
    assert audit[0]["detail"]["serial"] == pki.serial_hex(cert.serial_number)
    assert audit[0]["detail"]["old_serial"] == pki.serial_hex(old_cert.serial_number)

    # And it can renew again with the new one.
    assert _renew(client, new, _csr("site-a")).status_code == 200


def test_renewal_refuses_another_name_a_bad_csr_and_a_revoked_node(client, admin_headers):
    tokens = _seed()["tokens"]
    _, headers = _enrol(client, tokens, "site-a")

    r = _renew(client, headers, _csr("site-b"))
    assert r.status_code == 400 and "site-b" in r.json()["detail"]
    assert _renew(client, headers, "not a csr").status_code == 400
    # Nothing changed: the current certificate still works.
    assert client.get("/api/v1/agent/desired-state", headers=headers).status_code in (200, 204, 404)

    node = next(n for n in client.get("/api/v1/nodes", headers=admin_headers).json() if n["name"] == "site-a")
    assert client.post(f"/api/v1/nodes/{node['id']}/revoke", headers=admin_headers).status_code == 200
    assert _renew(client, headers, _csr("site-a")).status_code == 401
