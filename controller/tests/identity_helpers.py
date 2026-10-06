"""Shared by the identity tests: settings with cookies over plain HTTP and a
fake OpenID Connect issuer (RSA key made in the test, HTTP calls patched)."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from exaconnect_controller.identity import oidc
from exaconnect_controller.settings import Settings

from .conftest import ADMIN, DB_URL, PROXY_SECRET

ISSUER = "https://id.example.test/realms/exacarib"
CLIENT = "exacarib-connect"
PUBLIC = "https://connect.example.test"


def identity_settings(tmp_path, **kw) -> Settings:
    base = dict(
        database_url=DB_URL,
        data_dir=str(tmp_path / "data"),
        proxy_secret=PROXY_SECRET,
        admin_email=ADMIN[0],
        admin_password=ADMIN[1],
        routing_interval_s=0,
        cookie_secure=False,
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT,
        oidc_client_secret="",
        public_url=PUBLIC,
        oidc_idps="google,microsoft",
        keycloak_admin_client_id="",
        keycloak_admin_client_secret="",
    )
    base.update(kw)
    return Settings(**base)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FakeIssuer:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.claims: dict = {}
        self.challenge = ""
        self.tamper = None  # a function(token) -> token
        self.token_calls = 0

    def jwks(self) -> dict:
        pub = self.key.public_key().public_numbers()
        n = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": b64(n),
                    "e": b64(pub.e.to_bytes(3, "big")),
                }
            ]
        }

    def sign(self, claims: dict, alg: str = "RS256", key=None) -> str:
        head = b64(json.dumps({"alg": alg, "kid": self.kid, "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        sig = (key or self.key).sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{b64(sig)}"

    def http(self, method: str, url: str, data=None, headers=None) -> dict:
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
            }
        if url.endswith("/certs"):
            return self.jwks()
        if url.endswith("/token"):
            self.token_calls += 1
            assert data["grant_type"] == "authorization_code"
            assert data["redirect_uri"] == f"{PUBLIC}/api/v1/auth/oidc/callback"
            digest = b64(hashlib.sha256(data["code_verifier"].encode()).digest())
            if digest != self.challenge:
                raise oidc.OidcError("The sign-in gateway answered 400.")
            token = self.sign(self.claims)
            return {"id_token": self.tamper(token) if self.tamper else token, "access_token": "x"}
        raise AssertionError(url)

    def pem(self) -> bytes:
        return self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )


def fake_issuer(monkeypatch):
    """Use as: @pytest.fixture(name="issuer") def _issuer(monkeypatch): yield from fake_issuer(monkeypatch)"""
    fake = FakeIssuer()
    oidc.clear_cache()
    monkeypatch.setattr(oidc, "_http", fake.http)
    yield fake
    oidc.clear_cache()


def sign_in_via(
    client,
    fake: FakeIssuer,
    start_path: str,
    email: str,
    *,
    idp_claim: str | None = None,
    verified: bool = True,
    extra: dict | None = None,
    follow_start: bool = True,
):
    """Run the redirect dance: start -> (gateway) -> callback. Returns the callback response."""
    r = client.get(start_path, follow_redirects=False) if follow_start else start_path
    assert r.status_code == 302, r.text
    loc = urllib.parse.urlparse(r.headers["location"])
    q = dict(urllib.parse.parse_qsl(loc.query))
    fake.challenge = q["code_challenge"]
    now = int(time.time())
    fake.claims = {
        "iss": ISSUER,
        "aud": CLIENT,
        "azp": CLIENT,
        "sub": "abc",
        "iat": now,
        "exp": now + 300,
        "nonce": q["nonce"],
        "email": email,
        "email_verified": verified,
        "identity_provider": idp_claim if idp_claim is not None else q.get("kc_idp_hint"),
        **(extra or {}),
    }
    return client.get(f"/api/v1/auth/oidc/callback?state={q['state']}&code=thecode", follow_redirects=False), q
