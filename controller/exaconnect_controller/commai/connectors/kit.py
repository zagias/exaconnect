"""The connector kit (ADR 0034): shared plumbing for every app connector.

A connector built on the kit gets, without writing it again:

- **Two modes.** ``live`` calls the provider's real API. ``simulated`` sends
  the very same requests to an in-process stand-in (``Simulator``) that
  answers in the provider's own shapes, so request building, response
  mapping, pagination and errors are exercised before ExaCarib has the
  provider's app. A connection is simulated until the app is ready: its
  environment settings are present (``oauth.ready``) and ExaCarib has
  switched the go-live capability ``feature/integration-<app>`` on.
- **Auth.** OAuth 2.0 (``automation/oauth.py``), or credentials a business
  enters (API key, user name and app password, client id and secret),
  stored encrypted and never returned.
- **Calls** with rate-limit handling: a 429 (or 503) with a short
  ``Retry-After`` is waited out and retried in place (at most twice); a long
  one becomes a ``provider`` error so the action job retries later with the
  same idempotency key. Errors map to the repair causes of ADR 0020.
- **Pagination** (``paginate``), **idempotency** (``remember`` / ``known``
  and ``ref``), test-mode dry runs, and **webhook verification** helpers
  (HMAC hex/base64, timestamped signatures, Standard Webhooks).

A new connector: subclass ``KitConnector``, set ``app``, ``label``,
``category``, ``actions``, ``auth`` and ``base_url``; write ``execute``
with ``self.call``; give it a ``Simulator`` subclass; ``register`` it. It
appears in the catalogue (data-driven from the registry), with its go-live
capability declared by ``register``.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import golive
from . import Connector, ConnectorError
from . import register as _register

CATEGORIES = {
    "crm": "CRM",
    "email": "Email",
    "calendar": "Calendars and scheduling",
    "contacts": "Contacts",
    "helpdesk": "Helpdesk and ticketing",
    "chat": "Team chat",
    "commerce": "Commerce",
    "payments": "Payments",
    "files": "Files and knowledge",
    "automation": "Automation and webhooks",
    "custom": "Your own systems",
    "example": "Example apps",
    "other": "Other",
}

MAX_INLINE_WAIT_S = 5.0  # Retry-After up to this is waited out in place
INLINE_RETRIES = 2

# Tests replace this so a rate-limit wait doesn't slow them down.
sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Credential:
    """One field a business enters through secure entry (credentials auth)."""

    name: str
    label: str
    secret: bool = True
    pattern: str = r".{1,400}"


@dataclass(frozen=True)
class Setting:
    """A non-secret connection setting (tenant, subdomain, base URL...)."""

    name: str
    label: str
    pattern: str = r"[A-Za-z0-9._:/@-]{1,200}"
    required: bool = False


@dataclass
class Request:
    method: str
    url: str
    headers: dict
    body: Any  # parsed JSON, form dict, or text
    raw: bytes | None

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path

    @property
    def query(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.url).query))


@dataclass
class Response:
    status: int
    body: Any
    headers: dict = field(default_factory=dict)


# ---- signatures ------------------------------------------------------------------------


def hmac_hex(secret: str | bytes, message: bytes, algo: str = "sha256") -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.new(key, message, getattr(hashlib, algo)).hexdigest()


def hmac_b64(secret: str | bytes, message: bytes, algo: str = "sha256") -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    return base64.b64encode(hmac.new(key, message, getattr(hashlib, algo)).digest()).decode()


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def fresh(timestamp: str | int, tolerance_s: int = 300, now: float | None = None) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs((now or time.time()) - ts) <= tolerance_s


def ref(key: str, prefix: str = "cmai") -> str:
    """A short, stable reference written into a created record, so a retry
    can find what an earlier attempt created."""
    return prefix + hashlib.sha256(key.encode()).hexdigest()[:16]


def header(headers: dict, name: str) -> str:
    n = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == n:
            return str(v)
    return ""


def retry_after_s(headers: dict) -> float | None:
    """Seconds to wait from Retry-After (seconds or an HTTP date) or a reset header."""
    v = header(headers, "Retry-After")
    if v:
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(v)
                return max(0.0, (when - dt.datetime.now(dt.UTC)).total_seconds())
            except (TypeError, ValueError):
                return None
    for name in ("X-RateLimit-Reset", "RateLimit-Reset", "X-Rate-Limit-Reset"):
        v = header(headers, name)
        if v:
            try:
                n = float(v)
            except ValueError:
                continue
            return max(0.0, n - time.time()) if n > 1_000_000_000 else n
    return None


# ---- simulated stand-ins ---------------------------------------------------------------


class Simulator:
    """An in-process stand-in for a provider's API, answering in its shapes.

    Subclasses add routes with ``route(method, pattern)`` and keep records in
    ``sim_records`` (per business), so the stand-in behaves like the real
    thing: created objects can be read back, duplicates are refused the way
    the provider refuses them. A connection's ``settings.simulate_failure``
    (``expired_signin``, ``permission``, ``provider``, ``rate_limit``)
    rehearses a broken integration.
    """

    app = ""

    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, Callable]] = []
        for name in dir(self):
            fn = getattr(self, name)
            spec = getattr(fn, "_route", None)
            if spec:
                self.routes.append((spec[0], re.compile(spec[1]), fn))

    @staticmethod
    def route(method: str, pattern: str):
        def deco(fn):
            fn._route = (method, pattern)
            return fn

        return deco

    def handle(self, conn: psycopg.Connection, connection: dict, req: Request) -> Response:
        mode = (connection.get("settings") or {}).get("simulate_failure")
        if mode == "expired_signin":
            return Response(401, self.error_body(401, "The access token has expired."))
        if mode == "permission":
            return Response(403, self.error_body(403, "Insufficient permissions for this request."))
        if mode == "provider":
            return Response(503, self.error_body(503, "Service unavailable."))
        if mode == "rate_limit":
            return Response(429, self.error_body(429, "Too many requests."), {"Retry-After": "120"})
        path = req.path
        for method, pat, fn in self.routes:
            m = pat.search(path)
            if method == req.method and m:
                out = fn(conn, connection, req, m)
                if isinstance(out, Response):
                    return out
                status, body = out[0], out[1]
                return Response(status, body, out[2] if len(out) > 2 else {})
        return Response(404, self.error_body(404, f"No route for {req.method} {path}"))

    def error_body(self, status: int, message: str) -> Any:
        return {"error": {"code": status, "message": message}}

    # storage --------------------------------------------------------------------------

    def _app(self) -> str:
        return f"sim:{self.app}"

    def put(self, conn, connection: dict, kind: str, oid: str, data: dict) -> dict:
        conn.execute(
            """INSERT INTO sim_records (customer_id, app, kind, idempotency_key, data) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, app, idempotency_key) DO UPDATE SET data = EXCLUDED.data""",
            (connection["customer_id"], self._app(), kind, f"{kind}:{oid}", Jsonb(data)),
        )
        return data

    def get(self, conn, connection: dict, kind: str, oid: str) -> dict | None:
        row = conn.execute(
            "SELECT data FROM sim_records WHERE customer_id = %s AND app = %s AND idempotency_key = %s",
            (connection["customer_id"], self._app(), f"{kind}:{oid}"),
        ).fetchone()
        return row["data"] if row else None

    def delete(self, conn, connection: dict, kind: str, oid: str) -> bool:
        cur = conn.execute(
            "DELETE FROM sim_records WHERE customer_id = %s AND app = %s AND idempotency_key = %s",
            (connection["customer_id"], self._app(), f"{kind}:{oid}"),
        )
        return cur.rowcount > 0

    def all(self, conn, connection: dict, kind: str) -> list[dict]:
        rows = conn.execute(
            "SELECT data FROM sim_records WHERE customer_id = %s AND app = %s AND kind = %s ORDER BY created_at, id",
            (connection["customer_id"], self._app(), kind),
        ).fetchall()
        return [r["data"] for r in rows]

    def new_id(self, conn, connection: dict, kind: str, digits: bool = False) -> str:
        n = (
            conn.execute(
                "SELECT count(*) AS n FROM sim_records WHERE customer_id = %s AND app = %s AND kind = %s",
                (connection["customer_id"], self._app(), kind),
            ).fetchone()["n"]
            + 1
        )
        if digits:
            return str(1000 + n)
        seed = f"{connection['customer_id']}:{kind}:{n}".encode()
        return f"{kind[:3]}{hashlib.sha256(seed).hexdigest()[:12]}"


# ---- the connector base ----------------------------------------------------------------


class KitConnector(Connector):
    category = "other"
    auth = "oauth"  # oauth | credentials | none
    provider_name = ""  # used in messages; defaults to label
    credentials: tuple[Credential, ...] = ()
    settings_fields: tuple[Setting, ...] = ()
    simulator: Simulator | None = None
    health_path = ""
    golive_criteria: dict[str, str] = {}
    needs_from_exacarib = ""  # what Dudley registers, in words
    webhooks = ""  # how the app tells CommAI about changes, in words ("" if it can't)
    docs_url = ""

    # ---- identity ------------------------------------------------------------------

    @property
    def golive_key(self) -> str:
        return f"integration-{self.app}"

    def name(self) -> str:
        return self.provider_name or self.label

    def env_names(self) -> list[str]:
        from ..automation import oauth

        return oauth.env_names(self.app) if self.auth == "oauth" else []

    # ---- modes ---------------------------------------------------------------------

    def real_ready(self, conn: psycopg.Connection, customer_id: Any) -> tuple[bool, list[str]]:
        """Whether this business can use the real app: what is missing, in words."""
        from ..automation import oauth, vault

        why: list[str] = []
        if self.auth == "oauth":
            ok, reason = oauth.ready(self.app)
            if not ok:
                why.append(reason)
        elif self.auth == "credentials" and not vault.configured():
            why.append("Secure storage is not set up: the controller needs EXA_SECRETS_KEY.")
        if not golive.enabled(conn, "feature", self.golive_key, customer_id):
            why.append(f"ExaCarib has not switched {self.label} on yet (go-live checks).")
        return (not why), why

    @staticmethod
    def simulated(connection: dict) -> bool:
        return connection.get("auth_method") == "simulated"

    def dry(self, connection: dict) -> bool:
        """Test mode against the real app checks and shows writes without sending
        them, unless settings.test_writes says the account is a sandbox. The
        stand-in is a sandbox already."""
        if self.simulated(connection):
            return False
        return bool(connection.get("test")) and not (connection.get("settings") or {}).get("test_writes")

    # ---- settings ------------------------------------------------------------------

    def check_settings(self, settings: dict) -> dict:
        fields = {s.name: s for s in self.settings_fields}
        for k, v in settings.items():
            f = fields.get(k)
            if f and not re.fullmatch(f.pattern, str(v)):
                raise ValueError(f"{f.label} is not valid.")
        return settings

    def settings(self, connection: dict) -> dict:
        return connection.get("settings") or {}

    # ---- calls ---------------------------------------------------------------------

    def base_url(self, conn: psycopg.Connection, connection: dict) -> str:
        raise NotImplementedError

    def auth_headers(self, conn: psycopg.Connection, connection: dict) -> dict:
        from ..automation import oauth

        if self.simulated(connection):
            return {"Authorization": "Bearer standin"}
        return {"Authorization": f"Bearer {oauth.access_token(conn, connection)}"}

    def cause(self, status: int, body: Any) -> str:
        if status == 401:
            return "expired_signin"
        if status == 403:
            return "permission"
        if status in (400, 404, 409, 410, 412, 422):
            return "input"
        return "provider"

    def message(self, status: int, body: Any) -> str:
        msg = ""
        if isinstance(body, dict):
            e = body.get("error")
            if isinstance(e, dict):
                msg = str(e.get("message") or e.get("code") or "")
            elif isinstance(e, str):
                msg = str(body.get("error_description") or body.get("message") or e)
            else:
                msg = str(body.get("message") or body.get("detail") or "")
        elif isinstance(body, list) and body and isinstance(body[0], dict):
            msg = str(body[0].get("message", ""))
        return f"{self.name()} answered {status}" + (f": {msg[:200]}" if msg else ".")

    def fail(self, r: Response | Any) -> ConnectorError:
        return ConnectorError(self.message(r.status, r.body), self.cause(r.status, r.body))

    def call(
        self,
        conn: psycopg.Connection,
        connection: dict,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        form: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
        allow: tuple[int, ...] = (),
    ) -> Response:
        """One call to the app (or its stand-in). Returns the response when its
        status is in ``ok`` or ``allow``; raises ConnectorError otherwise."""
        from ..automation import http

        url = path if path.startswith("http") else self.base_url(conn, connection).rstrip("/") + path
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params, doseq=True)}"
        if not self.simulated(connection) and not golive.enabled(
            conn, "feature", self.golive_key, connection["customer_id"]
        ):
            raise ConnectorError(f"ExaCarib has not switched {self.label} on yet.", "permission")
        h = {"Accept": "application/json", **self.auth_headers(conn, connection), **(headers or {})}
        attempt = 0
        while True:
            if self.simulated(connection):
                r = self._simulate(conn, connection, method, url, h, json_body, form, content)
            else:
                try:
                    hr = http.request(method, url, json_body=json_body, form=form, content=content, headers=h)
                except http.NetworkError as e:
                    raise ConnectorError(str(e), "provider") from None
                r = Response(hr.status, hr.body, hr.headers or {})
            if r.status in (429, 503) and attempt < INLINE_RETRIES:
                wait = retry_after_s(r.headers)
                if wait is not None and wait <= MAX_INLINE_WAIT_S:
                    sleep(wait)
                    attempt += 1
                    continue
            break
        if r.status in ok or r.status in allow:
            return r
        if r.status in (429, 503):
            wait = retry_after_s(r.headers)
            hint = f" Try again in {int(wait)} s." if wait else ""
            raise ConnectorError(f"{self.name()} is limiting requests ({r.status}).{hint}", "provider")
        raise self.fail(r)

    def _simulate(self, conn, connection, method, url, headers, json_body, form, content) -> Response:
        if self.simulator is None:
            return Response(501, {"error": {"message": "No stand-in for this app."}})
        if json_body is not None:
            body, raw = json_body, json.dumps(json_body).encode()
        elif form is not None:
            body, raw = dict(form), urllib.parse.urlencode(form).encode()
        elif content is not None:
            body, raw = content.decode("utf-8", "replace"), content
        else:
            body, raw = None, None
        # The stand-in works on a copy, inside a savepoint, like a remote system would.
        with conn.transaction():
            return self.simulator.handle(conn, connection, Request(method, url, headers, body, raw))

    def paginate(
        self,
        conn: psycopg.Connection,
        connection: dict,
        path: str,
        *,
        items: Callable[[Any], list],
        next_page: Callable[[Response], str | dict | None],
        params: dict | None = None,
        max_items: int = 200,
        max_pages: int = 20,
    ) -> Iterator[dict]:
        """Follow a provider's pages: ``next_page`` returns the next URL, or
        extra params (a cursor), or None at the end."""
        url, p, n = path, dict(params or {}), 0
        for _ in range(max_pages):
            r = self.call(conn, connection, "GET", url, params=p or None)
            for item in items(r.body) or []:
                yield item
                n += 1
                if n >= max_items:
                    return
            nxt = next_page(r)
            if not nxt:
                return
            if isinstance(nxt, dict):
                p = {**p, **nxt}
            else:
                url, p = nxt, {}

    # ---- idempotency ---------------------------------------------------------------

    def known(self, conn, connection: dict, key: str) -> dict | None:
        return conn.execute(
            "SELECT object_type, object_id FROM commai_connector_objects WHERE customer_id = %s AND app = %s"
            " AND idempotency_key = %s",
            (connection["customer_id"], self.app, key),
        ).fetchone()

    def remember(self, conn, connection: dict, key: str, obj: str, oid: str) -> None:
        conn.execute(
            """INSERT INTO commai_connector_objects (customer_id, app, idempotency_key, object_type, object_id)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (connection["customer_id"], self.app, key, obj, oid),
        )

    # ---- health --------------------------------------------------------------------

    def health(self, conn, connection: dict) -> dict:
        if not self.health_path:
            return {"ok": True, "cause": "", "detail": ""}
        try:
            self.call(conn, connection, "GET", self.health_path)
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        mode = "the stand-in" if self.simulated(connection) else self.name()
        return {"ok": True, "cause": "", "detail": f"{self.label} answering ({mode})."}

    # ---- webhooks from the app -----------------------------------------------------

    def verify_webhook(self, conn, connection: dict, hook: dict, headers: dict, body: bytes, query: dict) -> bool:
        """Check the app signed this delivery. ``hook`` holds the per-connection
        token and secret. Apps without webhooks refuse everything."""
        return False

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        """The deliveries' events as [{"type": "...", "id": "...", "data": {...}}]."""
        return []

    def on_webhook_event(self, conn: psycopg.Connection, connection: dict, event: dict) -> None:
        """Called once for each verified, first-time event from ``webhook_events``,
        after integration.event is recorded. Connectors override it to act on the
        change; the default does nothing."""
        return None

    def webhook_handshake(self, headers: dict, body: bytes, query: dict) -> tuple[int, str, str] | None:
        """A subscription check the app makes before it sends events
        (status, content type, body), or None."""
        return None

    # ---- describing ----------------------------------------------------------------

    def describe(self) -> dict:
        out = super().describe()
        out.update(
            category=self.category,
            category_label=CATEGORIES.get(self.category, self.category),
            golive_key=self.golive_key,
            env=self.env_names(),
            needs_from_exacarib=self.needs_from_exacarib,
            webhooks=self.webhooks,
            credential_fields=[{"name": c.name, "label": c.label, "secret": c.secret} for c in self.credentials],
            settings_fields=[{"name": s.name, "label": s.label, "required": s.required} for s in self.settings_fields],
            has_simulator=self.simulator is not None,
            docs_url=self.docs_url,
        )
        return out


