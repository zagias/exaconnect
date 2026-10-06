"""Secure-cookie sessions: cookie flags, CSRF guard, logout and password rotation."""

import pytest

from .conftest import ADMIN
from .identity_helpers import identity_settings

PORTAL = {"X-Requested-With": "exa-portal"}


@pytest.fixture
def settings(tmp_path):
    return identity_settings(tmp_path)


def _login(client):
    r = client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200, r.text
    return r


def test_login_sets_httponly_cookie_and_still_returns_token(client):
    r = _login(client)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("exa_session=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # cookie_secure=False in this test
    assert r.json()["token"]
    # Bearer still works for the SDK and scripts.
    fresh = client.__class__(client.app)
    assert fresh.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code == 200


def test_cookie_is_secure_by_default(tmp_path):
    from exaconnect_controller.settings import Settings

    assert Settings().cookie_secure is True


def test_cookie_authenticates_reads_and_guards_writes(client):
    _login(client)
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["email"] == ADMIN[0]
    # A write with only the cookie must carry the portal header.
    r = client.post("/api/v1/auth/api-keys", json={"name": "k"})
    assert r.status_code == 403
    r = client.post("/api/v1/auth/api-keys", json={"name": "k"}, headers=PORTAL)
    assert r.status_code == 201, r.text


def test_logout_clears_cookie_and_session(client):
    token = _login(client).json()["token"]
    r = client.post("/api/v1/auth/logout", headers=PORTAL)
    assert r.status_code == 204
    assert 'exa_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_password_change_rotates_cookie(client):
    old = _login(client).json()["token"]
    other = client.__class__(client.app)
    other_token = other.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).json()["token"]
    r = client.post(
        "/api/v1/auth/password", json={"current": ADMIN[1], "new": "a much longer new password"}, headers=PORTAL
    )
    assert r.status_code == 204, r.text
    new = r.cookies.get("exa_session") or client.cookies.get("exa_session")
    assert new and new != old
    assert client.get("/api/v1/auth/me").status_code == 200
    for t in (old, other_token):
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_bearer_password_change_keeps_that_session(client):
    token = _login(client).json()["token"]
    client.cookies.clear()
    h = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/v1/auth/password", json={"current": ADMIN[1], "new": "another long new password"}, headers=h)
    assert r.status_code == 204
    assert client.get("/api/v1/auth/me", headers=h).status_code == 200


def test_websocket_helper_still_authenticates(client):
    from exaconnect_controller.commai.api.inbox import _ws_user

    token = _login(client).json()["token"]

    class _Ws:
        url = type("U", (), {"path": "/api/v1/commai/customers/x/live"})()

    assert _ws_user(_Ws(), f"Bearer {token}").email == ADMIN[0]
