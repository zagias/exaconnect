"""CommAI: the shared inbox, private notes, webhooks, events and actions
(ADR 0016 in the ExaConnect repository).

    from exaconnect import ExaConnect
    exa = ExaConnect("https://connect.exacarib.com")
    inbox = exa.commai()                      # your business
    for c in inbox.conversations.list(view="unassigned")["items"]:
        print(c["contact_name"], c["preview"])
    inbox.conversations.reply(c["id"], "Thanks, we're on it.", idempotency_key="order-123-ack")

Receiving webhooks:

    from exaconnect.commai import verify_webhook
    ok = verify_webhook(secret, request.headers["X-ExaCarib-Timestamp"], body_bytes,
                        request.headers["X-ExaCarib-Signature"])
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import ExaConnect


def verify_webhook(secret: str, timestamp: str, body: bytes, signature: str, tolerance_s: int = 300) -> bool:
    """True if a CommAI webhook is genuine and recent. De-duplicate on the
    X-ExaCarib-Event-Id header: a retried delivery carries the same id."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v1={mac}", signature)


class CommAI:
    def __init__(self, exa: ExaConnect, customer_id: str):
        self.customer_id = str(customer_id)
        base = f"/commai/customers/{self.customer_id}"
        r = exa._request
        self.contacts = _Contacts(r, base)
        self.conversations = _Conversations(r, base)
        self.webhooks = _Webhooks(r, base)
        self.events = _Events(r, base)
        self.actions = _Actions(r, base)
        self._r, self._base = r, base

    def settings(self) -> dict:
        return self._r("GET", f"{self._base}/settings")

    def teams(self) -> list[dict]:
        return self._r("GET", f"{self._base}/teams")

    def connectors(self) -> list[dict]:
        """Every app CommAI connects to, with the exact actions each supports."""
        return self._r("GET", f"{self._base}/connectors")


class _Part:
    def __init__(self, r, base: str):
        self._r, self._base = r, base

    @staticmethod
    def _key(idempotency_key: str | None) -> dict:
        return {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}


class _Contacts(_Part):
    def list(self, q: str = "", limit: int = 50, before: str | None = None) -> dict:
        return self._r("GET", f"{self._base}/contacts", params={"q": q, "limit": limit, "before": before})

    def get(self, contact_id: str) -> dict:
        return self._r("GET", f"{self._base}/contacts/{contact_id}")

    def create(self, name: str = "", email: str = "", phone: str = "", **extra: Any) -> dict:
        return self._r("POST", f"{self._base}/contacts", json={"name": name, "email": email, "phone": phone, **extra})


class _Conversations(_Part):
    def list(
        self, view: str = "all", state: str | None = None, q: str = "", limit: int = 50, before: str | None = None
    ) -> dict:
        """{"items": [...], "next": cursor}. Pass `before=next` for the next page."""
        return self._r(
            "GET",
            f"{self._base}/conversations",
            params={"view": view, "state": state, "q": q, "limit": limit, "before": before},
        )

    def get(self, conversation_id: str) -> dict:
        return self._r("GET", f"{self._base}/conversations/{conversation_id}")

    def start(
        self,
        address: str,
        body: str = "",
        channel: str = "api",
        name: str = "",
        subject: str = "",
        external_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        return self._r(
            "POST",
            f"{self._base}/conversations",
            headers=self._key(idempotency_key),
            json={
                "channel": channel,
                "address": address,
                "body": body,
                "name": name,
                "subject": subject,
                "external_id": external_id,
            },
        )

    def reply(
        self,
        conversation_id: str,
        body: str = "",
        template: str = "",
        take_over: bool = False,
        idempotency_key: str | None = None,
    ) -> dict:
        """Send on the conversation's own channel. Reuse `idempotency_key` when
        retrying so the customer never gets the message twice."""
        return self._r(
            "POST",
            f"{self._base}/conversations/{conversation_id}/messages",
            headers=self._key(idempotency_key),
            json={"body": body, "template": template, "take_over": take_over},
        )

    def notes(self, conversation_id: str) -> list[dict]:
        """Private notes. Needs a key with the commai:notes scope."""
        return self._r("GET", f"{self._base}/conversations/{conversation_id}/notes")

    def add_note(self, conversation_id: str, body: str, mentions: list[str] | None = None) -> dict:
        return self._r(
            "POST",
            f"{self._base}/conversations/{conversation_id}/notes",
            json={"body": body, "mentions": mentions or []},
        )

    def assign(
        self, conversation_id: str, assignee_id: str | None = None, team_id: str | None = None, reason: str = ""
    ) -> dict:
        return self._r(
            "POST",
            f"{self._base}/conversations/{conversation_id}/assign",
            json={"assignee_id": assignee_id, "team_id": team_id, "reason": reason},
        )

    def set_state(self, conversation_id: str, state: str, reason: str = "") -> dict:
        return self._r(
            "POST", f"{self._base}/conversations/{conversation_id}/state", json={"state": state, "reason": reason}
        )

    def take_over(self, conversation_id: str) -> dict:
        return self._r("POST", f"{self._base}/conversations/{conversation_id}/takeover")

    def export(self, conversation_id: str) -> dict:
        return self._r("GET", f"{self._base}/conversations/{conversation_id}/export")


class _Webhooks(_Part):
    def list(self) -> list[dict]:
        return self._r("GET", f"{self._base}/webhooks")

    def create(self, url: str, events: list[str] | None = None, description: str = "") -> dict:
        """The signing secret is in "secret", shown only this once."""
        return self._r(
            "POST", f"{self._base}/webhooks", json={"url": url, "events": events or ["*"], "description": description}
        )

    def delete(self, endpoint_id: str) -> None:
        self._r("DELETE", f"{self._base}/webhooks/{endpoint_id}")

    def test(self, endpoint_id: str) -> dict:
        return self._r("POST", f"{self._base}/webhooks/{endpoint_id}/test")

    def deliveries(self, endpoint_id: str) -> list[dict]:
        return self._r("GET", f"{self._base}/webhooks/{endpoint_id}/deliveries")


class _Events(_Part):
    def list(self, after: int = 0, type: str | None = None, limit: int = 100) -> dict:
        return self._r("GET", f"{self._base}/events", params={"after": after, "type": type, "limit": limit})


class _Actions(_Part):
    def list(self, status: str | None = None) -> list[dict]:
        return self._r("GET", f"{self._base}/actions", params={"status": status})

    def run(self, app: str, action: str, inputs: dict, conversation_id: str | None = None, test: bool = False) -> dict:
        return self._r(
            "POST",
            f"{self._base}/actions",
            json={"app": app, "action": action, "inputs": inputs, "conversation_id": conversation_id, "test": test},
        )

    def approve(self, run_id: str) -> dict:
        return self._r("POST", f"{self._base}/actions/{run_id}/approve")