def register(c: KitConnector) -> KitConnector:
    """Register a kit connector and declare its go-live capability (it starts off)."""
    criteria = {
        "app-registered": f"ExaCarib's {c.label} app (or access) is registered and its settings are on the server"
        + (f" ({', '.join(c.env_names())})." if c.env_names() else "."),
        "live-test": f"A real {c.label} test account passed every allowed action, including idempotent retries.",
        "permissions": "Requested permissions are the least the allowed actions need, and are written down.",
        **c.golive_criteria,
    }
    golive.declare(
        "feature",
        c.golive_key,
        f"{c.label} integration",
        criteria,
        {"category": c.category, "env": c.env_names()},
    )
    return _register(c)


def receives_webhooks(c: Connector) -> bool:
    """True when the connector checks the app's own webhook signature."""
    fn = getattr(type(c), "verify_webhook", None)
    return fn is not None and fn is not KitConnector.verify_webhook


def describe_any(c: Connector) -> dict:
    """The catalogue entry of any connector (kit or not)."""
    out = c.describe()
    out.setdefault("category", getattr(c, "category", "other"))
    out.setdefault("category_label", CATEGORIES.get(out["category"], out["category"]))
    out.setdefault("golive_key", "")
    out.setdefault("env", [])
    out.setdefault("needs_from_exacarib", getattr(c, "needs_from_exacarib", ""))
    out.setdefault("webhooks", getattr(c, "webhooks", ""))
    out.setdefault("credential_fields", [])
    out.setdefault("settings_fields", [])
    out.setdefault("has_simulator", c.app.startswith("sim_"))
    out.setdefault("docs_url", "")
    return out
