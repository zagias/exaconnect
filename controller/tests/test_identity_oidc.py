"""Google and Microsoft sign-in through the gateway, against a fake issuer."""

import time
import urllib.parse

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from exaconnect_controller import db

from .conftest import ADMIN
from .identity_helpers import CLIENT, ISSUER, fake_issuer, identity_settings, sign_in_via


@pytest.fixture
def settings(tmp_path):
    return identity_settings(tmp_path)


@pytest.fixture(name="issuer")
def _issuer(monkeypatch):
    yield from fake_issuer(monkeypatch)


def test_providers_and_start_redirect(client, issuer):
    p = client.get("/api/v1/auth/providers").json()
    assert [x["id"] for x in p["providers"]] == ["google", "microsoft"]
    r = client.get("/api/v1/auth/oidc/start?idp=google&next=/sites", follow_redirects=False)
    assert r.status_code == 302
    loc = urllib.parse.urlparse(r.headers["location"])
    q = dict(urllib.parse.parse_qsl(loc.query))
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/protocol/openid-connect/auth"
    assert q["kc_idp_hint"] == "google" and q["code_challenge_method"] == "S256" and q["client_id"] == CLIENT
    assert q["nonce"] and q["state"] and q["redirect_uri"].endswith("/api/v1/auth/oidc/callback")
    assert client.get("/api/v1/auth/oidc/start?idp=someone-else", follow_redirects=False).status_code == 404


def test_no_buttons_when_not_configured(tmp_path):
    from fastapi.testclient import TestClient

    from exaconnect_controller.main import create_app

    s = identity_settings(tmp_path, oidc_issuer="", database_url="")
    with TestClient(create_app(s)) as c:
        assert c.get("/api/v1/auth/providers").json()["providers"] == []
        assert c.get("/api/v1/auth/oidc/start?idp=google", follow_redirects=False).status_code == 404


def test_existing_user_signs_in_with_google(client, issuer):
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google&next=/sites", ADMIN[0].upper())
    assert r.status_code == 302 and r.headers["location"] == "/sites"
    assert "exa_session=" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").json()["email"] == ADMIN[0]
    with db.tx() as conn:
        row = conn.execute("SELECT detail FROM audit_log WHERE action = 'login' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["detail"]["via"] == "oidc:google"


def test_unknown_email_is_not_created(client, issuer):
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=microsoft", "stranger@corp.example")
    assert r.status_code == 302 and r.headers["location"].startswith("/?signin_error=")
    assert "set-cookie" not in r.headers
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE email = 'stranger@corp.example'").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'login_failed' AND detail->>'reason' = 'no_account'"
        ).fetchone()


def test_unverified_email_refused(client, issuer):
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google", ADMIN[0], verified=False)
    assert "signin_error" in r.headers["location"]


def test_state_is_single_use(client, issuer):
    r, q = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google", ADMIN[0])
    assert "signin_error" not in r.headers["location"]
    again = client.get(f"/api/v1/auth/oidc/callback?state={q['state']}&code=thecode", follow_redirects=False)
    assert "signin_error" in again.headers["location"]


@pytest.mark.parametrize(
    "bad",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example/realms/x"},
        {"exp": int(time.time()) - 3600},
        {"nonce": "not-the-nonce"},
        {"identity_provider": "microsoft"},  # asked for google
    ],
)
def test_bad_id_tokens_refused(client, issuer, bad):
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google", ADMIN[0], extra=bad)
    assert "signin_error" in r.headers["location"], bad
    assert client.get("/api/v1/auth/me").status_code == 401


def test_wrong_signature_refused(client, issuer):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer.tamper = lambda tok: issuer.sign(issuer.claims, key=other)
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google", ADMIN[0])
    assert "signin_error" in r.headers["location"]


def test_alg_none_refused(client, issuer):
    def none_alg(tok):
        import base64
        import json

        head = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
        return f"{head}.{tok.split('.')[1]}."

    issuer.tamper = none_alg
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google", ADMIN[0])
    assert "signin_error" in r.headers["location"]


def test_open_redirect_blocked(client, issuer):
    r, _ = sign_in_via(client, issuer, "/api/v1/auth/oidc/start?idp=google&next=//evil.example", ADMIN[0])
    assert r.headers["location"] == "/"
