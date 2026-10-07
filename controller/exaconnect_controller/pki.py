"""The controller's private CA: signs agent client certificates (mTLS) and the
agent-facing server certificate used by the TLS proxy."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CLIENT_DAYS = 365
SERVER_DAYS = 825


class CSRError(ValueError):
    pass


@dataclass
class CA:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @property
    def pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    @property
    def fingerprint(self) -> str:
        return self.cert.fingerprint(hashes.SHA256()).hex()

    def sign_client(self, csr_pem: bytes, common_name: str) -> tuple[bytes, str]:
        """Sign a CSR as a client certificate for common_name. Returns (PEM, serial hex)."""
        try:
            csr = x509.load_pem_x509_csr(csr_pem)
        except ValueError as e:
            raise CSRError("not a PEM certificate request") from e
        if not csr.is_signature_valid:
            raise CSRError("CSR signature is invalid")
        now = dt.datetime.now(dt.UTC)
        serial = x509.random_serial_number()
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(self.cert.subject)
            .public_key(csr.public_key())
            .serial_number(serial)
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=CLIENT_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM), serial_hex(serial)


def serial_hex(serial: int) -> str:
    """Canonical form for comparing with nginx's $ssl_client_serial."""
    return normalise_serial(format(serial, "X"))


def normalise_serial(s: str) -> str:
    return s.strip().upper().replace(":", "").lstrip("0") or "0"


def _write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    tmp.replace(path)


def load_or_create(data_dir: str, sans: list[str]) -> CA:
    """Load the CA from data_dir/pki, creating it on first start. Also (re)writes
    the proxy's server certificate into data_dir/tls."""
    pki = Path(data_dir) / "pki"
    pki.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path, cert_path = pki / "ca.key", pki / "ca.crt"
    if key_path.exists() and cert_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ExaCarib"),
                x509.NameAttribute(NameOID.COMMON_NAME, f"ExaConnect CA {secrets.token_hex(4)}"),
            ]
        )
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        _write(
            key_path,
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ),
            0o600,
        )
        _write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    ca = CA(key=key, cert=cert)
    _write_server_cert(ca, Path(data_dir) / "tls", sans)
    return ca


def _server_names(path: Path) -> set[str]:
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (ValueError, x509.ExtensionNotFound):
        return set()
    return {str(n.value) for n in alt}


def _write_server_cert(ca: CA, tls_dir: Path, sans: list[str]) -> None:
    tls_dir.mkdir(parents=True, exist_ok=True)
    if (tls_dir / "server.crt").exists() and (tls_dir / "server.key").exists():
        # Reissue only when a name is missing (a new public gateway name, ADR 0024). Agents
        # trust the CA, not this certificate, so a reissue needs nothing from them.
        if set(sans) <= _server_names(tls_dir / "server.crt"):
            return
    key = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    for s in sans:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(s)))
        except ValueError:
            alt.append(x509.DNSName(s))
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0] if sans else "controller")]))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca.key, hashes.SHA256())
    )
    # The proxy's master process reads this as root.
    _write(
        tls_dir / "server.key",
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        0o600,
    )
    _write(tls_dir / "server.crt", cert.public_bytes(serialization.Encoding.PEM) + ca.pem, 0o644)
    _write(tls_dir / "ca.crt", ca.pem, 0o644)
