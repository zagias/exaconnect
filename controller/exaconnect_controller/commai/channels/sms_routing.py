"""SMS routing across more than one carrier (ADR 0023).

ExaCarib keeps a route table: for each destination country, a primary and a
fallback SMS carrier, with the cost of each per message segment. Carriers are
declared in the go-live registry (kind "carrier") and start off.

The SMS channel is replaced here by ``RoutedSms``, which keeps phase 2's rules
(account live, opt-out words, daily country limits) and adds the country rules
(countries.py). When a route exists for the destination it is used; when none
exists the business's own SMS account provider sends, as before.

No duplicate sends. The durable send job (inbox ``message.send``) already never
sends a message that is no longer queued. On top of that, every hand-off to a
carrier is recorded in ``sms_route_attempts`` and uses the message id as the
carrier's idempotency key:

- ``accepted``: the carrier took it. Never sent again.
- ``refused``: the carrier said no and did not send (``Refused``). The next
  carrier in the route is tried at once: this is failover.
- ``unknown``: the carrier may have taken it (a timeout or a broken
  connection). The job retries later, **on that carrier only**, with the same
  idempotency key, and never fails over, because the first carrier may already
  have delivered it.
"""

from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import channels, golive, usage
from . import countries, messaging, providers

CARRIER_CRITERIA = {
    "agreement": "Commercial agreement and rates agreed with the carrier (not needed for a simulator).",
    "idempotency": "The carrier's handling of a repeated request with the same reference is confirmed, so a "
    "retry after a timeout cannot send twice.",
    "receipts": "Delivery receipts mapped to CommAI statuses and tested.",
}


class Refused(providers.ProviderError):
    """The carrier refused and did not send: safe to try another carrier."""


class Carrier:
    key = ""
    label = ""
    simulated = False

    def missing(self) -> list[str]:
        return []

    def send(self, conn: psycopg.Connection, account: dict, to: str, body: str, *, ref: str) -> str:
        """Send one SMS from the account's number. `ref` is the idempotency key.
        Raise Refused when nothing was sent, ProviderError when it is unknown."""
        raise NotImplementedError


class SimulatedCarrier(Carrier):
    """Works with no account. Sends land in sim_channel_outbox. A repeated `ref`
    returns the first send's reference without sending again, as a carrier with
    an idempotency key would. Faults come from sms_sim_faults."""

    simulated = True

    def __init__(self, key: str, label: str):
        self.key, self.label = key, label

    def send(self, conn: psycopg.Connection, account: dict, to: str, body: str, *, ref: str) -> str:
        fault = conn.execute("SELECT mode FROM sms_sim_faults WHERE carrier = %s", (self.key,)).fetchone()
        mode = fault["mode"] if fault else ""
        if mode == "refuse":
            raise Refused(f"{self.label} refused the message (simulated fault).")
        if mode == "timeout":
            raise providers.ProviderError(f"{self.label} did not answer in time (simulated fault, nothing sent).")
        seen = conn.execute(
            """SELECT provider_ref FROM sim_channel_outbox WHERE channel = 'sms' AND headers->>'carrier' = %s
               AND headers->>'ref' = %s""",
            (self.key, ref),
        ).fetchone()
        if seen:
            return seen["provider_ref"]
        pref = f"{self.key}-{secrets.token_hex(10)}"
        conn.execute(
            """INSERT INTO sim_channel_outbox (customer_id, account_id, channel, to_address, body, headers,
                                                provider_ref)
               VALUES (%s, %s, 'sms', %s, %s, %s, %s)""",
            (account["customer_id"], account["id"], to, body, Jsonb({"carrier": self.key, "ref": ref}), pref),
        )
        if mode == "timeout_after_send":
            raise providers.ProviderError(f"{self.label} took the message but the answer was lost (simulated fault).")
        return pref


class TwilioCarrier(Carrier):
    """Twilio as an SMS carrier. NOT live until EXA_TWILIO_ACCOUNT_SID and
    EXA_TWILIO_AUTH_TOKEN are set (see providers.Twilio). Twilio's Messages API
    has no idempotency key: a timeout is treated as unknown and never failed over."""

    key = "sms-twilio"
    label = "Twilio SMS"

    def missing(self) -> list[str]:
        return providers.Twilio().missing({"settings": {}})

    def send(self, conn: psycopg.Connection, account: dict, to: str, body: str, *, ref: str) -> str:
        if self.missing():
            raise Refused("Twilio is not configured on the server.")
        try:
            return providers.Twilio().send_text({**account, "channel": "sms", "settings": {}}, to, body, ref=ref)
        except providers.ProviderError as e:
            if str(e).startswith("Twilio answered 4"):
                raise Refused(str(e)) from e  # a 4xx: Twilio did not accept it
            raise


CARRIERS: dict[str, Carrier] = {
    c.key: c
    for c in (
        SimulatedCarrier("sms-sim-a", "Simulated SMS carrier A"),
        SimulatedCarrier("sms-sim-b", "Simulated SMS carrier B"),
        TwilioCarrier(),
    )
}
for _c in CARRIERS.values():
    golive.declare(
        "carrier",
        _c.key,
        _c.label,
        CARRIER_CRITERIA,
        {"service": "sms", "simulated": _c.simulated},
    )


# ---- routes ------------------------------------------------------------------------


def route_for(conn: psycopg.Connection, country: str) -> dict | None:
    return conn.execute("SELECT * FROM sms_routes WHERE country = %s", (country,)).fetchone()


