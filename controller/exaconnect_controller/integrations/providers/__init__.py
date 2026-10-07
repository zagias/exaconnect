"""Connectors: thin profiles over the standards layer (ADR 0026).

A provider says which settings it needs (and which of them are secret),
what it takes to go live, and how to turn one CloudEvent into the
provider's own API call. Delivery, retries, the simulated stand-in, the
delivery log and incident bookkeeping are shared (delivery.py).
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ..transport import Http, Transport, sim_ok


class ProviderError(Exception):
    """A delivery failed. `retry` says whether trying again could help."""

    def __init__(self, message: str, code: int | None = None, retry: bool = True):
        super().__init__(message)
        self.code = code
        self.retry = retry


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    required: bool = False
    default: Any = None
    help: str = ""
    kind: str = "text"  # text, url, bool, int, choice, list
    choices: tuple[str, ...] = ()

    def public(self) -> dict:
        out = {
            "name": self.name,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
            "kind": self.kind,
            "help": self.help,
        }
        if self.default is not None:
            out["default"] = self.default
        if self.choices:
            out["choices"] = list(self.choices)
        return out


@dataclass
class Outcome:
    code: int | None = None
    detail: dict = field(default_factory=dict)
    skipped: str = ""  # a reason when nothing needed sending


class Context:
    """What a provider gets for one delivery."""

    def __init__(self, conn, integration: dict, config: dict, secrets: dict, http: Transport, test: bool = False):
        self.conn = conn
        self.integration = integration
        self.config = config
        self.secrets = secrets
        self.http = http
        self.test = test

    # Incidents opened in ITSM tools, keyed by the event's dedup key.
    def incident(self, dedup_key: str) -> dict | None:
        if self.conn is None or not dedup_key:
            return None
        return self.conn.execute(
            "SELECT * FROM connect_incidents WHERE integration_id = %s AND dedup_key = %s",
            (self.integration["id"], dedup_key),
        ).fetchone()

    def opened(self, dedup_key: str, external_id: str) -> None:
        if self.conn is None or not dedup_key:
            return
        self.conn.execute(
            """INSERT INTO connect_incidents (integration_id, dedup_key, external_id)
               VALUES (%s, %s, %s)
               ON CONFLICT (integration_id, dedup_key) DO UPDATE SET external_id = EXCLUDED.external_id,
                 state = 'open', opened_at = now(), updated_at = now()""",
            (self.integration["id"], dedup_key, external_id),
        )

    def resolved(self, dedup_key: str) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            "UPDATE connect_incidents SET state = 'resolved', updated_at = now()"
            " WHERE integration_id = %s AND dedup_key = %s",
            (self.integration["id"], dedup_key),
        )


class Provider:
    key = ""
    name = ""
    category = ""  # webhook, chat, alerting, itsm, monitoring, siem, logs, automation, carrier
    docs = ""  # the provider's real API reference
    api = ""  # the API used, in a few words
    live_needs = ""  # what must be provided to go live
    fields: tuple[Field, ...] = ()
    receives_events = True
    owners: tuple[str, ...] = ("customer", "carrier")

    def secret_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.secret]

    def credentials_present(self, config: dict, secrets: dict) -> bool:
        return all((secrets.get(f.name) if f.secret else config.get(f.name)) for f in self.fields if f.required)

    def validate(self, config: dict) -> dict:
        """Normalise settings (not secrets). Raise ValueError with a message people can read."""
        out: dict[str, Any] = {}
        for f in self.fields:
            if f.secret:
                continue
            v = config.get(f.name, f.default)
            if v is None or v == "":
                if f.required:
                    raise ValueError(f"{f.label} is required.")
                if f.default is not None:
                    out[f.name] = f.default
                continue
            if f.kind == "url":
                check_url_shape(str(v), f.label)
                v = str(v).rstrip("/")
            elif f.kind == "bool":
                v = bool(v)
            elif f.kind == "int":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{f.label} must be a whole number.") from None
            elif f.kind == "choice":
                if v not in f.choices:
                    raise ValueError(f"{f.label} must be one of {', '.join(f.choices)}.")
            elif f.kind == "list":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                v = [str(x)[:120] for x in v][:50]
            elif f.kind == "pem":
                v = str(v).strip()
                if len(v) > 20_000 or "-----BEGIN CERTIFICATE-----" not in v:
                    raise ValueError(f"{f.label} must be one or more PEM certificates.")
                try:
                    import ssl

                    ssl.create_default_context().load_verify_locations(cadata=v)
                except (ssl.SSLError, ValueError):
                    raise ValueError(f"{f.label} is not a certificate that can be read.") from None
            else:
                v = str(v).strip()[:500]
            out[f.name] = v
        return out

    def validate_secrets(self, secrets: dict) -> dict:
        out = {}
        for f in self.fields:
            if f.secret and secrets.get(f.name):
                v = str(secrets[f.name]).strip()
                if f.kind == "url":
                    check_url_shape(v, f.label)
                out[f.name] = v[:4000]
        return out

    def deliver(self, ctx: Context, event: dict) -> Outcome:
        raise NotImplementedError

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        return sim_ok(202, {})

    def public(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "docs": self.docs,
            "api": self.api,
            "live_needs": self.live_needs,
            "receives_events": self.receives_events,
            "owners": list(self.owners),
            "fields": [f.public() for f in self.fields],
        }


def check_url_shape(url: str, label: str = "Address") -> None:
    """Cheap checks at save time (no DNS); the full public-address check runs before each live send."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise ValueError(f"{label} must be a full https:// address.")
    if p.scheme == "http" and not _lab():
        raise ValueError(f"{label} must start with https://.")
    if p.username or p.password:
        raise ValueError(f"{label} must not contain a user name or password; use the secret fields.")


