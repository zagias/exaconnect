"""Directory templates and connectors (ADR 0030): templates per provider, SCIM
quirks with real-shaped payloads, connection tests, pull connectors (Microsoft
Graph, Google Directory API, LDAP/AD), presets, approval and tenant isolation."""

from __future__ import annotations

import base64
import datetime as dt
import json
import urllib.parse

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from exaconnect_controller import db
from exaconnect_controller.commai import golive
from exaconnect_controller.commai.automation import http
from exaconnect_controller.identity import sessions
from exaconnect_controller.identity.directory import checks, quirks, scim_fixtures, sources, templates

from .commai_helpers import business, run_jobs
from .identity_helpers import ISSUER, PUBLIC, identity_settings

SCIM = "/api/v1/scim/v2"
IDP_MD = "urn:oasis:names:tc:SAML:2.0:metadata"


@pytest.fixture
def settings(tmp_path):
    return identity_settings(tmp_path)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EXA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("EXA_DIRECTORY_LDAP_ALLOW_PLAIN", raising=False)
    monkeypatch.delenv("EXA_MS_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("EXA_GOOGLE_DIRECTORY_CREDENTIALS", raising=False)


def _url(b: dict, provider: str, tail: str = "") -> str:
    return f"/api/v1/customers/{b['id']}/directory/{provider}{tail}"


def _switch_on(provider: str, customer_id: str) -> None:
    key = f"directory-{provider}"
    with db.tx() as conn:
        for c in golive.get(conn, "feature", key)["criteria"]:
            golive.check(conn, "feature", key, c["criterion"], True, "test run", "test")
        golive.set_status(conn, "feature", key, "pilot", "test", [customer_id])


def _cert(days: int = 365) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


def idp_metadata(entity: str = "https://sts.windows.net/t/", days: int = 365) -> str:
    return f"""<?xml version="1.0"?>
<EntityDescriptor xmlns="{IDP_MD}" entityID="{entity}">
  <IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <KeyDescriptor use="signing"><KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#"><X509Data>
      <X509Certificate>{_cert(days)}</X509Certificate></X509Data></KeyInfo></KeyDescriptor>
    <NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</NameIDFormat>
    <SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
      Location="https://login.microsoftonline.com/t/saml2"/>
  </IDPSSODescriptor>
</EntityDescriptor>"""


TENANT = "0f0e0d0c-0b0a-4908-8706-050403020100"
APP = "1a1b1c1d-1e1f-4a2b-8c3d-4e5f60718293"


# ---- templates --------------------------------------------------------------------------------


def test_every_template_renders_values_for_this_business(client):
    b = business(client, people=("agent",))
    r = client.get("/api/v1/directory/templates", headers=b["agent"]["h"])
    assert {t["provider"] for t in r.json()} == {
        "entra",
        "google",
        "okta",
        "jumpcloud",
        "onelogin",
        "ping",
        "auth0",
        "active-directory",
        "ldap",
    }
    listing = client.get(f"/api/v1/customers/{b['id']}/directory", headers=b["agent"]["h"]).json()
    assert all(x["status"] == "off" and x["available"] is False for x in listing)

    # Saving starts the set-up and fixes the alias; values are made for this business.
    r = client.put(_url(b, "entra"), json={"inputs": {"tenant_id": TENANT}}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    g = r.json()
    alias = g["setup"] and client.get(_url(b, "entra"), headers=b["agent"]["h"]).json()
    values = {v["key"]: v for v in g["values"]}
    assert values["entity_id"]["label"] == "Identifier (Entity ID)" and values["entity_id"]["value"] == ISSUER
    acs = values["acs_url"]["value"]
    assert acs.startswith(f"{ISSUER}/broker/sso-entra-") and acs.endswith("/endpoint")
    assert values["scim_url"] == {
        "key": "scim_url",
        "label": "Tenant URL",
        "value": f"{PUBLIC}/api/v1/scim/v2",
        "missing": "",
    }
    assert values["sign_on_url"]["value"].startswith(f"{PUBLIC}/api/v1/auth/oidc/start?idp=sso-entra-")
    md = g["provider_metadata"]
    assert md["url"].startswith(f"https://login.microsoftonline.com/{TENANT}/federationmetadata")
    assert md["needs"] == ["app_id"]
    assert any(acs in s for s in g["steps"]) and any(
        f"{PUBLIC}/api/v1/scim/v2?aadOptscim062020" in s for s in g["steps"]
    )
    assert g["saml"]["claims"]["groups"].endswith("/claims/groups")
    assert g["saml"]["name_id_format"] == templates.SAML_EMAIL
    assert any("title case" in q for q in g["scim"]["quirks"])
    assert alias["setup"]["status"] == "off"

    # Bad input is refused with an example.
    r = client.put(_url(b, "entra"), json={"inputs": {"tenant_id": "contoso"}}, headers=b["agent"]["h"])
    assert r.status_code == 422 and "Example" in r.json()["detail"]

    # Okta: provider's own console labels; OneLogin: a validator regex for the ACS URL.
    ok = client.put(
        _url(b, "okta"), json={"inputs": {"okta_domain": "https://Example.okta.com/"}}, headers=b["agent"]["h"]
    )
    vals = {v["key"]: v for v in ok.json()["values"]}
    assert vals["acs_url"]["label"] == "Single sign-on URL" and vals["entity_id"]["label"].startswith("Audience URI")
    assert vals["scim_url"]["label"] == "SCIM connector base URL"
    assert ok.json()["setup"]["inputs"]["okta_domain"] == "example.okta.com"
    ol = client.put(_url(b, "onelogin"), json={"inputs": {"subdomain": "example"}}, headers=b["agent"]["h"]).json()
    vals = {v["key"]: v["value"] for v in ol["values"]}
    assert vals["acs_validator"].startswith("^https://") and vals["acs_validator"].endswith("/endpoint$")

    # Google: no SCIM; the pull values wait for ExaCarib's service account.
    gg = client.put(_url(b, "google"), json={"mode": "pull"}, headers=b["agent"]["h"]).json()
    vals = {v["key"]: v for v in gg["values"]}
    assert gg["scim"]["supported"] is False and "scim_url" not in vals
    assert vals["google_scopes"]["value"].count("readonly") == 3
    assert vals["google_client_id"]["missing"].startswith("Waiting for ExaCarib")

    # A mode the provider doesn't offer is refused.
    assert client.put(_url(b, "auth0"), json={"mode": "scim"}, headers=b["agent"]["h"]).status_code == 422


def test_ping_and_auth0_render_their_patterns():
    ctx = templates.Context(PUBLIC, ISSUER, "sso-ping-abc", {"region": "eu", "env_id": TENANT, "app_id": APP})
    out = templates.render(templates.PING, ctx, "saml", "scim")
    assert out["provider_metadata"]["url"] == f"https://auth.pingone.eu/{TENANT}/saml20/metadata/{APP}"
    assert {v["key"]: v["value"] for v in out["values"]}["scim_filter"] == 'userName eq "%s"'
    out = templates.render(
        templates.AUTH0, templates.Context(PUBLIC, ISSUER, "sso-a", {"auth0_domain": "x.eu.auth0.com"}), "oidc", "none"
    )
    assert out["provider_metadata"]["url"] == "https://x.eu.auth0.com/.well-known/openid-configuration"
    assert {v["key"] for v in out["values"]} == {"redirect_uri", "sign_on_url"}
    # Without a gateway the values say why they are empty.
    out = templates.render(templates.OKTA, templates.Context(PUBLIC, "", "sso-o"), "saml", "scim")
    assert {v["key"]: v["missing"] for v in out["values"]}["acs_url"].startswith("Set when")


# ---- SCIM quirks ------------------------------------------------------------------------------


def test_quirk_normaliser():
    ops = quirks.normalise_ops(
        [
            {"op": "Replace", "path": "urn:ietf:params:scim:schemas:core:2.0:User:active", "value": False},
            {"op": "replace", "path": "displayName", "value": [{"value": "Ana"}]},
            {"op": "REMOVE", "path": "members", "value": {"value": "u1"}},
            {"op": "replace", "value": {"urn:ietf:params:scim:schemas:core:2.0:User": {"displayName": "B"}}},
        ]
    )
    assert ops[0] == {"op": "replace", "path": "active", "value": False}
    assert ops[1]["value"] == "Ana" and ops[2]["value"] == [{"value": "u1"}]
    assert ops[3]["value"] == {"displayName": "B"}
    assert quirks.normalise_filter('userName Eq "a@b.c"') == 'userName eq "a@b.c"'
    assert quirks.normalise_filter('urn:ietf:params:scim:schemas:core:2.0:User:userName EQ "x"') == 'userName eq "x"'


def _scim_token(client, b) -> dict:
    r = client.post(f"/api/v1/customers/{b['id']}/scim-tokens", json={"name": "t"}, headers=b["agent"]["h"])
    return {"Authorization": f"Bearer {r.json()['token']}", "Content-Type": "application/scim+json"}


@pytest.mark.parametrize("provider", sorted(scim_fixtures.FIXTURES))
def test_provider_scim_payloads_through_the_real_endpoint(client, provider):
    b = business(client, people=("agent",))
    h = _scim_token(client, b)
    email = f"ana.{provider}@examplebank.example"
    reqs = scim_fixtures.requests_for(provider, {"email": email, "domain": "examplebank.example", "password": "pw-x1"})
    user_id = group_id = ""
    session = None
    for method, path, body in reqs:
        path = path.replace("{user_id}", user_id).replace("{group_id}", group_id)
        body = json.loads(json.dumps(body).replace("{user_id}", user_id).replace("{group_id}", group_id))
        url = SCIM + path.split("?")[0]
        params = dict(urllib.parse.parse_qsl(path.split("?")[1])) if "?" in path else None
        r = client.request(method, url, json=body, params=params, headers=h)
        assert r.status_code in (200, 201), f"{provider} {method} {path}: {r.text}"
        if method == "POST" and path == "/Users":
            user_id = r.json()["id"]
            with db.tx() as conn:
                session, _ = sessions.start(conn, user_id, 1, "sso")
        if method == "POST" and path == "/Groups":
            group_id = r.json()["id"]
    final = client.get(f"{SCIM}/Users/{user_id}", headers=h).json()
    assert final["active"] is False  # every provider's switch-off shape works
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session}"}).status_code == 401
    if provider == "okta":
        assert client.get(f"{SCIM}/Groups/{group_id}", headers=h).json()["displayName"] == "ExaCarib Support"
        assert final["name"]["familyName"] == "Lee-Brown"
    if provider == "entra":
        assert final["displayName"] == "Ana M. Lee" and final["name"]["givenName"] == "Ana M."
    if provider == "ping":
        assert final["name"]["familyName"] == "Lee-Brown" and final["displayName"] == "Ana Lee-Brown"
    if provider == "onelogin":
        assert final["displayName"] == "Ana J. Lee"
        assert client.get(f"{SCIM}/Groups/{group_id}", headers=h).json()["members"] == []


# ---- connection tests -------------------------------------------------------------------------


def test_saml_metadata_parse_errors_are_clear():
    res = checks.parse_saml("<EntityDescriptor><broken></EntityDescriptor>")
    assert res[0]["ok"] is False and "line 1, column" in res[0]["detail"]
    res = checks.parse_saml("<html><body>Sign in</body></html>")
    assert "not SAML metadata" in res[0]["detail"] and "<html>" in res[0]["detail"]
    sp = f'<EntityDescriptor xmlns="{IDP_MD}" entityID="x"><SPSSODescriptor/></EntityDescriptor>'
    assert "service provider metadata" in checks.parse_saml(sp)[1]["detail"]
    assert "DOCTYPE" in checks.parse_saml('<!DOCTYPE x [<!ENTITY a "b">]><x/>')[0]["detail"]
    good = checks.parse_saml(idp_metadata(), templates.SAML_EMAIL)
    assert all(r["ok"] for r in good) and any("valid until" in r["detail"] for r in good)
    soon = checks.parse_saml(idp_metadata(days=10))
    assert any(r.get("warn") and "Renew" in r["detail"] for r in soon)
    assert checks.host_allowed("https://evil.example.com/md.xml", ("login.microsoftonline.com",)).startswith(
        "That address"
    )
    assert (
        checks.host_allowed("http://login.microsoftonline.com/x", ("login.microsoftonline.com",))
        == "Use an https:// address."
    )
    assert checks.host_allowed("https://10.0.0.5/md", ()) is not None
    assert (
        checks.host_allowed("https://x.oktapreview.com/app/1/sso/saml/metadata", (".okta.com", ".oktapreview.com"))
        is None
    )
    res = checks.parse_discovery(b"<html>", "https://x/.well-known/openid-configuration")
    assert "did not return JSON" in res[0]["detail"]
    res = checks.parse_discovery(json.dumps({"issuer": "https://x"}), "https://x/.well-known/openid-configuration")
    assert "missing authorization_endpoint" in res[0]["detail"]


def test_guided_entra_setup_test_and_connect(client, monkeypatch):
    b = business(client, people=("agent", "agent2"))
    h = b["agent"]["h"]
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return idp_metadata().encode()

    monkeypatch.setattr(checks, "fetch", fake_fetch)
    r = client.put(
        _url(b, "entra"),
        json={
            "protocol": "saml",
            "mode": "scim",
            "inputs": {"tenant_id": TENANT, "app_id": APP},
            "domains": ["examplebank.example"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    setup = r.json()["setup"]
    assert setup["connection"]["status"] == "draft"
    assert setup["connection"]["metadata_url"].startswith(f"https://login.microsoftonline.com/{TENANT}/")
    assert setup["domains"][0]["status"] == "pending"  # ExaCarib still approves the domain

    t = client.post(_url(b, "entra", "/test"), headers=h).json()
    names = {x["check"]: x for x in t["results"]}
    assert names["metadata"]["ok"] and names["signing certificate"]["ok"]
    assert names["test sign-in"]["ok"] is False and "Test sign-in" in names["test sign-in"]["detail"]
    assert names["SCIM requests"]["ok"] is True, names["SCIM requests"]
    assert t["signin_test_path"].endswith("/test") and fetched
    with db.tx() as conn:  # the dry run kept nothing
        assert conn.execute("SELECT count(*) AS n FROM users WHERE email LIKE 'scim-test-%%'").fetchone()["n"] == 0

    # Off until ExaCarib switches the provider on, and only after a successful test sign-in.
    r = client.post(_url(b, "entra", "/connect"), headers=h)
    assert r.status_code == 409 and "not switched on" in r.json()["detail"]
    _switch_on("entra", b["id"])
    r = client.post(_url(b, "entra", "/connect"), headers=h)
    assert r.status_code == 409 and "test sign-in" in r.json()["detail"]
    with db.tx() as conn:
        conn.execute(
            "UPDATE sso_connections SET status = 'tested', last_test = %s WHERE id = %s",
            (json.dumps({"ok": True, "email": "ana@examplebank.example"}), setup["connection"]["id"]),
        )
    r = client.post(_url(b, "entra", "/connect"), headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    token = out["scim_token_value"]
    assert token.startswith("scim_") and out["setup"]["status"] == "connected"
    assert out["setup"]["connection"]["status"] == "enabled" and out["setup"]["scim_token"]["prefix"] == token[:12]
    sh = {"Authorization": f"Bearer {token}"}
    assert client.get(f"{SCIM}/Users", headers=sh).status_code == 200
    again = client.get(_url(b, "entra"), headers=h).json()
    assert "scim_token_value" not in again  # shown once

    # Bad metadata from the provider is reported plainly.
    monkeypatch.setattr(checks, "fetch", lambda url: b"<html><body>Sign in to your account</body></html>")
    bad = client.post(_url(b, "entra", "/test"), headers=h).json()
    assert bad["ok"] is False and "not SAML metadata" in bad["results"][0]["detail"]

    # Disconnect: sign-in off, token revoked.
    r = client.post(_url(b, "entra", "/disconnect"), headers=h).json()
    assert r["setup"]["status"] == "off" and r["setup"]["connection"]["status"] == "disabled"
    assert client.get(f"{SCIM}/Users", headers=sh).status_code == 401

    # A metadata URL off the provider's domain is refused.
    r = client.put(_url(b, "entra"), json={"metadata_url": "https://evil.example.com/md.xml"}, headers=h)
    assert r.status_code == 422 and "provider's own domain" in r.json()["detail"]


# ---- presets and approval -----------------------------------------------------------------------


def test_admin_preset_waits_for_a_second_admin(client):
    b = business(client, people=("agent", "agent2"))
    h = _scim_token(client, b)
    u = client.post(f"{SCIM}/Users", json={"userName": "boss@examplebank.example", "active": True}, headers=h).json()
    g = client.post(
        f"{SCIM}/Groups", json={"displayName": "ExaCarib Admins", "members": [{"value": u["id"]}]}, headers=h
    ).json()
    client.put(_url(b, "okta"), json={"mode": "scim"}, headers=b["agent"]["h"])
    pv = client.get(_url(b, "okta", "/preview"), headers=b["agent"]["h"]).json()["groups"]
    admins = next(x for x in pv if x["group"] == "ExaCarib Admins")
    assert admins["business_admin"] == "preset not accepted" and admins["in_exacarib"]

    r = client.put(
        _url(b, "okta", "/presets"), json={"accepted": ["admins", "agents", "nope"]}, headers=b["agent"]["h"]
    )
    assert r.status_code == 422
    client.put(_url(b, "okta", "/presets"), json={"accepted": ["admins", "agents"]}, headers=b["agent"]["h"])
    assert client.post(_url(b, "okta", "/presets/apply"), headers=b["agent"]["h"]).json()["applied"] == [
        "ExaCarib Admins"
    ]
    with db.tx() as conn:
        row = conn.execute("SELECT access_scopes FROM users WHERE id = %s", (u["id"],)).fetchone()
    assert row["access_scopes"] is not None  # still member rights only
    pv = client.get(_url(b, "okta", "/preview"), headers=b["agent"]["h"]).json()["groups"]
    assert next(x for x in pv if x["group"] == "ExaCarib Admins")["business_admin"] == "needs approval"

    base = f"/api/v1/customers/{b['id']}/directory-groups/{g['id']}/approve-admin"
    assert client.post(base, headers=b["agent"]["h"]).status_code == 403  # the one who accepted can't approve
    assert client.post(base, headers=b["agent2"]["h"]).status_code == 200
    with db.tx() as conn:
        assert (
            conn.execute("SELECT access_scopes FROM users WHERE id = %s", (u["id"],)).fetchone()["access_scopes"]
            is None
        )


# ---- pull connectors ----------------------------------------------------------------------------


class FakeHttp:
    """Microsoft Graph and the Google Directory API, as far as the connectors use them."""

    def __init__(self):
        self.calls = []
        self.graph_users = [
            {
                "id": "u1",
                "userPrincipalName": "ana@examplebank.example",
                "mail": "ana@examplebank.example",
                "givenName": "Ana",
                "surname": "Lee",
                "displayName": "Ana Lee",
                "accountEnabled": True,
            },
            {
                "id": "u2",
                "userPrincipalName": "ben@examplebank.example",
                "mail": None,
                "givenName": "Ben",
                "surname": "Ray",
                "displayName": "Ben Ray",
                "accountEnabled": True,
            },
            {
                "id": "u3",
                "userPrincipalName": "out@examplebank.example",
                "mail": "out@examplebank.example",
                "givenName": "Not",
                "surname": "InScope",
                "displayName": "x",
                "accountEnabled": True,
            },
        ]
        self.graph_groups = [{"id": "g1", "displayName": "ExaCarib Agents"}, {"id": "g2", "displayName": "Finance"}]
        self.graph_members = {"g1": [{"id": "u1"}, {"id": "u2"}], "g2": [{"id": "u3"}]}
        self.consented = True

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers.get("Authorization", "")))
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.netloc == "login.microsoftonline.com":
            form = dict(urllib.parse.parse_qsl(data.decode()))
            assert (
                form["grant_type"] == "client_credentials" and form["scope"] == "https://graph.microsoft.com/.default"
            )
            if not self.consented:
                return http.Response(400, {"error": "unauthorized_client"}, {})
            return http.Response(200, {"access_token": "graph-tok"}, {})
        if u.netloc == "graph.microsoft.com":
            assert method == "GET" and headers["Authorization"] == "Bearer graph-tok"
            if u.path == "/v1.0/users":
                if q.get("$top") == "1":
                    return http.Response(200, {"value": self.graph_users[:1]}, {})
                # Two pages, to follow @odata.nextLink.
                if "skip" in q:
                    return http.Response(200, {"value": self.graph_users[1:]}, {})
                return http.Response(200, {"value": self.graph_users[:1], "@odata.nextLink": url + "&skip=1"}, {})
            if u.path == "/v1.0/groups":
                return http.Response(200, {"value": self.graph_groups}, {})
            gid = u.path.split("/")[3]
            return http.Response(200, {"value": self.graph_members.get(gid, [])}, {})
        if u.netloc == "oauth2.googleapis.com":
            form = dict(urllib.parse.parse_qsl(data.decode()))
            head, body, _ = form["assertion"].split(".")
            claims = json.loads(base64.urlsafe_b64decode(body + "=="))
            assert claims["sub"] == "it-admin@examplebank.example" and "readonly" in claims["scope"]
            assert "write" not in claims["scope"].replace("readonly", "")
            return http.Response(200, {"access_token": "g-tok"}, {})
        if u.netloc == "admin.googleapis.com":
            if u.path.endswith("/groups"):
                return http.Response(
                    200,
                    {
                        "groups": [
                            {"id": "gg1", "name": "ExaCarib Agents", "email": "a@x"},
                            {"id": "gg2", "name": "All staff"},
                        ]
                    },
                    {},
                )
            if u.path.endswith("/members"):
                return http.Response(
                    200, {"members": [{"id": "gu1", "type": "USER"}, {"id": "grp", "type": "GROUP"}]}, {}
                )
            if u.path.endswith("/users"):
                return http.Response(
                    200,
                    {
                        "users": [
                            {
                                "id": "gu1",
                                "primaryEmail": "Cara@ExampleBank.example",
                                "name": {"givenName": "Cara", "familyName": "Diaz", "fullName": "Cara Diaz"},
                                "suspended": False,
                            },
                            {"id": "gu9", "primaryEmail": "z@examplebank.example", "name": {}},
                        ]
                    },
                    {},
                )
        raise AssertionError(url)


def test_graph_pull_refuses_without_app_then_syncs(client, monkeypatch):
    b = business(client, people=("agent",))
    h = b["agent"]["h"]
    client.put(_url(b, "entra"), json={"protocol": "", "mode": "pull", "inputs": {"tenant_id": TENANT}}, headers=h)
    t = client.post(_url(b, "entra", "/test"), headers=h).json()
    assert t["ok"] is False and "EXA_MS_GRAPH_CLIENT_ID" in t["results"][0]["detail"]

    fake = FakeHttp()
    monkeypatch.setattr(http, "transport", fake)
    monkeypatch.setenv("EXA_MS_GRAPH_CLIENT_ID", "app-" + "1" * 8)
    monkeypatch.setenv("EXA_MS_GRAPH_CLIENT_SECRET", "s" * 20)
    fake.consented = False
    t = client.post(_url(b, "entra", "/test"), headers=h).json()
    assert "admin consent link" in t["results"][0]["detail"]
    guide = client.get(_url(b, "entra"), headers=h).json()
    consent = {v["key"]: v["value"] for v in guide["values"]}["admin_consent_url"]
    assert consent == f"https://login.microsoftonline.com/{TENANT}/adminconsent?client_id=app-11111111"
    fake.consented = True
    t = client.post(_url(b, "entra", "/test"), headers=h).json()
    assert t["ok"], t
    pv = client.get(_url(b, "entra", "/preview"), params={"live": True}, headers=h).json()
    assert [g["group"] for g in pv["groups"]] == ["ExaCarib Agents"] and "2 people" in pv["note"]

    _switch_on("entra", b["id"])
    assert client.post(_url(b, "entra", "/connect"), headers=h).status_code == 200
    run_jobs()
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT email, provisioned_by, access_scopes FROM users WHERE customer_id = %s"
            " AND provisioned_by IS NOT NULL"
            " ORDER BY email",
            (b["id"],),
        ).fetchall()
        team = conn.execute(
            "SELECT count(*) AS n FROM commai_team_members m JOIN commai_teams t ON t.id = m.team_id WHERE t.name = %s",
            ("ExaCarib Agents",),
        ).fetchone()["n"]
        nxt = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE kind = 'directory.sync' AND status = 'queued'"
        ).fetchone()
    assert [r["email"] for r in rows] == ["ana@examplebank.example", "ben@examplebank.example"]  # out of scope ignored
    assert all(r["provisioned_by"] == "directory" and r["access_scopes"] for r in rows) and team == 2
    assert nxt["n"] == 1  # the next run is scheduled
    assert all("secret" not in c[1] for c in fake.calls)
    last = client.get(_url(b, "entra"), headers=h).json()["setup"]["last_sync"]
    assert last["ok"] and last["added"] == 2 and last["groups_added"] == 1


def test_google_directory_pull(client, monkeypatch):
    b = business(client, people=("agent",))
    h = b["agent"]["h"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    monkeypatch.setenv(
        "EXA_GOOGLE_DIRECTORY_CREDENTIALS",
        json.dumps({"client_email": "sa@exacarib.iam.example", "private_key": pem.decode(), "client_id": "1234567890"}),
    )
    monkeypatch.setattr(http, "transport", FakeHttp())
    r = client.put(
        _url(b, "google"),
        json={"protocol": "saml", "mode": "pull", "inputs": {"admin_email": "it-admin@examplebank.example"}},
        headers=h,
    )
    vals = {v["key"]: v["value"] for v in r.json()["values"]}
    assert vals["google_client_id"] == "1234567890"
    t = client.post(_url(b, "google", "/test"), headers=h).json()
    by = {x["check"]: x for x in t["results"]}
    assert by["Google Directory API"]["ok"] and by["sign-in details"]["ok"] is False  # metadata not uploaded yet
    with db.tx() as conn:
        setup = conn.execute("SELECT * FROM directory_setups WHERE customer_id = %s", (b["id"],)).fetchone()
        from exaconnect_controller.identity.directory import sync

        out = sync.run(conn, setup)
        u = conn.execute("SELECT * FROM users WHERE email = 'cara@examplebank.example'").fetchone()
    assert out["added"] == 1 and u["given_name"] == "Cara" and u["customer_id"] is not None


# ---- LDAP and Active Directory -------------------------------------------------------------------


BASE = "dc=examplebank,dc=example"


@pytest.fixture
def ad(monkeypatch):
    import ldap3

    server = ldap3.Server("dc1.examplebank.example", use_ssl=True, get_info=None)
    seed = ldap3.Connection(server, user=f"cn=svc,{BASE}", password="bind-pw-123", client_strategy=ldap3.MOCK_SYNC)
    seed.strategy.add_entry(f"cn=svc,{BASE}", {"userPassword": "bind-pw-123", "objectClass": "person"})
    assert seed.bind()
    opened = []

    def open_ldap(cfg, password):
        assert cfg.use_ssl and cfg.port == 636
        opened.append(cfg)
        return ldap3.Connection(server, user=cfg.bind_dn, password=password, client_strategy=ldap3.MOCK_SYNC)

    monkeypatch.setattr(sources, "open_ldap", open_ldap)

    class Dir:
        conn = seed
        calls = opened

        def person(self, cn, mail, guid, uac="512", given="", sn=""):
            self.conn.strategy.add_entry(
                f"cn={cn},ou=People,{BASE}",
                {
                    "objectClass": ["top", "person", "user"],
                    "objectCategory": "person",
                    "cn": cn,
                    "mail": mail,
                    "givenName": given or cn.split()[0],
                    "sn": sn or cn.split()[-1],
                    "displayName": cn,
                    "objectGUID": bytes([guid]) * 16,
                    "userAccountControl": uac,
                },
            )

        def group(self, cn, members, guid):
            self.conn.strategy.add_entry(
                f"cn={cn},ou=Groups,{BASE}",
                {
                    "objectClass": ["top", "group"],
                    "cn": cn,
                    "objectGUID": bytes([guid]) * 16,
                    "member": [f"cn={m},ou=People,{BASE}" for m in members],
                },
            )

        def modify(self, dn, changes):
            self.conn.modify(dn, {k: [(ldap3.MODIFY_REPLACE, v)] for k, v in changes.items()})

        def delete(self, dn):
            self.conn.delete(dn)

    return Dir()


def _ad_setup(client, b, **extra):
    body = {
        "protocol": "",
        "mode": "ldap",
        "host": "dc1.examplebank.example",
        "base_dn": BASE,
        "bind_dn": f"cn=svc,{BASE}",
        "bind_password": "bind-pw-123",
        **extra,
    }
    return client.put(_url(b, "active-directory"), json=body, headers=b["agent"]["h"])


def test_ldap_sync_adds_updates_disables_and_ends_sessions(client, ad):
    b = business(client, people=("agent",))
    h = b["agent"]["h"]
    for i, n in enumerate(["Ana Lee", "Ben Ray", "Cy Fox", "Di Moss", "Ed Hart", "Flo Ng"], 1):
        ad.person(n, f"{n.split()[0].lower()}@examplebank.example", i)
    ad.group("ExaCarib Agents", ["Ana Lee", "Ben Ray", "Cy Fox"], 50)
    ad.group("ExaCarib Admins", ["Ana Lee"], 51)
    ad.group("Domain Users", ["Ana Lee", "Ben Ray"], 52)

    r = _ad_setup(client, b)
    assert r.status_code == 200, r.text
    setup = r.json()["setup"]
    assert setup["has_password"] and "bind_password" not in json.dumps(setup)
    with db.tx() as conn:
        assert "bind-pw-123" not in json.dumps(conn.execute("SELECT * FROM directory_setups").fetchone(), default=str)
        assert "bind-pw-123" not in json.dumps(conn.execute("SELECT * FROM audit_log").fetchall(), default=str)

    t = client.post(_url(b, "active-directory", "/test"), headers=h).json()
    assert t["ok"], t
    assert any("6 people" in x["detail"] for x in t["results"]) and "LDAPS" in t["results"][0]["detail"]
    client.put(_url(b, "active-directory", "/presets"), json={"accepted": ["agents", "admins"]}, headers=h)
    _switch_on("active-directory", b["id"])
    assert client.post(_url(b, "active-directory", "/connect"), headers=h).status_code == 200
    run_jobs()
    with db.tx() as conn:
        users = {
            r["email"]: r for r in conn.execute("SELECT * FROM users WHERE provisioned_by = 'directory'").fetchall()
        }
        groups = {g["display_name"]: g for g in conn.execute("SELECT * FROM scim_groups").fetchall()}
    assert len(users) == 6 and set(groups) == {"ExaCarib Agents", "ExaCarib Admins"}  # prefix filter
    assert groups["ExaCarib Admins"]["admin_requested"] and groups["ExaCarib Admins"]["admin_approved_at"] is None
    assert users["ana@examplebank.example"]["access_scopes"] is not None  # admin waits for approval
    assert users["ana@examplebank.example"]["scim_external_id"].startswith("active-directory:")

    with db.tx() as conn:
        ana_session, _ = sessions.start(conn, users["ana@examplebank.example"]["id"], 1, "sso")
        ben_session, _ = sessions.start(conn, users["ben@examplebank.example"]["id"], 1, "sso")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ana_session}"}).status_code == 200

    # Update, disable (userAccountControl bit 2) and removal from the directory.
    ad.modify(f"cn=Cy Fox,ou=People,{BASE}", {"sn": "Fox-Lane", "displayName": "Cy Fox-Lane"})
    ad.modify(f"cn=Ana Lee,ou=People,{BASE}", {"userAccountControl": "514"})
    ad.delete(f"cn=Ben Ray,ou=People,{BASE}")
    assert client.post(_url(b, "active-directory", "/sync"), headers=h).status_code == 202
    from exaconnect_controller.commai import jobs

    assert jobs.run_pending() == 1  # only the sync asked for now; the scheduled one waits its interval
    last = client.get(_url(b, "active-directory"), headers=h).json()["setup"]["last_sync"]
    assert last["ok"] and last["updated"] == 1 and last["disabled"] == 2, json.dumps(last)
    with db.tx() as conn:
        cy = conn.execute("SELECT * FROM users WHERE email = 'cy@examplebank.example'").fetchone()
        ben = conn.execute("SELECT * FROM users WHERE email = 'ben@examplebank.example'").fetchone()
        ben_groups = conn.execute(
            "SELECT count(*) AS n FROM scim_group_members WHERE user_id = %s", (ben["id"],)
        ).fetchone()
    assert cy["family_name"] == "Fox-Lane" and cy["display_name"] == "Cy Fox-Lane"
    assert ben["disabled_at"] is not None and ben_groups["n"] == 0
    for s in (ana_session, ben_session):
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {s}"}).status_code == 401

    # Re-enabled in the directory: switched back on.
    ad.modify(f"cn=Ana Lee,ou=People,{BASE}", {"userAccountControl": "512"})
    with db.tx() as conn:
        from exaconnect_controller.identity.directory import sync

        setup = conn.execute("SELECT * FROM directory_setups").fetchone()
        sync.run(conn, setup)
        assert (
            conn.execute("SELECT disabled_at FROM users WHERE email = 'ana@examplebank.example'").fetchone()[
                "disabled_at"
            ]
            is None
        )

    # A wrong filter that would switch most people off is refused.
    for n in ("Cy Fox", "Di Moss", "Ed Hart", "Flo Ng"):
        ad.delete(f"cn={n},ou=People,{BASE}")
    with db.tx() as conn:
        setup = conn.execute("SELECT * FROM directory_setups").fetchone()
        with pytest.raises(sync.SyncError, match="Refused"):
            sync.run(conn, setup)
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM users WHERE provisioned_by = 'directory' AND disabled_at IS NULL"
            ).fetchone()["n"]
            == 5
        )


