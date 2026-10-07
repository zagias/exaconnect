"""Python SDK for ExaConnect, ExaCarib's connectivity platform.

    from exaconnect import ExaConnect
    exa = ExaConnect("https://connect.exacarib.com")   # key from EXACONNECT_API_KEY
    cid = exa.me()["customer_id"]
    for c in exa.circuits.list(cid):
        print(c["name"], c["status"])

See docs/automation-contract.md in the ExaConnect repository.
"""

from .client import ExaConnect, ExaConnectError, verify_event

# The product is "ExaCarib Connect"; ExaConnect stays as the original name.
Connect = ExaConnect
from .commai import verify_webhook

__all__ = ["Connect", "ExaConnect", "ExaConnectError", "verify_event", "verify_webhook"]
__version__ = "0.1.0"
