"""Two-step sign-in: enrol, challenge, recovery codes, replay, throttling, disable."""

import time

import pytest

from exaconnect_controller import db
from exaconnect_controller.identity import totp

from .conftest import ADMIN
from .identity_helpers import identity_settings


@pytest.fixture
def settings(tmp_path):
    return identity_settings(tmp_path)


def _code(secret: str, offset: int = 0) -> str:
    return totp.totp(totp.decode(secret), time.time() + offset)


def _enrol(client, h) -> tuple[str, list[str]]:
    r = client.post("/api/v1/auth/two-step/start", headers=h)
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert client.post("/api/v1/auth/two-step/confirm", json={"code": "000000"}, headers=h).status_code == 400
    r = client.post("/api/v1/auth/two-step/confirm", json={"code": _code(secret)}, headers=h)
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    return secret, codes


def _password(client):
    return client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})


def test_enrol_then_login_needs_second_step(client, admin_headers):
    secret, codes = _enrol(client, admin_headers)
    with db.tx() as conn:
        stored = [r["code_hash"] for r in conn.execute("SELECT code_hash FROM mfa_recovery_codes").fetchall()]
    assert all(c not in stored for c in codes) and all(h.startswith("scrypt$") for h in stored)
    st = client.get("/api/v1/auth/two-step", headers=admin_headers).json()
    assert st["enabled"] and st["recovery_codes_left"] == 10

    client.cookies.clear()
    r = _password(client)
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True and "token" not in body
    assert "set-cookie" not in r.headers
    # The challenge alone is not a session.
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['challenge']}"}).status_code == 401

    r = client.post("/api/v1/auth/login/mfa", json={"challenge": body["challenge"], "code": "123456"})
    assert r.status_code == 401
    # The code used to confirm enrolment can't be replayed; the next step's can.
    r = client.post("/api/v1/auth/login/mfa", json={"challenge": body["challenge"], "code": _code(secret, 30)})
    assert r.status_code == 200, r.text
    assert r.json()["token"] and "exa_session=" in r.headers["set-cookie"]
    assert r.json()["user"]["two_step"] is True
    # Single use.
    again = client.post("/api/v1/auth/login/mfa", json={"challenge": body["challenge"], "code": _code(secret, 30)})
    assert again.status_code == 401


def test_recovery_code_works_once(client, admin_headers):
    _, codes = _enrol(client, admin_headers)
    ch = _password(client).json()["challenge"]
    r = client.post("/api/v1/auth/login/mfa", json={"challenge": ch, "code": codes[0].upper()})
    assert r.status_code == 200, r.text
    ch = _password(client).json()["challenge"]
    assert client.post("/api/v1/auth/login/mfa", json={"challenge": ch, "code": codes[0]}).status_code == 401
    st = client.get("/api/v1/auth/two-step", headers=admin_headers).json()
    assert st["recovery_codes_left"] == 9


def test_challenge_wears_out(client, admin_headers):
    secret, _ = _enrol(client, admin_headers)
    ch = _password(client).json()["challenge"]
    for _ in range(5):
        assert client.post("/api/v1/auth/login/mfa", json={"challenge": ch, "code": "000000"}).status_code == 401
    # Even the right code is refused once the challenge is used up.
    assert client.post("/api/v1/auth/login/mfa", json={"challenge": ch, "code": _code(secret, 30)}).status_code == 401
    with db.tx() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE action = 'login_failed' AND detail->>'reason' = 'two_step'"
        ).fetchone()["n"]
    assert n >= 5


def test_disable_needs_a_code(client, admin_headers):
    secret, _ = _enrol(client, admin_headers)
    assert (
        client.post("/api/v1/auth/two-step/disable", json={"code": "000000"}, headers=admin_headers).status_code == 400
    )
    r = client.post("/api/v1/auth/two-step/disable", json={"code": _code(secret, 30)}, headers=admin_headers)
    assert r.status_code == 204
    r = _password(client)
    assert r.status_code == 200 and r.json()["token"]
    with db.tx() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert "two_step.enable" in actions and "two_step.disable" in actions


def test_new_recovery_codes_replace_old(client, admin_headers):
    secret, old = _enrol(client, admin_headers)
    r = client.post("/api/v1/auth/two-step/recovery-codes", json={"code": _code(secret, 30)}, headers=admin_headers)
    assert r.status_code == 200
    ch = _password(client).json()["challenge"]
    assert client.post("/api/v1/auth/login/mfa", json={"challenge": ch, "code": old[1]}).status_code == 401
    ch = _password(client).json()["challenge"]
    assert (
        client.post("/api/v1/auth/login/mfa", json={"challenge": ch, "code": r.json()["recovery_codes"][0]}).status_code
        == 200
    )
