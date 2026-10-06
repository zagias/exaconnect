"""SIP provider interface (ADR 0021).

ExaCarib has no SIP provider account yet (Dudley's choice, phase 3), so the
working provider is `SimulatedProvider`: numbers from the fictional range
+1 868 555 0100 to 0199, port orders that move only when an admin moves them,
and test calls that succeed unless a fault is injected. `HttpSipProvider` is
the placeholder for the real one: it reads its settings from the
environment and refuses to act until they are set.

Every call that creates something takes an idempotency key. Asked twice with
the same key, a provider must return the same result, never a second number:
that is what lets a failed provisioning step retry safely.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import psycopg

from .common import VoiceError, digits

SIM_PREFIX = "+186855501"  # +1 868 555 01xx: fictional, never a real subscriber


class ProviderError(VoiceError):
    """The provider refused or failed. Safe to retry with the same key."""

    def __init__(self, message: str):
        super().__init__(message, 502)


class NotConfigured(ProviderError):
    pass


class SipProvider:
    name = "base"
    live = False

    def order_number(self, conn: psycopg.Connection, customer_id: Any, key: str, area: str = "868") -> dict:
        """-> {"e164", "ref"}; the same key always returns the same number."""
        raise NotImplementedError

    def release_number(self, conn: psycopg.Connection, e164: str) -> None:
        raise NotImplementedError

    def submit_port(self, conn: psycopg.Connection, customer_id: Any, e164: str, losing_carrier: str, key: str) -> dict:
        """-> {"ref", "status"}"""
        raise NotImplementedError

    def test_call(self, conn: psycopg.Connection, e164: str) -> dict:
        """Place a short test call to the number. -> {"ok": bool, "detail": str}"""
        raise NotImplementedError

    def register_emergency_address(self, conn: psycopg.Connection, e164: str, address: dict) -> str:
        """-> status: 'pending' | 'registered' | 'rejected'. Island rules are phase 3."""
        raise NotImplementedError

    def supplier_rate(self, destination: str) -> Decimal:
        """What the provider charges ExaCarib per minute (used to estimate margin)."""
        raise NotImplementedError


# Faults the tests (and the lab) inject: {"order_number": 2} fails the next two calls.
FAULTS: dict[str, int] = {}


def _fault(op: str) -> None:
    left = FAULTS.get(op, 0)
    if left > 0:
        FAULTS[op] = left - 1
        raise ProviderError(f"Simulated provider failure on {op}.")


class SimulatedProvider(SipProvider):
    name = "simulated"
    live = False

    def order_number(self, conn, customer_id, key, area="868"):
        row = conn.execute("SELECT e164 FROM voice_sim_provider_numbers WHERE idempotency_key = %s", (key,)).fetchone()
        if row:
            return {"e164": row["e164"], "ref": f"sim:{row['e164']}"}
        _fault("order_number")
        taken = {
            r["e164"] for r in conn.execute("SELECT e164 FROM voice_sim_provider_numbers WHERE NOT released").fetchall()
        }
        for i in range(100):
            e164 = f"{SIM_PREFIX}{i:02d}"
            if e164 in taken:
                continue
            conn.execute(
                """INSERT INTO voice_sim_provider_numbers (e164, customer_id, idempotency_key) VALUES (%s, %s, %s)
                   ON CONFLICT (e164) DO UPDATE SET customer_id = EXCLUDED.customer_id,
                     idempotency_key = EXCLUDED.idempotency_key, released = false, created_at = now()""",
                (e164, customer_id, key),
            )
            return {"e164": e164, "ref": f"sim:{e164}"}
        raise ProviderError("The simulated number range (+1 868 555 0100-0199) is used up.")

    def release_number(self, conn, e164):
        conn.execute("UPDATE voice_sim_provider_numbers SET released = true WHERE e164 = %s", (e164,))

    def submit_port(self, conn, customer_id, e164, losing_carrier, key):
        _fault("submit_port")
        return {"ref": f"simport:{key}", "status": "submitted"}

    def test_call(self, conn, e164):
        try:
            _fault("test_call")
        except ProviderError as e:
            return {"ok": False, "detail": str(e)}
        return {"ok": True, "detail": "Simulated test call answered and audio heard both ways."}

    def register_emergency_address(self, conn, e164, address):
        return "pending"

    def supplier_rate(self, destination):
        d = digits(destination)
        if d.startswith("1868"):
            return Decimal("0.0080")
        if d.startswith("1"):
            return Decimal("0.0120")
        return Decimal("0.1500")


class HttpSipProvider(SipProvider):
    """The real SIP provider. Not live until EXA_SIP_PROVIDER_URL and
    EXA_SIP_PROVIDER_KEY are set for an account Dudley has opened (phase 3).
    The provider and its API are not chosen yet, so every call refuses."""

    name = "sip"
    live = True

    def __init__(self) -> None:
        self.url = os.environ.get("EXA_SIP_PROVIDER_URL", "")
        self.key = os.environ.get("EXA_SIP_PROVIDER_KEY", "")

    def _refuse(self) -> None:
        if not self.url or not self.key:
            raise NotConfigured("The SIP provider is not set up yet (phase 3). Use the simulated provider.")
        raise NotConfigured("The SIP provider's API adapter is written when the provider is chosen (phase 3).")

    def order_number(self, conn, customer_id, key, area="868"):
        self._refuse()

    def release_number(self, conn, e164):
        self._refuse()

    def submit_port(self, conn, customer_id, e164, losing_carrier, key):
        self._refuse()

    def test_call(self, conn, e164):
        self._refuse()

    def register_emergency_address(self, conn, e164, address):
        self._refuse()

    def supplier_rate(self, destination):
        self._refuse()


def get() -> SipProvider:
    """EXA_SIP_PROVIDER picks the provider: "simulated" (the default) or "sip"."""
    return HttpSipProvider() if os.environ.get("EXA_SIP_PROVIDER", "simulated") == "sip" else SimulatedProvider()
