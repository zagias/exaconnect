"""Passkeys as the second step, with a software authenticator made in the test."""

import base64
import hashlib
import json
import os

import pytest

pytest.importorskip("webauthn")
import cbor2  # noqa: E402  (installed with webauthn)
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from .conftest import ADMIN  # noqa: E402
from .identity_helpers import PUBLIC, identity_settings  # noqa: E402

RP_ID = "connect.example.test"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    def __init__(self, origin: str = PUBLIC) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.origin = origin

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict) -> dict:
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth_data = (
            hashlib.sha256(options["rp"]["id"].encode()).digest()
            + bytes([0x45])  # user present, user verified, attested credential data
            + self.count.to_bytes(4, "big")
            + bytes(16)
            + len(self.cred_id).to_bytes(2, "big")
            + self.cred_id
            + cose
        )
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cd = self._client_data("webauthn.create", options["challenge"])
        return {
            "id": b64(self.cred_id),
            "rawId": b64(self.cred_id),
            "type": "public-key",
            "response": {"clientDataJSON": b64(cd), "attestationObject": b64(att)},
        }

    def get(self, options: dict) -> dict:
        self.count += 1
        auth_data = hashlib.sha256(options["rpId"].encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        cd = self._client_data("webauthn.get", options["challenge"])
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64(self.cred_id),
            "rawId": b64(self.cred_id),
            "type": "public-key",
            "response": {"clientDataJSON": b64(cd), "authenticatorData": b64(auth_data), "signature": b64(sig)},
        }


@pytest.fixture
def settings(tmp_path):
    return identity_settings(tmp_path)


def _add(client, h, device):
    r = client.post("/api/v1/auth/passkeys/options", headers=h)
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts["rp"]["id"] == RP_ID
    r = client.post(
        "/api/v1/auth/passkeys",
        headers=h,
        json={"ticket": r.json()["ticket"], "credential": device.create(opts), "name": "Laptop"},
    )
    return r


def _password(client):
    return client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).json()


def test_passkey_second_step(client, admin_headers):
    device = SoftAuthenticator()
    r = _add(client, admin_headers, device)
    assert r.status_code == 201, r.text
    assert len(r.json()["recovery_codes"]) == 10
    st = client.get("/api/v1/auth/two-step", headers=admin_headers).json()
    assert st["passkeys_available"] and [p["name"] for p in st["passkeys"]] == ["Laptop"]

    out = _password(client)
    assert out["mfa_required"] and "passkey" in out["methods"] and "totp" not in out["methods"]
    r = client.post("/api/v1/auth/login/passkey/options", json={"challenge": out["challenge"]})
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts["allowCredentials"][0]["id"] == b64(device.cred_id)
    r = client.post(
        "/api/v1/auth/login/passkey",
        json={"challenge": out["challenge"], "ticket": r.json()["ticket"], "credential": device.get(opts)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["token"] and r.json()["user"]["two_step"] is True


def test_wrong_device_or_origin_refused(client, admin_headers):
    device = SoftAuthenticator()
    assert _add(client, admin_headers, device).status_code == 201
    # A different key with the same credential id.
    impostor = SoftAuthenticator()
    impostor.cred_id = device.cred_id
    out = _password(client)
    r = client.post("/api/v1/auth/login/passkey/options", json={"challenge": out["challenge"]}).json()
    bad = client.post(
        "/api/v1/auth/login/passkey",
        json={"challenge": out["challenge"], "ticket": r["ticket"], "credential": impostor.get(r["options"])},
    )
    assert bad.status_code == 401
    # A phishing origin.
    phish = SoftAuthenticator(origin="https://connect.example.test.evil.example")
    phish.key, phish.cred_id = device.key, device.cred_id
    r = client.post("/api/v1/auth/login/passkey/options", json={"challenge": out["challenge"]}).json()
    bad = client.post(
        "/api/v1/auth/login/passkey",
        json={"challenge": out["challenge"], "ticket": r["ticket"], "credential": phish.get(r["options"])},
    )
    assert bad.status_code == 401
    # Registration from the wrong origin is refused too.
    assert _add(client, admin_headers, SoftAuthenticator(origin="https://evil.example")).status_code == 400


def test_removing_last_passkey_turns_two_step_off(client, admin_headers):
    assert _add(client, admin_headers, SoftAuthenticator()).status_code == 201
    pk = client.get("/api/v1/auth/two-step", headers=admin_headers).json()["passkeys"][0]
    assert client.delete(f"/api/v1/auth/passkeys/{pk['id']}", headers=admin_headers).status_code == 204
    assert _password(client).get("token")
    assert client.get("/api/v1/auth/two-step", headers=admin_headers).json()["recovery_codes_left"] == 0
