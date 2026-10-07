"""SIP provider interface (ADR 0021, extended for stage 5 in ADR 0027).

ExaCarib has no SIP provider account yet, so the working provider is
`SimulatedProvider`. It behaves like a real one: number search and ordering
by country (from fictional ranges only), port orders that the provider
reviews and answers later (documents needed, rejected, or accepted with a
firm order commitment date), emergency address checks that take time and can
fail, call records for reconciliation, and SIP OPTIONS answers per trunk.
Its long-running answers are rows in `voice_sim_provider_requests` with a
`ready_at`; durable jobs poll them, exactly as they will poll a real API.

`HttpSipProvider` is the skeleton of the real adapter. It reads its settings
from EXA_SIP_PROVIDER_URL, EXA_SIP_PROVIDER_KEY and EXA_SIP_PROVIDER_ACCOUNT
and refuses to do anything until all three are set. The paths it calls are
placeholders to match to the chosen provider's API.

Every call that creates something takes an idempotency key. Asked twice with
the same key, a provider must return the same result, never a second number:
that is what lets a failed provisioning step retry safely.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import countries
from .common import VoiceError, digits

SIM_PREFIX = "+186855501"  # +1 868 555 01xx: fictional, never a real subscriber

# How long the simulated provider takes to answer (seconds). Tests set them to 0.
SIM_DELAYS: dict[str, float] = {"port": 120, "emergency": 30}
PORT_NOTICE_DAYS = 5  # business days from acceptance to the earliest switch-over
RESCHEDULE_NOTICE_DAYS = 2


class ProviderError(VoiceError):
    """The provider refused or failed. Safe to retry with the same key."""

    def __init__(self, message: str, code: int = 502):
        super().__init__(message, code)


class NotConfigured(ProviderError):
    pass


def add_business_days(day: dt.date, n: int) -> dt.date:
    while n > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


class SipProvider:
    name = "base"
    live = False

    # numbers
    def search_numbers(
        self, conn: psycopg.Connection, country: str, area: str = "", contains: str = "", limit: int = 10
    ) -> list[dict]:
        """-> [{"e164", "country", "area"}] numbers free to order now."""
        raise NotImplementedError

    def order_number(
        self,
        conn: psycopg.Connection,
        customer_id: Any,
        key: str,
        area: str = "868",
        country: str | None = None,
        e164: str | None = None,
    ) -> dict:
        """-> {"e164", "ref"}; the same key always returns the same number."""
        raise NotImplementedError

    def release_number(self, conn: psycopg.Connection, e164: str) -> None:
        raise NotImplementedError

    # porting
    def submit_port(
        self,
        conn: psycopg.Connection,
        customer_id: Any,
        e164: str,
        losing_carrier: str,
        key: str,
        details: dict | None = None,
    ) -> dict:
        """-> {"ref", "status"}. details: account_name, account_number,
        requested_date, documents (the types sent so far)."""
        raise NotImplementedError

    def port_status(self, conn: psycopg.Connection, ref: str) -> dict:
        """-> {"status": submitted|documents_needed|accepted|rejected|cancelled|activated|rolled_back,
        "foc_date", "code", "reason"}"""
        raise NotImplementedError

    def send_port_documents(self, conn: psycopg.Connection, ref: str, documents: list[dict]) -> dict:
        raise NotImplementedError

    def reschedule_port(self, conn: psycopg.Connection, ref: str, day: dt.date) -> dict:
        raise NotImplementedError

    def cancel_port(self, conn: psycopg.Connection, ref: str) -> None:
        raise NotImplementedError

    def activate_port(self, conn: psycopg.Connection, ref: str) -> dict:
        """Cut the number over to ExaCarib on the agreed date."""
        raise NotImplementedError

    def rollback_port(self, conn: psycopg.Connection, ref: str) -> dict:
        """Hand the number back to the losing carrier after a failed cut-over."""
        raise NotImplementedError

    # calls
    def test_call(self, conn: psycopg.Connection, e164: str) -> dict:
        """Place a short test call to the number. -> {"ok": bool, "detail": str}"""
        raise NotImplementedError

    # emergency addresses
    def register_emergency_address(self, conn: psycopg.Connection, e164: str, address: dict) -> str:
        """-> status: 'pending' | 'registered' | 'rejected' (kept for stage 2 moves)."""
        raise NotImplementedError

    def validate_emergency_address(self, conn: psycopg.Connection, address: dict, key: str) -> dict:
        """Submit an address for checking. -> {"ref", "status"}"""
        raise NotImplementedError

    def emergency_address_status(self, conn: psycopg.Connection, ref: str) -> dict:
        """-> {"status": pending|registered|rejected, "reason", "normalised"}"""
        raise NotImplementedError

    # carriers and trunks
    def options_ping(self, conn: psycopg.Connection, carrier: dict) -> dict:
        """A SIP OPTIONS request to the carrier's trunk. -> {"ok", "rtt_ms", "code"}"""
        raise NotImplementedError

    def place_call(self, conn: psycopg.Connection, carrier: dict, to: str) -> dict:
        """Hand an outbound call to a carrier. -> {"ok", "detail"}"""
        raise NotImplementedError

    def fetch_cdrs(self, conn: psycopg.Connection, carrier: str, start: dt.date, end: dt.date) -> str:
        """The carrier's call records for the period, as CSV with the columns
        call_ref, started_at, destination, seconds, cost."""
        raise NotImplementedError

    def supplier_rate(self, destination: str) -> Decimal:
        """What the provider charges ExaCarib per minute (used to estimate margin)."""
        raise NotImplementedError


