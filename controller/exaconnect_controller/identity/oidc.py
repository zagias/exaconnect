"""OpenID Connect from the controller to the sign-in gateway (Keycloak):
authorization code with PKCE, and ID token checks (RS256 against the issuer's
JWKS; iss, aud, exp and nonce). Standard library and `cryptography` only."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

LEEWAY_S = 60
CACHE_S = 3600


class OidcError(Exception):
    """Something about the gateway's answer we won't accept. The message is safe to show."""


def _http(method: str, url: str, data: dict | None = None, headers: dict | None = None) -> dict:
    """JSON over HTTPS. Tests replace this function with a fake issuer."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method, headers={"Accept": "application/json", **(headers or {})}
    )
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (configured issuer)
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise OidcError(f"The sign-in gateway answered {e.code}.") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise OidcError("The sign-in gateway could not be reached.") from e


_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def _cached(key: str, fetch, fresh: bool = False) -> Any:
    with _lock:
        hit = _cache.get(key)
    if hit and not fresh and hit[0] > time.time():
        return hit[1]
    value = fetch()
    with _lock:
        _cache[key] = (time.time() + CACHE_S, value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def configured(settings) -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id and settings.public_url)


def discovery(issuer: str) -> dict:
    doc = _cached(f"disc:{issuer}", lambda: _http("GET", f"{issuer}/.well-known/openid-configuration"))
    if doc.get("issuer") != issuer:
        raise OidcError("The sign-in gateway's discovery document names a different issuer.")
    return doc


def redirect_uri(settings) -> str:
    return f"{settings.public_url}/api/v1/auth/oidc/callback"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pkce() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def authorize_url(
    settings, state: str, nonce: str, challenge: str, idp_hint: str | None, prompt: str | None = None
) -> str:
    doc = discovery(settings.oidc_issuer)
    q = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri(settings),
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if idp_hint:
        q["kc_idp_hint"] = idp_hint
    if prompt:
        q["prompt"] = prompt
    return f"{doc['authorization_endpoint']}?{urllib.parse.urlencode(q)}"


def exchange(settings, code: str, verifier: str) -> dict:
    doc = discovery(settings.oidc_issuer)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(settings),
        "client_id": settings.oidc_client_id,
        "code_verifier": verifier,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret
    tokens = _http("POST", doc["token_endpoint"], data)
    if not tokens.get("id_token"):
        raise OidcError("The sign-in gateway did not return an ID token.")
    return tokens


def _key(jwks_uri: str, kid: str | None) -> rsa.RSAPublicKey:
    for fresh in (False, True):  # an unknown kid means the gateway rotated keys
        keys = _cached(f"jwks:{jwks_uri}", lambda: _http("GET", jwks_uri), fresh=fresh).get("keys", [])
        for k in keys:
            if k.get("kty") == "RSA" and (kid is None or k.get("kid") == kid) and k.get("use", "sig") == "sig":
                n = int.from_bytes(_unb64url(k["n"]), "big")
                e = int.from_bytes(_unb64url(k["e"]), "big")
                return rsa.RSAPublicNumbers(e, n).public_key()
    raise OidcError("The ID token was signed with a key the sign-in gateway does not publish.")


def verify_id_token(settings, id_token: str, nonce: str, now: float | None = None) -> dict:
    try:
        head_b64, body_b64, sig_b64 = id_token.split(".")
        header = json.loads(_unb64url(head_b64))
        claims = json.loads(_unb64url(body_b64))
        sig = _unb64url(sig_b64)
    except (ValueError, TypeError) as e:
        raise OidcError("The ID token is not a valid JWT.") from e
    if header.get("alg") != "RS256":
        raise OidcError("The ID token is not signed with RS256.")
    doc = discovery(settings.oidc_issuer)
    key = _key(doc["jwks_uri"], header.get("kid"))
    try:
        key.verify(sig, f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as e:
        raise OidcError("The ID token's signature is not valid.") from e
    t = time.time() if now is None else now
    if claims.get("iss") != settings.oidc_issuer:
        raise OidcError("The ID token is from a different issuer.")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if settings.oidc_client_id not in auds:
        raise OidcError("The ID token is for a different client.")
    if len(auds) > 1 and claims.get("azp") != settings.oidc_client_id:
        raise OidcError("The ID token is for a different client.")
    if not isinstance(claims.get("exp"), int | float) or claims["exp"] < t - LEEWAY_S:
        raise OidcError("The ID token has expired.")
    if isinstance(claims.get("iat"), int | float) and claims["iat"] > t + LEEWAY_S:
        raise OidcError("The ID token was issued in the future.")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("The ID token does not match this sign-in.")
    return claims
