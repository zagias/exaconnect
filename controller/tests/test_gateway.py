"""The agent gateway on the internet (ADR 0024): the public name on the certificate, and in enrolment details."""

from __future__ import annotations

from dataclasses import replace

from cryptography import x509

from exaconnect_controller import pki
from exaconnect_controller.settings import Settings

from .test_flow import _enrol, _seed


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
