"""The client and its resources. Every method returns the API's JSON."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import httpx

API = "/api/v1"

if TYPE_CHECKING:
    from .commai import CommAI


class ExaConnectError(Exception):
    """The API refused a request. `detail` is its plain-English reason, `code`
    a stable word to branch on ("not_found", "rate_limited"...), `request_id`
    the id to quote to support, and `retry_after` the seconds to wait after a 429."""

    def __init__(self, status: int, detail: str, code: str = "", request_id: str = "", retry_after: int | None = None):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Keyword arguments to a JSON body, leaving out those not given."""
    return {k: v for k, v in d.items() if v is not _UNSET}


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


_UNSET: Any = _Unset()


class ExaConnect:
    """A connection to an ExaConnect controller.

    `api_key` defaults to EXACONNECT_API_KEY and `base_url` to EXACONNECT_URL.
    Instead of a key, `email` and `password` sign in for a session.
    `client` is an httpx.Client to send requests with (its base URL is used).
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        email: str | None = None,
        password: str | None = None,
        timeout: float = 30,
        client: httpx.Client | None = None,
    ):
        base_url = base_url or os.environ.get("EXACONNECT_URL")
        if client is None and not base_url:
            raise ValueError("Give the controller's URL, or set EXACONNECT_URL.")
        self._http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._token = api_key or (None if email else os.environ.get("EXACONNECT_API_KEY"))
        if self._token is None:
            if not (email and password):
                raise ValueError("Give an API key (or set EXACONNECT_API_KEY), or an email and password.")
            self._token = self._request("POST", "/auth/login", json={"email": email, "password": password})["token"]
        self.sites = Sites(self)
        self.circuits = Circuits(self)
        self.internet = Internet(self)
        self.traffic = Traffic(self)
        self.orders = Orders(self)
        self.partners = Partners(self)
        self.encryption = Encryption(self)
        self.metering = Metering(self)
        self.decisions = Decisions(self)
        self.storm = Storm(self)
        self.api_keys = ApiKeys(self)
        self.links = Links(self)
        self.integrations = Integrations(self)
        self.hooks = Hooks(self)
        self.notices = Notices(self)
        self.onramps = Onramps(self)

    def commai(self, customer_id: str | None = None) -> CommAI:
        """Jibsy for one business (yours by default): contacts, conversations,
        notes, webhooks, events and actions. See commai.py."""
        from .commai import CommAI

        return CommAI(self, customer_id or self.me()["customer_id"])

    def __repr__(self) -> str:
        return f"ExaConnect({str(self._http.base_url)!r})"  # never the key

    def _request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None, headers: dict | None = None
    ) -> Any:
        headers = {
            **(headers or {}),
            **({"Authorization": f"Bearer {self._token}"} if getattr(self, "_token", None) else {}),
        }
        params = {k: v for k, v in (params or {}).items() if v is not None}
        r = self._http.request(method, API + path, json=json, params=params, headers=headers)
        if r.status_code >= 400:
            body: dict = {}
            try:
                body = r.json()
                detail = body.get("detail", r.text)
            except (ValueError, AttributeError):
                detail = r.text
            if isinstance(detail, list):  # a validation error: the first problem
                detail = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" for e in detail)
            retry = r.headers.get("retry-after", "")
            raise ExaConnectError(
                r.status_code,
                str(detail),
                code=str(body.get("code", "")) if isinstance(body, dict) else "",
                request_id=r.headers.get("x-request-id", ""),
                retry_after=int(retry) if retry.isdigit() else None,
            )
        if r.status_code == 204 or not r.content:
            return None
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    def me(self) -> dict:
        """Who this key or session acts as: email, role and customer_id."""
        return self._request("GET", "/auth/me")

    def version(self) -> dict:
        return self._request("GET", "/version")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ExaConnect:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Resource:
    def __init__(self, exa: ExaConnect):
        self._r = exa._request


class Sites(_Resource):
    def list(self) -> list[dict]:
        return self._r("GET", "/sites")

    def get(self, site_id: str) -> dict:
        return self._r("GET", f"/sites/{site_id}")

    def by_name(self, customer_id: str | None = None) -> dict[str, dict]:
        """Sites keyed by name, optionally for one customer."""
        return {s["name"]: s for s in self.list() if customer_id is None or str(s["customer_id"]) == str(customer_id)}

    def metrics(self, site_id: str, minutes: int = 15) -> dict:
        return self._r("GET", f"/sites/{site_id}/metrics", params={"minutes": minutes})


class Circuits(_Resource):
    """Virtual circuits to clouds and between sites (ADR 0009, 0012)."""

    def providers(self) -> dict:
        return self._r("GET", "/circuits/providers")

    def list(self, customer_id: str) -> list[dict]:
        return self._r("GET", f"/customers/{customer_id}/circuits")

    def get(self, customer_id: str, circuit_id: int) -> dict:
        for c in self.list(customer_id):
            if c["id"] == circuit_id:
                return c
        raise ExaConnectError(404, "Circuit not found.")

    def create(self, customer_id: str, **fields: Any) -> dict:
        """Fields as the API takes them: name, kind ("cloud" or "site"), bandwidth_mbps, and
        provider, region, peer_address, peer_asn, psk, cloud_prefixes, secondary_peer_address...
        for a cloud circuit, or a_site_id, b_site_id, a_vlan, b_vlan for a site circuit."""
        return self._r("POST", f"/customers/{customer_id}/circuits", json=fields)

    def update(self, customer_id: str, circuit_id: int, **changes: Any) -> dict:
        return self._r("PATCH", f"/customers/{customer_id}/circuits/{circuit_id}", json=changes)

    def delete(self, customer_id: str, circuit_id: int) -> None:
        self._r("DELETE", f"/customers/{customer_id}/circuits/{circuit_id}")

    def charges(self, customer_id: str, circuit_id: int, month: str | None = None) -> dict:
        return self._r("GET", f"/customers/{customer_id}/circuits/{circuit_id}/charges", params={"month": month})

    def metrics(self, customer_id: str, circuit_id: int, minutes: int = 60) -> list[dict]:
        return self._r("GET", f"/customers/{customer_id}/circuits/{circuit_id}/metrics", params={"minutes": minutes})


class Internet(_Resource):
    """Internet breakout, firewall rules and port forwards (ADR 0010)."""

    def get(self, customer_id: str) -> dict:
        return self._r("GET", f"/customers/{customer_id}/internet")

    def set_mode(self, customer_id: str, site_id: str, mode: str) -> dict:
        """mode: "pop" (through ExaCarib's PoP), "local" (straight out) or "off"."""
        return self._r("PATCH", f"/customers/{customer_id}/internet/sites/{site_id}", json={"mode": mode})

    def create_rule(self, customer_id: str, action: str, **fields: Any) -> dict:
        return self._r("POST", f"/customers/{customer_id}/firewall/rules", json={"action": action, **fields})

    def update_rule(self, customer_id: str, rule_id: int, **changes: Any) -> dict:
        return self._r("PATCH", f"/customers/{customer_id}/firewall/rules/{rule_id}", json=changes)

    def delete_rule(self, customer_id: str, rule_id: int) -> None:
        self._r("DELETE", f"/customers/{customer_id}/firewall/rules/{rule_id}")

    def order_rules(self, customer_id: str, ids: list[int]) -> list[dict]:
        return self._r("POST", f"/customers/{customer_id}/firewall/order", json={"ids": ids})

    def create_forward(
        self, customer_id: str, protocol: str, port: int, to_site_id: str, to_address: str, **fields: Any
    ) -> dict:
        body = {"protocol": protocol, "port": port, "to_site_id": to_site_id, "to_address": to_address, **fields}
        return self._r("POST", f"/customers/{customer_id}/port-forwards", json=body)

    def update_forward(self, customer_id: str, forward_id: int, **changes: Any) -> dict:
        return self._r("PATCH", f"/customers/{customer_id}/port-forwards/{forward_id}", json=changes)

    def delete_forward(self, customer_id: str, forward_id: int) -> None:
        self._r("DELETE", f"/customers/{customer_id}/port-forwards/{forward_id}")


class Traffic(_Resource):
    """Traffic rules and application detection (ADR 0007)."""

    def rules(self, customer_id: str) -> list[dict]:
        return self._r("GET", f"/customers/{customer_id}/rules")

    def create_rule(self, customer_id: str, **fields: Any) -> dict:
        return self._r("POST", f"/customers/{customer_id}/rules", json=fields)

    def replace_rule(self, customer_id: str, rule_id: int, **fields: Any) -> dict:
        return self._r("PUT", f"/customers/{customer_id}/rules/{rule_id}", json=fields)

    def delete_rule(self, customer_id: str, rule_id: int) -> None:
        self._r("DELETE", f"/customers/{customer_id}/rules/{rule_id}")

    def applications(self, customer_id: str) -> list[dict]:
        return self._r("GET", f"/customers/{customer_id}/applications")

    def catalogue(self) -> list[dict]:
        return self._r("GET", "/applications/catalogue")


class Orders(_Resource):
    """Plain-English ordering (ADR 0011). Nothing changes until an order is confirmed."""

    def draft(self, customer_id: str, text: str, engine: str = "auto") -> dict:
        return self._r("POST", f"/customers/{customer_id}/orders/draft", json={"text": text, "engine": engine})

    def create(self, customer_id: str, actions: list[dict]) -> dict:
        return self._r("POST", f"/customers/{customer_id}/orders", json={"actions": actions})

    def list(self, customer_id: str) -> list[dict]:
        return self._r("GET", f"/customers/{customer_id}/orders")

    def get(self, customer_id: str, order_id: int) -> dict:
        return self._r("GET", f"/customers/{customer_id}/orders/{order_id}")

    def confirm(self, customer_id: str, order_id: int, inputs: list[dict] | None = None) -> dict:
        """inputs: one dict per action with what `needs` asks for, such as the gateway's psk."""
        return self._r("POST", f"/customers/{customer_id}/orders/{order_id}/confirm", json={"inputs": inputs or []})

    def cancel(self, customer_id: str, order_id: int) -> dict:
        return self._r("POST", f"/customers/{customer_id}/orders/{order_id}/cancel")


class Partners(_Resource):
    def list(self, category: str | None = None, q: str | None = None) -> list[dict]:
        return self._r("GET", "/partners", params={"category": category, "q": q})

    def get(self, slug: str) -> dict:
        return self._r("GET", f"/partners/{slug}")


class Encryption(_Resource):
    def report(self, customer_id: str) -> dict:
        return self._r("GET", f"/customers/{customer_id}/encryption")


class Metering(_Resource):
    """Usage and carrier settlement: 95th percentile per link (section 4.5 of the brief)."""

    def links(self, hours: int | None = None) -> dict:
        return self._r("GET", "/metering/links", params={"hours": hours})

    def samples(self, link_id: str, hours: int | None = None) -> dict:
        return self._r("GET", f"/metering/links/{link_id}/samples", params={"hours": hours})

    def usage(self, hours: int | None = None) -> dict:
        return self._r("GET", "/metering/usage", params={"hours": hours})

    def settlement_csv(self, hours: int | None = None) -> str:
        return self._r("GET", "/metering/settlement.csv", params={"hours": hours})


class Decisions(_Resource):
    """AI SLA routing decisions, each with its reason."""

    def list(self, site_id: str | None = None, class_name: str | None = None, limit: int = 100) -> list[dict]:
        return self._r("GET", "/decisions", params={"site_id": site_id, "class_name": class_name, "limit": limit})

    def get(self, decision_id: int) -> dict:
        return self._r("GET", f"/decisions/{decision_id}")


class Storm(_Resource):
    """Storm Mode, per site."""

    def set(self, site_id: str, on: bool) -> dict:
        return self._r("POST", f"/sites/{site_id}/storm", json={"on": on})


class ApiKeys(_Resource):
    def list(self) -> list[dict]:
        return self._r("GET", "/auth/api-keys")

    def create(self, name: str, days: int | None = None, scopes: list[str] | None = None) -> dict:
        """The new key is in "token", shown only this once. `scopes` limits it
        (connect, commai:read, commai:write, commai:notes, commai:admin)."""
        return self._r("POST", "/auth/api-keys", json={"name": name, "days": days, "scopes": scopes})

    def get(self, key_id: int) -> dict:
        return self._r("GET", f"/auth/api-keys/{key_id}")

    def revoke(self, key_id: int) -> None:
        self._r("DELETE", f"/auth/api-keys/{key_id}")


class Links(_Resource):
    """Carrier links: customers their own, carriers theirs."""

    def get(self, link_id: str) -> dict:
        return self._r("GET", f"/links/{link_id}")

    def for_site(self, site_id: str) -> list[dict]:
        return self._r("GET", f"/sites/{site_id}/links")


class Integrations(_Resource):
    """Connectors and webhooks (ADR 0026). Secrets are write-only; each is simulated until it is live."""

    def catalogue(self) -> dict:
        """Event kinds, connectors with the settings each needs, and cloud on-ramp adapters."""
        return self._r("GET", "/integrations/catalogue")

    def list(self) -> list[dict]:
        return self._r("GET", "/integrations")

    def get(self, integration_id: int) -> dict:
        return self._r("GET", f"/integrations/{integration_id}")

    def create(
        self,
        provider: str,
        name: str,
        *,
        config: dict | None = None,
        secrets: dict | None = None,
        event_types: list[str] | None = None,
        site_ids: list[str] | None = None,
        min_severity: str = "info",
    ) -> dict:
        """A webhook's signing secret is in "signing_secret", shown only this once."""
        body = {
            k: v
            for k, v in {
                "provider": provider,
                "name": name,
                "config": config,
                "secrets": secrets,
                "event_types": event_types,
                "site_ids": site_ids,
                "min_severity": min_severity,
            }.items()
            if v is not None
        }
        return self._r("POST", "/integrations", json=body)

    def update(self, integration_id: int, **changes: Any) -> dict:
        return self._r("PATCH", f"/integrations/{integration_id}", json=changes)

    def delete(self, integration_id: int) -> None:
        self._r("DELETE", f"/integrations/{integration_id}")

    def test(self, integration_id: int) -> dict:
        """Send a test.ping now; returns the delivery."""
        return self._r("POST", f"/integrations/{integration_id}/test")

    def deliveries(self, integration_id: int, limit: int = 50) -> list[dict]:
        return self._r("GET", f"/integrations/{integration_id}/deliveries", params={"limit": limit})

    def retry(self, integration_id: int, delivery_id: int) -> dict:
        return self._r("POST", f"/integrations/{integration_id}/deliveries/{delivery_id}/retry")

    def sync(self, integration_id: int) -> dict:
        """NetBox: push to or pull from NetBox now."""
        return self._r("POST", f"/integrations/{integration_id}/sync")

    def metrics(self, openmetrics: bool = False) -> str:
        """The Prometheus (or OpenMetrics) text for this key's organisation."""
        accept = "application/openmetrics-text; version=1.0.0" if openmetrics else "text/plain"
        return self._r("GET", "/metrics", headers={"Accept": accept})


class Hooks(_Resource):
    """REST hooks, as Zapier, Make and n8n use them."""

    def subscribe(self, target_url: str, events: list[str] | None = None, platform: str = "other") -> dict:
        body = {"target_url": target_url, "platform": platform, **({"events": events} if events else {})}
        return self._r("POST", "/hooks", json=body)

    def unsubscribe(self, hook_id: int) -> None:
        self._r("DELETE", f"/hooks/{hook_id}")

    def sample(self, event: str = "path.down") -> list[dict]:
        return self._r("GET", "/hooks/sample", params={"event": event})


class Notices(_Resource):
    """Carrier fault and maintenance notices. Carriers post about their own links only."""

    def list(self) -> list[dict]:
        return self._r("GET", "/notices")

    def get(self, notice_id: int) -> dict:
        return self._r("GET", f"/notices/{notice_id}")

    def post(self, kind: str, title: str, link_ids: list[str], **fields: Any) -> dict:
        """Carriers: kind is maintenance (give starts_at and ends_at) or fault."""
        return self._r("POST", "/carrier/notices", json={"kind": kind, "title": title, "link_ids": link_ids, **fields})

    def update(self, notice_id: int, **changes: Any) -> dict:
        return self._r("PATCH", f"/carrier/notices/{notice_id}", json=changes)


class Onramps(_Resource):
    """Dedicated cloud on-ramps: AWS Direct Connect, Azure ExpressRoute, Google Partner Interconnect, Megaport."""

    def adapters(self) -> list[dict]:
        return self._r("GET", "/onramps/adapters")

    def list(self, customer_id: str) -> list[dict]:
        return self._r("GET", f"/customers/{customer_id}/onramps")

    def get(self, customer_id: str, onramp_id: int) -> dict:
        return self._r("GET", f"/customers/{customer_id}/onramps/{onramp_id}")

    def create(self, customer_id: str, provider: str, bandwidth_mbps: int, **fields: Any) -> dict:
        body = {"provider": provider, "bandwidth_mbps": bandwidth_mbps, **fields}
        return self._r("POST", f"/customers/{customer_id}/onramps", json=body)

    def refresh(self, customer_id: str, onramp_id: int) -> dict:
        return self._r("POST", f"/customers/{customer_id}/onramps/{onramp_id}/refresh")

    def delete(self, customer_id: str, onramp_id: int) -> dict:
        return self._r("DELETE", f"/customers/{customer_id}/onramps/{onramp_id}")


def verify_event(secret: str, headers: dict, body: bytes, tolerance_s: int = 300, now: int | None = None) -> bool:
    """Check a Connect webhook (a CloudEvent signed per Standard Webhooks): webhook-id,
    webhook-timestamp and webhook-signature headers, and the raw body."""
    import base64
    import hashlib
    import hmac
    import time

    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("webhook-id", ""), h.get("webhook-timestamp", ""), h.get("webhook-signature", "")
    try:
        stamp = int(ts)
    except ValueError:
        return False
    if abs((int(time.time()) if now is None else now) - stamp) > tolerance_s:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    want = "v1," + base64.b64encode(mac).decode()
    return any(hmac.compare_digest(want, s) for s in sigs.split())
