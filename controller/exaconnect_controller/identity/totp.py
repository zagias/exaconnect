"""Time-based one-time passwords (RFC 6238 over RFC 4226 HOTP), standard library only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP = 30
DIGITS = 6
_ALGS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


def hotp(key: bytes, counter: int, digits: int = DIGITS, alg: str = "sha1") -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), _ALGS[alg]).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**digits).zfill(digits)


def totp(key: bytes, at: float | None = None, digits: int = DIGITS, alg: str = "sha1", step: int = STEP) -> str:
    t = time.time() if at is None else at
    return hotp(key, int(t // step), digits, alg)


def new_secret() -> str:
    """A 160-bit secret in base32, as authenticator apps expect it."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def decode(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def otpauth_uri(secret: str, account: str, issuer: str = "ExaCarib") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP}
    )
    return f"otpauth://totp/{label}?{q}"


def verify(
    secret: str, code: str, at: float | None = None, window: int = 1, after_step: int | None = None
) -> int | None:
    """The time step the code matches (within +/- window steps), or None.
    Steps at or before `after_step` are refused, so a code works only once."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return None
    key = decode(secret)
    now = int((time.time() if at is None else at) // STEP)
    for step in range(now - window, now + window + 1):
        if after_step is not None and step <= after_step:
            continue
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None
