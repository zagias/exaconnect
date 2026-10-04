"""Python SDK for ExaConnect, ExaCarib's connectivity platform.

    from exaconnect import ExaConnect
    exa = ExaConnect("https://connect.exacarib.com")   # key from EXACONNECT_API_KEY
    cid = exa.me()["customer_id"]
    for c in exa.circuits.list(cid):
        print(c["name"], c["status"])

See docs/automation-contract.md in the ExaConnect repository.
"""

from .client import ExaConnect, ExaConnectError

__all__ = ["ExaConnect", "ExaConnectError"]
__version__ = "0.1.0"
