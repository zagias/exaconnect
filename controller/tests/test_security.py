from exaconnect_controller import pki
from exaconnect_controller.security import hash_password, token_hash, verify_password


def test_password_round_trip():
    h = hash_password("s3cret")
    assert h.startswith("scrypt$")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret", "garbage")


def test_token_hash_is_stable():
    assert token_hash("abc") == token_hash("abc") != token_hash("abd")


def test_serial_normalisation_matches_nginx_forms():
    assert pki.serial_hex(0x0ABC) == "ABC"
    assert pki.normalise_serial("0abc") == "ABC"
    assert pki.normalise_serial("0A:BC") == "ABC"
