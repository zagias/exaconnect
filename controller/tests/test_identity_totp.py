"""TOTP against the RFC 6238 appendix B test vectors."""

import pytest

from exaconnect_controller.identity import totp

VECTORS = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]
KEYS = {
    "sha1": b"12345678901234567890",
    "sha256": b"12345678901234567890123456789012",
    "sha512": b"1234567890123456789012345678901234567890123456789012345678901234",
}


@pytest.mark.parametrize("t,sha1,sha256,sha512", VECTORS)
def test_rfc6238_vectors(t, sha1, sha256, sha512):
    assert totp.totp(KEYS["sha1"], t, digits=8, alg="sha1") == sha1
    assert totp.totp(KEYS["sha256"], t, digits=8, alg="sha256") == sha256
    assert totp.totp(KEYS["sha512"], t, digits=8, alg="sha512") == sha512


def test_rfc4226_hotp_vectors():
    expected = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
    assert [totp.hotp(KEYS["sha1"], i) for i in range(10)] == expected


def test_verify_window_and_replay():
    secret = totp.new_secret()
    key = totp.decode(secret)
    t = 1_700_000_000
    now_code = totp.totp(key, t)
    step = totp.verify(secret, now_code, at=t)
    assert step == t // 30
    assert totp.verify(secret, totp.totp(key, t - 30), at=t) == step - 1  # one step of clock drift
    assert totp.verify(secret, totp.totp(key, t - 90), at=t) is None
    assert totp.verify(secret, now_code, at=t, after_step=step) is None  # used once already
    assert totp.verify(secret, "12345", at=t) is None
    assert totp.verify(secret, "abcdef", at=t) is None


def test_secret_and_uri():
    secret = totp.new_secret()
    assert len(totp.decode(secret)) == 20
    uri = totp.otpauth_uri(secret, "ana@bank.example")
    assert uri.startswith("otpauth://totp/ExaCarib%3Aana%40bank.example?")
    assert f"secret={secret}" in uri and "issuer=ExaCarib" in uri
