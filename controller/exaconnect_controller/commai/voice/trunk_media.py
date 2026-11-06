"""Typed carrier media policy; configuration never establishes operational proof."""

from .common import VoiceError

LEGACY = "LEGACY"
SRTP_REQUIRED = "SRTP_REQUIRED"
SUITE = "AES_CM_128_HMAC_SHA1_80"


class PolicyError(VoiceError):
    def __init__(self, message="Carrier candidates have incompatible media policies."):
        super().__init__(message, 422)


def mode(outbound: dict, adapter: str) -> str:
    if not isinstance(outbound, dict):
        raise PolicyError("The carrier's encrypted audio policy is invalid.")
    required = outbound.get("srtp_required", False)
    if type(required) is not bool:
        raise PolicyError("The carrier's encrypted audio policy is invalid.")
    if required and (adapter != "sip" or outbound.get("transport") != "tls"):
        raise PolicyError("Required encrypted audio needs a real SIP trunk with TLS transport.")
    return SRTP_REQUIRED if required else LEGACY


def candidates(rows: list[dict]) -> str:
    modes = {mode(row.get("outbound") or {}, row.get("adapter", "")) for row in rows}
    if len(modes) > 1:
        raise PolicyError()
    return next(iter(modes), LEGACY)
