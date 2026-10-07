"""How integrations reach the outside world, and how they don't (ADR 0026).

Every integration is OFF by default: nothing leaves the controller until
EXA_INTEGRATIONS_LIVE=1 is set AND the integration has its credentials.
Until then a simulated stand-in answers the way the provider would, and the
request it would have sent is kept in connect_sim_outbox (with every secret
replaced by ``[secret]``), so the portal can show exactly what would go out.

Secrets (tokens, routing keys, webhook URLs that embed a signature) are
encrypted with Fernet under EXA_SECRETS_KEY, the same key CommAI uses, and
are never returned, logged or stored in the clear.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from cryptography.fernet import InvalidToken

from ..commai import webhooks
from ..commai.automation import vault

log = logging.getLogger("exaconnect.integrations")

REDACTED = "[secret]"
SECRET_HEADERS = ("authorization", "dd-api-key", "x-api-key", "api-key", "x-auth-token")


def live_enabled() -> bool:
    return os.environ.get("EXA_INTEGRATIONS_LIVE", "") in ("1", "true", "yes")


# ---- secrets -------------------------------------------------------------------


def encrypt(values: dict[str, str]) -> str:
    if not values:
        return ""
    return vault._fernet().encrypt(json.dumps(values).encode()).decode()


def decrypt(ciphertext: str) -> dict[str, str]:
    if not ciphertext:
        return {}
    try:
        return json.loads(vault._fernet().decrypt(ciphertext.encode()))
    except InvalidToken:
        raise vault.VaultError("A stored secret could not be decrypted (was EXA_SECRETS_KEY changed?).") from None


# ---- HTTP ----------------------------------------------------------------------


@dataclass
class Http:
    status: int
    text: str = ""
    headers: dict = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.text) if self.text else {}
        except ValueError:
            return {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Unreachable(Exception):
    """The provider could not be reached at all (DNS, refused, time-out)."""


Simulator = Callable[[str, str, dict, bytes], Http]


def _urllib(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> Http:
    webhooks.check_url(url)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - checked above
            return Http(r.status, r.read(1_000_000).decode("utf-8", "replace"), dict(r.headers))
    except urllib.error.HTTPError as e:
        return Http(e.code, e.read(100_000).decode("utf-8", "replace"), dict(e.headers or {}))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Unreachable(f"{urllib.parse.urlsplit(url).hostname} could not be reached ({type(e).__name__}).") from None


# Tests may replace this; by default it is real HTTP (to local fake servers in tests).
sender = _urllib


class Transport:
    """One integration's way out: live HTTP, or the simulated stand-in."""

    def __init__(
        self,
        conn: psycopg.Connection | None,
        integration_id: Any,
        provider: str,
        live: bool,
        secrets: dict[str, str] | None = None,
        simulator: Simulator | None = None,
    ):
        self.conn = conn
        self.integration_id = integration_id
        self.provider = provider
        self.live = live
        self.secret_values = [v for v in (secrets or {}).values() if isinstance(v, str) and len(v) >= 4]
        self.simulator = simulator
        self.sent: list[dict] = []  # what went out (redacted), for the delivery log

    def redact(self, text: str) -> str:
        for v in sorted(self.secret_values, key=len, reverse=True):
            text = text.replace(v, REDACTED)
            # The same secret URL-encoded, or base64 inside a Basic header, is redacted by header name.
            text = text.replace(urllib.parse.quote(v, safe=""), REDACTED)
        return text

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        body: bytes | None = None,
        form: dict | None = None,
        timeout: float = 15,
    ) -> Http:
        h = {"User-Agent": "ExaCarib-Connect/1", **(headers or {})}
        if json_body is not None:
            h.setdefault("Content-Type", "application/json")
            body = json.dumps(json_body, separators=(",", ":"), default=str).encode()
        elif form is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(form).encode()
        shown_headers = {k: (REDACTED if k.lower() in SECRET_HEADERS else self.redact(v)) for k, v in h.items()}
        record = {
            "method": method,
            "url": self.redact(url),
            "headers": shown_headers,
            "body": self.redact((body or b"").decode("utf-8", "replace"))[:20_000],
        }
        self.sent.append(record)
        if self.live:
            return sender(method, url, h, body, timeout)
        if self.conn is not None:
            self.conn.execute(
                """INSERT INTO connect_sim_outbox (integration_id, provider, method, target, body)
                   VALUES (%s, %s, %s, %s, %s)""",
                (self.integration_id, self.provider, method, record["url"], record["body"]),
            )
        if self.simulator is None:
            return Http(202, "{}", {})
        return self.simulator(method, url, h, body or b"")


    def raw(self, proto: str, host: str, port: int, payload: bytes, send: Callable[[], None]) -> None:
        """A non-HTTP send (syslog, SNMP). Live: check the address and send; else record it."""
        shown = self.redact(payload.decode("utf-8", "replace"))[:20_000]
        self.sent.append({"method": proto, "url": f"{host}:{port}", "body": shown})
        if self.live:
            webhooks.check_url(f"https://{host}:{port}/")
            try:
                send()
            except OSError as e:
                raise Unreachable(f"{host}:{port} could not be reached ({type(e).__name__}).") from None
            return
        if self.conn is not None:
            self.conn.execute(
                """INSERT INTO connect_sim_outbox (integration_id, provider, method, target, body)
                   VALUES (%s, %s, %s, %s, %s)""",
                (self.integration_id, self.provider, proto, f"{host}:{port}", shown),
            )


def sim_ok(status: int = 200, body: Any = None) -> Http:
    return Http(status, json.dumps(body if body is not None else {}), {"Content-Type": "application/json"})


def tls_context(ca_pem: str = "", insecure: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    if insecure:  # lab only, never the default
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
