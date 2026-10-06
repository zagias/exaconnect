"""Enterprise SSO: set-up, test sign-in, domain approval, routing, require_sso, auto-provisioning."""

import pytest

from exaconnect_controller import db
from exaconnect_controller.identity import keycloak

from .commai_helpers import PASSWORD, business
from .identity_helpers import fake_issuer, identity_settings, sign_in_via

PORTAL = {"X-Requested-With": "exa-portal"}
METADATA = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://sts.bank.example/adfs">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
      Location="https://sts.bank.example/adfs/ls/"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""


@pytest.fixture
def settings(tmp_path):
    return identity_settings(tmp_path)


@pytest.fixture(name="issuer")
def _issuer(monkeypatch):
    yield from fake_issuer(monkeypatch)


def _base(b):
    return f"/api/v1/customers/{b['id']}/sso-connections"


def _setup(client, b, domains=("examplebank.example",)):
    r = client.post(
        _base(b),
        json={"protocol": "saml", "display_name": "Bank ADFS", "domains": list(domains), "metadata_xml": METADATA},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _test_and_enable(client, b, c, issuer, email):
    r = client.post(f"{_base(b)}/{c['id']}/test", json={"next": "/commai/settings"}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text

    class _R:
        status_code = 302
        headers = {"location": r.json()["start_url"]}
        text = ""

    r, _ = sign_in_via(client, issuer, _R(), email, follow_start=False)
    assert r.status_code == 302 and "sso_test=ok" in r.headers["location"], r.headers["location"]
    assert "set-cookie" not in r.headers  # a test never creates a session
    r = client.post(f"{_base(b)}/{c['id']}/enable", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    return r.json()


def _approve_all(client, admin_headers):
    for d in client.get("/api/v1/sso-domains", headers=admin_headers).json():
        assert client.post(f"/api/v1/sso-domains/{d['id']}/approve", headers=admin_headers).status_code == 200


def test_bad_inputs_refused(client):
    b = business(client)
    h = b["agent"]["h"]
    assert (
        client.post(
            _base(b), json={"protocol": "saml", "display_name": "x", "metadata_xml": "<nope"}, headers=h
        ).status_code
        == 422
    )
    assert (
        client.post(
            _base(b),
            json={"protocol": "saml", "display_name": "x", "domains": ["gmail.com"], "metadata_xml": METADATA},
            headers=h,
        ).status_code
        == 422
    )
    assert (
        client.post(
            _base(b), json={"protocol": "oidc", "display_name": "x", "metadata_url": "http://insecure"}, headers=h
        ).status_code
        == 422
    )
    xxe = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>' + METADATA.split("?>", 1)[1]
    assert (
        client.post(
            _base(b), json={"protocol": "saml", "display_name": "x", "metadata_xml": xxe}, headers=h
        ).status_code
        == 422
    )


def test_full_flow(client, admin_headers, issuer):
    b = business(client)
    c = _setup(client, b)
    assert c["status"] == "draft" and c["domains"][0]["status"] == "pending"
    assert keycloak._simulated.idps[c["alias"]]["providerId"] == "saml"
    email = b["agent"]["email"]  # agent@examplebank.example
    # Can't switch on before a successful test.
    assert client.post(f"{_base(b)}/{c['id']}/enable", headers=b["agent"]["h"]).status_code == 409
    c = _test_and_enable(client, b, c, issuer, email)
    assert c["status"] == "enabled" and c["last_test"]["ok"]
    # Not routed until an ExaCarib admin approves the domain.
    assert client.post("/api/v1/auth/sso/discover", json={"email": email}).json()["method"] == "password"
    assert client.get("/api/v1/sso-domains", headers=b["agent"]["h"]).status_code == 403
    _approve_all(client, admin_headers)
    d = client.post("/api/v1/auth/sso/discover", json={"email": email.upper()}).json()
    assert d["method"] == "sso" and d["start_url"].endswith(f"idp={c['alias']}&next=/")
    assert client.post("/api/v1/auth/sso/discover", json={"email": "x@other.example"}).json()["method"] == "password"

    # Existing person signs in through the company's provider.
    client.cookies.clear()
    r, _ = sign_in_via(client, issuer, d["start_url"], email)
    assert r.status_code == 302 and r.headers["location"] == "/", r.headers["location"]
    assert client.get("/api/v1/auth/me").json()["email"] == email

    # A new person from the approved domain is created with member rights only.
    client.cookies.clear()
    r, _ = sign_in_via(client, issuer, d["start_url"], "new.person@examplebank.example", extra={"name": "New Person"})
    assert r.headers["location"] == "/"
    me = client.get("/api/v1/auth/me").json()
    assert me["role"] == "customer" and me["customer_id"] == b["id"]
    assert me["scopes"] == ["commai:read", "commai:write", "commai:notes"]
    assert client.get(f"/api/v1/customers/{b['id']}/sso-connections").status_code == 403
    # A person from another domain can't come in through this provider.
    client.cookies.clear()
    r, _ = sign_in_via(client, issuer, d["start_url"], "someone@elsewhere.example")
    assert "signin_error" in r.headers["location"]

    # Require SSO: password sign-in for the domain is refused, Google too.
    r = client.patch(f"{_base(b)}/{c['id']}", json={"require_sso": True}, headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "enabled"
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 403
    client.cookies.clear()
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google", email)
    assert "signin_error" in r.headers["location"]

    with db.tx() as conn:
        actions = {r["action"] for r in conn.execute("SELECT action FROM audit_log").fetchall()}
    assert {
        "sso.create",
        "sso.test",
        "sso.enable",
        "sso.update",
        "sso.domain_approve",
        "login",
        "login_failed",
    } <= actions


def test_changing_metadata_needs_a_new_test(client, issuer, admin_headers):
    b = business(client)
    c = _setup(client, b)
    c = _test_and_enable(client, b, c, issuer, b["agent"]["email"])
    r = client.patch(
        f"{_base(b)}/{c['id']}", json={"metadata_url": "https://sts.bank.example/metadata.xml"}, headers=b["agent"]["h"]
    )
    assert r.status_code == 200 and r.json()["status"] == "draft"


def test_domain_cannot_be_claimed_twice(client, admin_headers):
    a = business(client, "Example Bank")
    b = business(client, "Other Bank")
    _setup(client, a, ["shared.example"])
    _setup(client, b, ["shared.example"])
    pending = client.get("/api/v1/sso-domains", headers=admin_headers).json()
    assert len(pending) == 2
    assert client.post(f"/api/v1/sso-domains/{pending[0]['id']}/approve", headers=admin_headers).status_code == 200
    assert client.post(f"/api/v1/sso-domains/{pending[1]['id']}/approve", headers=admin_headers).status_code == 409


def test_other_business_cannot_manage(client):
    a = business(client, "Example Bank")
    b = business(client, "Other Bank")
    c = _setup(client, a)
    assert client.get(_base(a), headers=b["agent"]["h"]).status_code == 403
    assert client.delete(f"{_base(a)}/{c['id']}", headers=b["agent"]["h"]).status_code == 403
    assert client.delete(f"{_base(a)}/{c['id']}", headers=a["agent"]["h"]).status_code == 204
    assert c["alias"] not in keycloak._simulated.idps


def test_test_needs_gateway(client, tmp_path):
    b = business(client)
    c = _setup(client, b)
    client.app.state.settings = identity_settings(tmp_path, oidc_issuer="")
    r = client.post(f"{_base(b)}/{c['id']}/test", headers=b["agent"]["h"])
    assert r.status_code == 409