def legs(conn: psycopg.Connection, route: dict, customer_id: Any) -> list[tuple[str, Decimal]]:
    """The route's carriers that are switched on for this business, in order, with their cost."""
    out = []
    for key, cost in (
        (route["primary_carrier"], route["primary_cost"]),
        (route["fallback_carrier"], route["fallback_cost"]),
    ):
        if key and key in CARRIERS and golive.enabled(conn, "carrier", key, customer_id):
            out.append((key, Decimal(cost)))
    return out


def segments(body: str) -> int:
    """SMS segments: GSM-7 text is 160 characters (153 per part when split), anything else 70 (67)."""
    gsm = all(ord(ch) < 128 for ch in body)
    single, part = (160, 153) if gsm else (70, 67)
    n = len(body)
    return 1 if n <= single else -(-n // part)


def _attempt(conn, msg: dict, country: str, carrier: str, outcome: str, ref: str = "", error: str = "", cost=0) -> None:
    conn.execute(
        """INSERT INTO sms_route_attempts (customer_id, message_id, country, carrier, outcome, provider_ref,
                                          error, cost)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (msg["customer_id"], msg["id"], country, carrier, outcome, ref, error[:500], cost),
    )


def send_routed(conn: psycopg.Connection, account: dict, msg: dict, to: str, country: str, route: dict) -> str:
    """Hand the message to the route's carriers; see the module notes. Returns the provider reference."""
    history = conn.execute(
        "SELECT carrier, outcome, provider_ref FROM sms_route_attempts WHERE message_id = %s ORDER BY id", (msg["id"],)
    ).fetchall()
    for h in history:
        if h["outcome"] == "accepted":
            return h["provider_ref"]  # already with a carrier: never again
    available = legs(conn, route, msg["customer_id"])
    if history and history[-1]["outcome"] == "unknown":
        # The last carrier may have it. Only that carrier, with the same key.
        stuck = history[-1]["carrier"]
        available = [(k, c) for k, c in available if k == stuck] or [
            (stuck, Decimal(route["primary_cost"] if stuck == route["primary_carrier"] else route["fallback_cost"]))
        ]
    if not available:
        raise channels.SendBlocked(f"No SMS carrier for {countries.name_of(country)} is switched on.")
    refusals = []
    for key, unit in available:
        carrier = CARRIERS[key]
        try:
            ref = carrier.send(conn, account, to, msg["body"], ref=str(msg["id"]))
        except Refused as e:
            _attempt(conn, msg, country, key, "refused", error=str(e))
            refusals.append(f"{carrier.label}: {e}")
            continue  # failover: nothing was sent
        except Exception as e:
            _attempt(conn, msg, country, key, "unknown", error=f"{type(e).__name__}: {e}")
            raise
        n = segments(msg["body"])
        cost = unit * n
        _attempt(conn, msg, country, key, "accepted", ref=ref, cost=cost)
        usage.record(
            conn,
            msg["customer_id"],
            "sms_route_cost",
            float(cost),
            ref=str(msg["id"]),
            detail={"carrier": key, "country": country, "segments": n, "currency": route["currency"]},
        )
        return ref
    raise providers.ProviderError("Every SMS carrier on the route refused the message. " + " ".join(refusals))


# ---- the SMS channel ----------------------------------------------------------------


class RoutedSms(messaging.Sms):
    """SMS with the country rules and carrier routing (replaces phase 2's channel)."""

    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        if template:
            raise channels.SendBlocked("SMS has no templates. Write the message as text.")
        acct = self.account(conn, conversation)
        country = countries.check_sms(conn, conversation, identity["address"], acct["address"])
        route = route_for(conn, country)
        if route is not None and not legs(conn, route, conversation["customer_id"]):
            raise channels.SendBlocked(f"No SMS carrier for {countries.name_of(country)} is switched on.")
        super().rules(conn, conversation, identity, body, template)

    def deliver(self, conn: psycopg.Connection, conversation: dict, message: dict) -> dict:
        # The rules still hold when a queued message goes out; the counted limits
        # were applied when it was accepted, so they are not counted again here.
        acct = self.account(conn, conversation)
        ident = messaging.identity_of(conn, conversation)
        if ident["opted_out"]:
            raise channels.SendBlocked(f"{ident['address']} has opted out of SMS messages.")
        country = countries.check_sms(conn, conversation, ident["address"], acct["address"], count_limits=False)
        route = route_for(conn, country)
        try:
            if route is not None:
                ref = send_routed(conn, acct, message, ident["address"], country, route)
            else:
                ref = providers.get(acct["provider"]).send_text(
                    acct, ident["address"], message["body"], ref=str(message["id"]), conn=conn
                )
        except channels.SendBlocked:
            raise
        except Exception as e:
            conn.execute(
                "UPDATE channel_accounts SET last_error = %s, last_error_at = now() WHERE id = %s",
                (f"{type(e).__name__}: {e}"[:500], acct["id"]),
            )
            raise
        conn.execute("UPDATE channel_accounts SET last_sent_at = now() WHERE id = %s", (acct["id"],))
        messaging.remember_thread(conn, conversation["id"], conversation["customer_id"], acct["id"])
        usage.record(conn, conversation["customer_id"], "message_out:sms", 1, ref=str(message["id"]))
        return {"status": "sent", "provider_ref": ref}


SMS = RoutedSms("sms", "SMS")
channels.register(SMS)


def route_attempts(conn: psycopg.Connection, customer_id: Any, since: dt.datetime | None = None) -> list[dict]:
    return conn.execute(
        """SELECT message_id, country, carrier, outcome, error, cost, at FROM sms_route_attempts
           WHERE customer_id = %s AND (%s::timestamptz IS NULL OR at >= %s) ORDER BY id DESC LIMIT 100""",
        (customer_id, since, since),
    ).fetchall()