def test_ldap_rules(client, ad, monkeypatch):
    b = business(client, people=("agent",))
    h = b["agent"]["h"]
    r = _ad_setup(client, b, plain_lab=True)
    assert r.status_code == 422 and "LDAPS" in r.json()["detail"]
    assert _ad_setup(client, b, user_filter="objectClass=user").status_code == 422
    assert _ad_setup(client, b, ca_cert_pem="not a cert").status_code == 422
    r = _ad_setup(client, b, bind_password="wrong-password")
    t = client.post(_url(b, "active-directory", "/test"), headers=h).json()
    assert t["ok"] is False and "refused the bind DN or password" in t["results"][0]["detail"]
    # Connecting needs a test that read the directory.
    _switch_on("active-directory", b["id"])
    assert client.post(_url(b, "active-directory", "/connect"), headers=h).status_code == 409
    # Plain LDAP only with the explicit lab setting.
    monkeypatch.setenv("EXA_DIRECTORY_LDAP_ALLOW_PLAIN", "lab")
    assert client.get(_url(b, "active-directory"), headers=h).json()["plain_ldap_allowed"] is True
    with pytest.raises(sources.SourceError):
        monkeypatch.delenv("EXA_DIRECTORY_LDAP_ALLOW_PLAIN")
        sources.LdapSource(sources.LdapConfig("h", BASE, "cn=x", use_ssl=False), "pw")
    # Without the vault key, the password can't be stored.
    monkeypatch.delenv("EXA_SECRETS_KEY")
    assert _ad_setup(client, b).status_code == 409