def _lab() -> bool:
    import os

    return os.environ.get("EXA_WEBHOOK_ALLOW_PRIVATE") == "1"


def raise_for(resp: Http, what: str) -> None:
    if resp.ok:
        return
    retry = resp.status >= 500 or resp.status in (408, 425, 429)
    snippet = resp.text[:200].replace("\n", " ")
    raise ProviderError(f"{what} answered {resp.status}: {snippet}", resp.status, retry)


REGISTRY: dict[str, Provider] = {}


def register(cls: type[Provider]) -> type[Provider]:
    """Class decorator: one shared instance per provider."""
    REGISTRY[cls.key] = cls()
    return cls


def get(key: str) -> Provider | None:
    _load()
    return REGISTRY.get(key)


def all_providers() -> list[Provider]:
    _load()
    return list(REGISTRY.values())


def _load() -> None:
    from .. import netbox  # noqa: F401
    from . import alerting, chat, itsm, monitoring, otlp, snmp, syslog, webhook  # noqa: F401


# ---- what connectors say about an event -----------------------------------------


def kind_name(ev: dict) -> str:
    from ..catalogue import name_of

    return name_of(ev["type"])


def title(ev: dict) -> str:
    from ..catalogue import KINDS

    k = KINDS.get(kind_name(ev))
    return k.title if k else kind_name(ev)


def summary(ev: dict) -> str:
    data = ev.get("data") or {}
    return str(data.get("summary") or title(ev))


def link(ev: dict) -> str:
    """Where the event shows in the portal (empty when EXA_PUBLIC_URL is not set)."""
    import os

    base = os.environ.get("EXA_PUBLIC_URL", "").rstrip("/")
    if not base:
        return ""
    data = ev.get("data") or {}
    if data.get("site_id"):
        return f"{base}/sites/{data['site_id']}"
    return f"{base}/"


def facts(ev: dict) -> list[tuple[str, str]]:
    """Short label/value pairs for cards and descriptions."""
    data = ev.get("data") or {}
    out = [("Event", title(ev)), ("Severity", str(ev.get("severity", "info")).capitalize())]
    for key, label in (
        ("site", "Site"),
        ("class", "Class"),
        ("path_label", "Path"),
        ("carrier", "Carrier"),
        ("node", "Node"),
        ("from_path", "From"),
        ("to_path", "To"),
        ("starts_at", "Starts"),
        ("ends_at", "Ends"),
    ):
        if data.get(key):
            out.append((label, str(data[key])))
    out.append(("Time", str(ev.get("time", ""))))
    return out
