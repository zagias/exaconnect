"""Validate signalling destinations before they reach a SIP URI or config file."""

from __future__ import annotations

import ipaddress
import re

from .common import VoiceError

LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


def host(value: object, *, qualified: bool = True) -> str:
    text = str(value or "").strip().lower()
    address = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    try:
        if "%" in address:
            raise ValueError("Scoped addresses are not remote SIP destinations.")
        return str(ipaddress.ip_address(address))
    except ValueError:
        text = text.rstrip(".")
        labels = text.split(".")
        if len(text) > 253 or (qualified and len(labels) < 2) or any(not LABEL.fullmatch(label) for label in labels):
            raise VoiceError(
                "Give the carrier's host name or IP address, without a SIP scheme, port or parameters.", 422
            ) from None
        return text


def destination(value: object, port: int, transport: str) -> str:
    address = host(value, qualified=False)
    if ":" in address:
        address = f"[{address}]"
    return f"sip:{address}:{port};transport={transport}"