# Faults the tests (and the lab) inject: {"order_number": 2} fails the next two calls.
# Keys: order_number, submit_port, test_call, port_reject, port_activate,
# emergency_reject, options:<carrier>, call:<carrier>.
FAULTS: dict[str, int] = {}


def _fault(op: str) -> None:
    left = FAULTS.get(op, 0)
    if left > 0:
        FAULTS[op] = left - 1
        raise ProviderError(f"Simulated provider failure on {op}.")


def _faulted(op: str) -> bool:
    try:
        _fault(op)
    except ProviderError:
        return True
    return False


def _later(kind: str) -> str:
    return f"now() + make_interval(secs => {float(SIM_DELAYS.get(kind, 0))})"


class SimulatedProvider(SipProvider):
    name = "simulated"
    live = False

    # ---- numbers ----
    def _taken(self, conn) -> set[str]:
        return {
            r["e164"] for r in conn.execute("SELECT e164 FROM voice_sim_provider_numbers WHERE NOT released").fetchall()
        } | {r["e164"] for r in conn.execute("SELECT e164 FROM voice_numbers WHERE status <> 'removed'").fetchall()}

    def search_numbers(self, conn, country, area="", contains="", limit=10):
        c = countries.get(country)
        area = countries.check_area(c, area)
        want = digits(contains)
        taken = self._taken(conn)
        out = []
        for e164 in countries.sim_range(c, area):
            if e164 in taken or (want and want not in digits(e164)):
                continue
            out.append({"e164": e164, "country": c.code, "area": area, "example": True})
            if len(out) >= limit:
                break
        return out

    def order_number(self, conn, customer_id, key, area="868", country=None, e164=None):
        row = conn.execute("SELECT e164 FROM voice_sim_provider_numbers WHERE idempotency_key = %s", (key,)).fetchone()
        if row:
            return {"e164": row["e164"], "ref": f"sim:{row['e164']}"}
        _fault("order_number")
        if country:
            c = countries.get(country)
            pool = countries.sim_range(c, countries.check_area(c, area))
        else:
            c = countries.COUNTRIES["TT"]
            pool = [f"{SIM_PREFIX}{i:02d}" for i in range(100)]
        taken = {
            r["e164"] for r in conn.execute("SELECT e164 FROM voice_sim_provider_numbers WHERE NOT released").fetchall()
        }
        if e164:
            if e164 not in pool:
                raise ProviderError(f"{e164} is not a number this provider offers there.", 422)
            if e164 in taken:
                raise ProviderError(f"{e164} has just been taken. Search again and pick another.", 409)
            pool = [e164]
        for n in pool:
            if n in taken:
                continue
            conn.execute(
                """INSERT INTO voice_sim_provider_numbers (e164, customer_id, idempotency_key, country)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (e164) DO UPDATE SET customer_id = EXCLUDED.customer_id,
                     idempotency_key = EXCLUDED.idempotency_key, released = false, country = EXCLUDED.country,
                     created_at = now()""",
                (n, customer_id, key, c.code),
            )
            return {"e164": n, "ref": f"sim:{n}"}
        raise ProviderError(f"The simulated number range for {c.name} is used up.")

    def release_number(self, conn, e164):
        conn.execute("UPDATE voice_sim_provider_numbers SET released = true WHERE e164 = %s", (e164,))

    # ---- porting ----
    def submit_port(self, conn, customer_id, e164, losing_carrier, key, details=None):
        _fault("submit_port")
        ref = f"simport:{key}"
        payload = {"e164": e164, "losing_carrier": losing_carrier, **(details or {})}
        conn.execute(
            f"""INSERT INTO voice_sim_provider_requests (ref, kind, customer_id, payload, status, ready_at)
                VALUES (%s, 'port', %s, %s, 'submitted', {_later("port")})
                ON CONFLICT (ref) DO NOTHING""",
            (ref, customer_id, Jsonb(payload)),
        )
        return {"ref": ref, "status": "submitted"}

    def _req(self, conn, ref, kind) -> dict:
        row = conn.execute(
            "SELECT *, ready_at <= now() AS ready FROM voice_sim_provider_requests WHERE ref = %s AND kind = %s"
            " FOR UPDATE",
            (ref, kind),
        ).fetchone()
        if row is None:
            raise ProviderError("The provider has no such request.", 404)
        return row

    def _set(self, conn, ref, status, outcome: dict, delay_kind: str | None = None) -> None:
        conn.execute(
            f"""UPDATE voice_sim_provider_requests SET status = %s, outcome = %s, updated_at = now()
                {", ready_at = " + _later(delay_kind) if delay_kind else ""} WHERE ref = %s""",
            (status, Jsonb(outcome), ref),
        )

    def port_status(self, conn, ref):
        r = self._req(conn, ref, "port")
        if r["status"] != "submitted" or not r["ready"]:
            return {"status": r["status"], **r["outcome"]}
        p = r["payload"]
        docs = set(p.get("documents") or [])
        if not {"loa", "bill"} <= docs:
            missing = [
                n for t, n in (("loa", "a signed letter of authorisation"), ("bill", "a recent bill")) if t not in docs
            ]
            out = {"code": "DOCUMENTS", "reason": "The losing carrier needs " + " and ".join(missing) + "."}
            self._set(conn, ref, "documents_needed", out)
            return {"status": "documents_needed", **out}
        if str(p.get("account_number", "")).endswith("0000") or _faulted("port_reject"):
            out = {
                "code": "ACCOUNT_MISMATCH",
                "reason": "The account number does not match the losing carrier's records for this number.",
            }
            self._set(conn, ref, "rejected", out)
            return {"status": "rejected", **out}
        earliest = add_business_days(dt.date.today(), PORT_NOTICE_DAYS)
        wanted = dt.date.fromisoformat(p["requested_date"]) if p.get("requested_date") else earliest
        foc = max(wanted, earliest)
        out = {"foc_date": foc.isoformat(), "code": "", "reason": ""}
        self._set(conn, ref, "accepted", out)
        return {"status": "accepted", **out}

    def send_port_documents(self, conn, ref, documents):
        r = self._req(conn, ref, "port")
        if r["status"] in ("cancelled", "activated", "rejected"):
            raise ProviderError("This port can't take documents any more.", 409)
        payload = {**r["payload"], "documents": sorted({d["type"] for d in documents})}
        conn.execute(
            f"""UPDATE voice_sim_provider_requests SET payload = %s, updated_at = now(),
                  status = CASE WHEN status = 'documents_needed' THEN 'submitted' ELSE status END,
                  ready_at = CASE WHEN status = 'documents_needed' THEN {_later("port")} ELSE ready_at END
                WHERE ref = %s""",
            (Jsonb(payload), ref),
        )
        return {"status": "submitted" if r["status"] == "documents_needed" else r["status"]}

    def reschedule_port(self, conn, ref, day):
        r = self._req(conn, ref, "port")
        if r["status"] not in ("accepted", "rolled_back"):
            raise ProviderError("Only an accepted port can be given a new date.", 409)
        earliest = add_business_days(dt.date.today(), RESCHEDULE_NOTICE_DAYS)
        if day < earliest:
            raise ProviderError(
                f"The losing carrier needs {RESCHEDULE_NOTICE_DAYS} working days' notice: "
                f"the earliest date is {earliest.isoformat()}.",
                422,
            )
        out = {**r["outcome"], "foc_date": day.isoformat()}
        self._set(conn, ref, "accepted", out)
        return {"status": "accepted", **out}

    def cancel_port(self, conn, ref):
        r = self._req(conn, ref, "port")
        if r["status"] == "activated":
            raise ProviderError("The number has already moved over.", 409)
        self._set(conn, ref, "cancelled", r["outcome"])

    def activate_port(self, conn, ref):
        r = self._req(conn, ref, "port")
        if r["status"] == "activated":
            return {"status": "activated"}  # asked again after a retry: same answer
        if r["status"] not in ("accepted",):
            raise ProviderError("This port is not ready to switch over.", 409)
        _fault("port_activate")
        self._set(conn, ref, "activated", r["outcome"])
        return {"status": "activated"}

    def rollback_port(self, conn, ref):
        r = self._req(conn, ref, "port")
        self._set(conn, ref, "rolled_back", {**r["outcome"], "rolled_back_at": dt.datetime.now(dt.UTC).isoformat()})
        return {"status": "rolled_back"}

    # ---- calls ----
    def test_call(self, conn, e164):
        try:
            _fault("test_call")
        except ProviderError as e:
            return {"ok": False, "detail": str(e)}
        return {"ok": True, "detail": "Simulated test call answered and audio heard both ways."}

    # ---- emergency addresses ----
    def register_emergency_address(self, conn, e164, address):
        return "pending"

    def validate_emergency_address(self, conn, address, key):
        ref = f"simaddr:{key}"
        conn.execute(
            f"""INSERT INTO voice_sim_provider_requests (ref, kind, payload, status, ready_at)
                VALUES (%s, 'emergency', %s, 'pending', {_later("emergency")})
                ON CONFLICT (ref) DO UPDATE SET payload = EXCLUDED.payload, status = 'pending',
                  ready_at = EXCLUDED.ready_at, updated_at = now()""",
            (ref, Jsonb(address)),
        )
        return {"ref": ref, "status": "pending"}

    def emergency_address_status(self, conn, ref):
        r = self._req(conn, ref, "emergency")
        if r["status"] != "pending" or not r["ready"]:
            return {"status": r["status"], **r["outcome"]}
        a = r["payload"]
        country = str(a.get("country") or "").upper()
        reason = ""
        if not str(a.get("address_line1", "")).strip():
            reason = "The street address is missing."
        elif not (str(a.get("city", "")).strip() or str(a.get("island", "")).strip()):
            reason = "Give the town or city."
        elif country in ("US", "CA", "GB") and not str(a.get("postcode", "")).strip():
            reason = "A postcode (ZIP) is needed for addresses here."
        elif country and country not in countries.COUNTRIES:
            reason = f"The provider does not register addresses in {country}."
        elif _faulted("emergency_reject"):
            reason = "The provider could not match this address to a known location."
        if reason:
            out = {"reason": reason, "normalised": {}}
            self._set(conn, ref, "rejected", out)
            return {"status": "rejected", **out}
        norm = {
            k: " ".join(str(a.get(k, "")).split()).upper()
            for k in ("address_line1", "address_line2", "city", "island", "postcode")
        }
        norm["country"] = country
        out = {"reason": "", "normalised": norm}
        self._set(conn, ref, "registered", out)
        return {"status": "registered", **out}

    # ---- carriers ----
    def options_ping(self, conn, carrier):
        key = carrier["key"]
        if _faulted(f"options:{key}"):
            return {"ok": False, "rtt_ms": None, "code": 408}
        rtt = 15 + int(hashlib.sha256(key.encode()).hexdigest()[:4], 16) % 60
        return {"ok": True, "rtt_ms": rtt, "code": 200}

    def place_call(self, conn, carrier, to):
        if _faulted(f"call:{carrier['key']}"):
            return {"ok": False, "detail": f"{carrier['name']} answered 503 Service Unavailable."}
        return {"ok": True, "detail": f"Connected through {carrier['name']} (simulated)."}

    def fetch_cdrs(self, conn, carrier, start, end):
        rows = conn.execute(
            """SELECT coalesce(nullif(provider_ref, ''), call_id) AS ref, started_at, to_number, seconds,
                      coalesce(carrier_cost, 0) AS cost
               FROM voice_cdrs WHERE carrier = %s AND status = 'completed' AND direction = 'outbound'
                 AND ended_at >= %s AND ended_at < %s ORDER BY ended_at""",
            (carrier, start, end),
        ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(("call_ref", "started_at", "destination", "seconds", "cost"))
        for r in rows:
            w.writerow((r["ref"], r["started_at"].isoformat(), digits(r["to_number"]), r["seconds"], f"{r['cost']:f}"))
        return buf.getvalue()

    def supplier_rate(self, destination):
        d = digits(destination)
        if d.startswith("1868"):
            return Decimal("0.0080")
        if d.startswith("1"):
            return Decimal("0.0120")
        return Decimal("0.1500")


class HttpSipProvider(SipProvider):
    """The real SIP provider's adapter (a skeleton). It refuses to run until
    EXA_SIP_PROVIDER_URL, EXA_SIP_PROVIDER_KEY and EXA_SIP_PROVIDER_ACCOUNT are
    set for an account Dudley has opened. The request paths below are
    placeholders: match them to the chosen provider's API, and test each one
    against its sandbox, before switching EXA_SIP_PROVIDER to "sip"."""

    name = "sip"
    live = True
    SETTINGS = ("EXA_SIP_PROVIDER_URL", "EXA_SIP_PROVIDER_KEY", "EXA_SIP_PROVIDER_ACCOUNT")

    def __init__(self) -> None:
        self.url = os.environ.get("EXA_SIP_PROVIDER_URL", "").rstrip("/")
        self.key = os.environ.get("EXA_SIP_PROVIDER_KEY", "")
        self.account = os.environ.get("EXA_SIP_PROVIDER_ACCOUNT", "")

    def missing(self) -> list[str]:
        return [n for n in self.SETTINGS if not os.environ.get(n)]

    def _call(self, method: str, path: str, body: dict | None = None, *, key: str = "") -> Any:
        missing = self.missing()
        if missing:
            raise NotConfigured(f"The SIP provider is not set up yet ({', '.join(missing)} missing).")
        req = urllib.request.Request(
            f"{self.url}/accounts/{self.account}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                **({"Idempotency-Key": key} if key else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - URL comes from the operator's settings
                data = r.read()
        except urllib.error.HTTPError as e:
            raise ProviderError(f"The SIP provider answered {e.code}.", 502 if e.code >= 500 else 422) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ProviderError("The SIP provider could not be reached.") from e
        return json.loads(data) if data else {}

    def search_numbers(self, conn, country, area="", contains="", limit=10):
        q = f"?country={country}&area={digits(area)}&contains={digits(contains)}&limit={int(limit)}"
        return self._call("GET", f"/numbers/available{q}").get("numbers", [])

    def order_number(self, conn, customer_id, key, area="868", country=None, e164=None):
        r = self._call("POST", "/numbers", {"country": country, "area": area, "e164": e164}, key=key)
        return {"e164": r["e164"], "ref": r["id"]}

    def release_number(self, conn, e164):
        self._call("DELETE", f"/numbers/{digits(e164)}")

    def submit_port(self, conn, customer_id, e164, losing_carrier, key, details=None):
        r = self._call("POST", "/ports", {"e164": e164, "losing_carrier": losing_carrier, **(details or {})}, key=key)
        return {"ref": r["id"], "status": "submitted"}

    def port_status(self, conn, ref):
        return self._call("GET", f"/ports/{ref}")

    def send_port_documents(self, conn, ref, documents):
        return self._call("POST", f"/ports/{ref}/documents", {"documents": documents})

    def reschedule_port(self, conn, ref, day):
        return self._call("PATCH", f"/ports/{ref}", {"foc_date": day.isoformat()})

    def cancel_port(self, conn, ref):
        self._call("DELETE", f"/ports/{ref}")

    def activate_port(self, conn, ref):
        return self._call("POST", f"/ports/{ref}/activate", {})

    def rollback_port(self, conn, ref):
        return self._call("POST", f"/ports/{ref}/rollback", {})

    def test_call(self, conn, e164):
        return self._call("POST", "/test-calls", {"to": e164})

    def register_emergency_address(self, conn, e164, address):
        return self._call("PUT", f"/numbers/{digits(e164)}/emergency-address", address).get("status", "pending")

    def validate_emergency_address(self, conn, address, key):
        r = self._call("POST", "/emergency-addresses", address, key=key)
        return {"ref": r["id"], "status": r.get("status", "pending")}

    def emergency_address_status(self, conn, ref):
        return self._call("GET", f"/emergency-addresses/{ref}")

    def options_ping(self, conn, carrier):
        # SIP OPTIONS is sent by Kamailio's dispatcher module, not over HTTP;
        # the controller reads its results (see deploy/kamailio/README.md).
        raise NotConfigured("Trunk health comes from Kamailio once it runs with a real carrier.")

    def place_call(self, conn, carrier, to):
        raise NotConfigured("Calls are placed by FreeSWITCH and Kamailio, not by the controller.")

    def fetch_cdrs(self, conn, carrier, start, end):
        r = self._call("GET", f"/cdrs?carrier={carrier}&from={start.isoformat()}&to={end.isoformat()}")
        return r.get("csv", "")

    def supplier_rate(self, destination):
        raise NotConfigured("The provider's rates come from its rate sheet (voice carriers).")


def get() -> SipProvider:
    """EXA_SIP_PROVIDER picks the provider: "simulated" (the default) or "sip"."""
    return HttpSipProvider() if os.environ.get("EXA_SIP_PROVIDER", "simulated") == "sip" else SimulatedProvider()