# ---- tenant isolation --------------------------------------------------------------------------


def test_tenant_isolation(client, ad):
    a = business(client, "Example Bank", people=("agent",))
    o = business(client, "Other Bank", people=("agent",))
    assert client.put(_url(a, "okta"), json={"mode": "scim"}, headers=a["agent"]["h"]).status_code == 200
    for method, tail in (
        ("GET", ""),
        ("PUT", ""),
        ("POST", "/test"),
        ("POST", "/connect"),
        ("GET", "/preview"),
        ("PUT", "/presets"),
        ("DELETE", ""),
    ):
        body = {"accepted": []} if tail == "/presets" else {}
        r = client.request(method, _url(a, "okta", tail), json=body, headers=o["agent"]["h"])
        assert r.status_code == 403, (method, tail, r.text)
    assert client.get(f"/api/v1/customers/{a['id']}/directory", headers=o["agent"]["h"]).status_code == 403
    # Each business has its own set-up, alias and values.
    ga = client.get(_url(a, "okta"), headers=a["agent"]["h"]).json()
    go = client.put(_url(o, "okta"), json={"mode": "scim"}, headers=o["agent"]["h"]).json()
    acs = lambda g: {v["key"]: v["value"] for v in g["values"]}["acs_url"]  # noqa: E731
    assert acs(ga) != acs(go)
    # Switching a provider on for one business (pilot) doesn't switch it on for another.
    _switch_on("okta", a["id"])
    assert client.get(_url(a, "okta"), headers=a["agent"]["h"]).json()["available"] is True
    assert client.get(_url(o, "okta"), headers=o["agent"]["h"]).json()["available"] is False
    # An LDAP sync only touches its own business.
    ad.person("Ana Lee", "ana@examplebank.example", 1)
    _ad_setup(client, a)
    with db.tx() as conn:
        from exaconnect_controller.identity.directory import sync

        sync.run(conn, conn.execute("SELECT * FROM directory_setups WHERE provider = 'active-directory'").fetchone())
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM users WHERE customer_id = %s AND provisioned_by = 'directory'", (o["id"],)
            ).fetchone()["n"]
            == 0
        )
    # Deleting removes the set-up and its SSO connection.
    assert client.delete(_url(a, "okta"), headers=a["agent"]["h"]).status_code == 204
    assert client.get(_url(a, "okta"), headers=a["agent"]["h"]).json()["setup"] is None
