#!/usr/bin/env python3
"""User-run private provisioning only: no provider/service/network action.

Do not run through a model-visible terminal. It writes a private Twilio
origination URL and a private verifier include; stdout never contains either.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from pathlib import Path

HEADER = "X-Jibsy-Trunk-Token"
TOKEN = re.compile(r"[0-9a-f]{64}")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def verifier(set_id: int, token: str) -> str:
    if type(set_id) is not int or not 1 <= set_id <= 2_147_483_647 or not TOKEN.fullmatch(token):
        raise ValueError("Invalid private origin material.")
    inner = hashlib.sha256(token.encode("ascii")).hexdigest()
    return hashlib.sha256(f"jibsy-origin-v1:{set_id}:{inner}".encode("ascii")).hexdigest()


def accepts(stored: str | None, set_id: int, values: list[str], *, tls: bool, source_allowed: bool) -> bool:
    """Offline/native-fixture oracle; the live SBC enforces the same contract."""
    if not tls or not source_allowed or len(values) != 1 or not stored:
        return False
    if not isinstance(values[0], str) or not TOKEN.fullmatch(values[0]):
        return False
    return hmac.compare_digest(stored, verifier(set_id, values[0]))


def sip_host(value: str) -> str:
    text = value.strip().lower()
    try:
        address = ipaddress.ip_address(text)
        return f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        labels = text.split(".")
        if len(text) > 253 or len(labels) < 2 or any(not LABEL.fullmatch(label) for label in labels):
            raise ValueError("Use the actual public SIP hostname/IP without scheme, port or query.") from None
        return text


def private_directory(path: Path):
    if os.geteuid() != 0 or not path.is_absolute():
        raise PermissionError("Use the private operator root process and an absolute private directory.")
    for parent in (path.parent, *path.parent.parents):
        metadata = parent.stat()
        if parent.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise PermissionError("Private material must have root-owned non-writable parents.")
    path.mkdir(mode=0o700, exist_ok=False)


def provision(set_id: int, public_host: str, port: int, directory: Path):
    if type(set_id) is not int or not 1 <= set_id <= 2_147_483_647 or not 1 <= port <= 65535:
        raise ValueError("Invalid dispatcher set or SIP port.")
    host = sip_host(public_host)
    private_directory(directory)
    token = secrets.token_hex(32)
    claim = verifier(set_id, token)
    url = f"sip:{host}:{port};transport=tls?{HEADER}={token}\n"
    assignment = '$sht(protectedauth=>set:' + str(set_id) + ') = "' + claim + '";\n'
    for name, content in (("origination-url.txt", url), ("private-origination-auth.cfg", assignment)):
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    # No token, verifier, URL or hash is returned to callers/logs.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-id", type=int, required=True)
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--port", type=int, default=5061)
    parser.add_argument("--private-directory", type=Path, required=True)
    args = parser.parse_args()
    provision(args.set_id, args.public_host, args.port, args.private_directory)
    print("Private origination files prepared. No provider or service changed.")


if __name__ == "__main__":
    main()
