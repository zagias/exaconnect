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

import base64
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
        self.routing = _Routing(r, base)
        self.ai = _AI(r, base)
        self.workflows = _Workflows(r, base)
        self.integrations = _Integrations(r, base)
        self.reports = _Reports(r, base)
        self.voice = _Voice(r, base)
        self._r, self._base = r, base

    def settings(self) -> dict:
        return self._r("GET", f"{self._base}/settings")

    def update_settings(self, **fields: Any) -> dict:
        """mode, timezone, first_reply_minutes, resolve_hours."""
        return self._r("PATCH", f"{self._base}/settings", json=fields)

    def service_targets(self) -> dict:
        return self._r("GET", f"{self._base}/service-targets")

    def set_service_targets(
        self,
        reminders: bool = True,
        remind_percent: int = 80,
        escalate: bool = True,
        escalate_team_id: str | None = None,
    ) -> dict:
        """Built-in reminders and escalations for first-reply and resolution targets."""
        return self._r(
            "PUT",
            f"{self._base}/service-targets",
            json={
                "reminders": reminders,
                "remind_percent": remind_percent,
                "escalate": escalate,
                "escalate_team_id": escalate_team_id,
            },
        )

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

    def update(self, contact_id: str, **fields: Any) -> dict:
        """name, email, phone, language, external_ref."""
        return self._r("PATCH", f"{self._base}/contacts/{contact_id}", json=fields)

    def history(self, contact_id: str, limit: int = 50) -> dict:
        """Conversations, calls, open requests, linked records and bookings."""
        return self._r("GET", f"{self._base}/contacts/{contact_id}/history", params={"limit": limit})

    def identities(self, contact_id: str) -> list[dict]:
        return self._r("GET", f"{self._base}/contacts/{contact_id}/identities")

    def add_identity(
        self, contact_id: str, channel: str, address: str, verified: bool = False, evidence: str = ""
    ) -> dict:
        """Add a channel address. `verified` says your own system checked the
        person owns it; say how in `evidence`."""
        return self._r(
            "POST",
            f"{self._base}/contacts/{contact_id}/identities",
            json={"channel": channel, "address": address, "verified": verified, "evidence": evidence},
        )

    def verify_identity(self, contact_id: str, identity_id: str, evidence: str, verified: bool = True) -> dict:
        return self._r(
            "POST",
            f"{self._base}/contacts/{contact_id}/identities/{identity_id}/verify",
            json={"verified": verified, "evidence": evidence},
        )

    def remove_identity(self, contact_id: str, identity_id: str) -> None:
        self._r("DELETE", f"{self._base}/contacts/{contact_id}/identities/{identity_id}")


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
        attachments: list[str] | None = None,
    ) -> dict:
        """Send on the conversation's own channel. Reuse `idempotency_key` when
        retrying so the customer never gets the message twice. `attachments`
        are ids from upload()."""
        return self._r(
            "POST",
            f"{self._base}/conversations/{conversation_id}/messages",
            headers=self._key(idempotency_key),
            json={"body": body, "template": template, "take_over": take_over, "attachments": attachments or []},
        )

    def upload(self, conversation_id: str, name: str, content_type: str, data: bytes) -> dict:
        """A file to attach to a reply (checked against what the channel can carry)."""
        return self._r(
            "POST",
            f"{self._base}/conversations/{conversation_id}/files",
            json={"name": name, "type": content_type, "data": base64.b64encode(data).decode()},
        )

    def update(
        self,
        conversation_id: str,
        *,
        priority: str | None = None,
        tags: list[str] | None = None,
        subject: str | None = None,
        intent: str | None = None,
    ) -> dict:
        """Change priority, tags, subject or intent. Only what you pass changes."""
        fields = {"priority": priority, "tags": tags, "subject": subject, "intent": intent}
        return self._r(
            "PATCH",
            f"{self._base}/conversations/{conversation_id}",
            json={k: v for k, v in fields.items() if v is not None},
        )

    def messages(self, conversation_id: str, cursor: str = "", limit: int = 100) -> dict:
        """One page of messages, oldest first: {"items", "next"}; pass `cursor=next` for more."""
        return self._r(
            "GET",
            f"{self._base}/conversations/{conversation_id}/messages",
            params={"cursor": cursor, "limit": limit},
        )

    def typing(self, conversation_id: str, typing: bool = True) -> dict:
        return self._r("POST", f"{self._base}/conversations/{conversation_id}/typing", json={"typing": typing})

    def presence(self, conversation_id: str) -> dict:
        return self._r("GET", f"{self._base}/conversations/{conversation_id}/presence")

    def hand_back(self, conversation_id: str) -> dict:
        """Give the conversation back to the AI agent."""
        return self._r("POST", f"{self._base}/conversations/{conversation_id}/handback")

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

    def set_state(self, conversation_id: str, state: str, reason: str = "", snoozed_until: str | None = None) -> dict:
        """`snoozed_until` (ISO time) with state "snoozed": it wakes by itself then."""
        body: dict[str, Any] = {"state": state, "reason": reason}
        if snoozed_until:
            body["snoozed_until"] = snoozed_until
        return self._r("POST", f"{self._base}/conversations/{conversation_id}/state", json=body)

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


class _Routing(_Part):
    """Teams, people and routing rules (by channel, language, intent, keywords and skills)."""

    def teams(self) -> list[dict]:
        return self._r("GET", f"{self._base}/teams")

    def create_team(self, name: str, members: list[str] | None = None, skills: list[str] | None = None) -> dict:
        return self._r(
            "POST", f"{self._base}/teams", json={"name": name, "members": members or [], "skills": skills or []}
        )

    def update_team(
        self, team_id: str, name: str, members: list[str] | None = None, skills: list[str] | None = None
    ) -> dict:
        return self._r(
            "PUT",
            f"{self._base}/teams/{team_id}",
            json={"name": name, "members": members or [], "skills": skills or []},
        )

    def delete_team(self, team_id: str) -> None:
        self._r("DELETE", f"{self._base}/teams/{team_id}")

    def members(self) -> list[dict]:
        return self._r("GET", f"{self._base}/members")

    def set_member(
        self,
        user_id: str,
        seat: str = "agent",
        skills: list[str] | None = None,
        languages: list[str] | None = None,
        available: bool = True,
    ) -> dict:
        return self._r(
            "PUT",
            f"{self._base}/members/{user_id}",
            json={"seat": seat, "skills": skills or [], "languages": languages or ["en"], "available": available},
        )

    def rules(self) -> list[dict]:
        return self._r("GET", f"{self._base}/routing-rules")

    def create_rule(
        self,
        name: str,
        match: dict | None = None,
        team_id: str | None = None,
        priority: str | None = None,
        skills: list[str] | None = None,
        position: int = 100,
        queue: str = "",
    ) -> dict:
        """`match` takes channel, language, intent and keywords; `skills` are
        what the person picked must have."""
        return self._r(
            "POST",
            f"{self._base}/routing-rules",
            json={
                "name": name,
                "match": match or {},
                "team_id": team_id,
                "priority": priority,
                "skills": skills or [],
                "position": position,
                "queue": queue,
            },
        )

    def delete_rule(self, rule_id: str) -> None:
        self._r("DELETE", f"{self._base}/routing-rules/{rule_id}")


class _AI(_Part):
    """The AI agents: profile, knowledge, what the AI did, gaps."""

    def status(self) -> dict:
        return self._r("GET", f"{self._base}/ai/status")

    def profile(self) -> dict:
        return self._r("GET", f"{self._base}/ai/profile")

    def set_profile(self, **fields: Any) -> dict:
        """name, tone, greeting, languages, memory, escalation... (needs commai:admin)."""
        return self._r("PUT", f"{self._base}/ai/profile", json=fields)

    def knowledge(self) -> list[dict]:
        return self._r("GET", f"{self._base}/ai/knowledge")

    def add_knowledge(self, title: str, body: str, source_url: str = "", approved: bool = False) -> dict:
        return self._r(
            "POST",
            f"{self._base}/ai/knowledge",
            json={"title": title, "body": body, "source_url": source_url, "approved": approved},
        )

    def approve_knowledge(self, source_id: str) -> dict:
        return self._r("POST", f"{self._base}/ai/knowledge/{source_id}/approve")

    def delete_knowledge(self, source_id: str) -> None:
        self._r("DELETE", f"{self._base}/ai/knowledge/{source_id}")

    def search(self, q: str) -> Any:
        return self._r("GET", f"{self._base}/ai/knowledge/search", params={"q": q})

    def runs(self, conversation_id: str) -> list[dict]:
        """Each AI answer in a conversation: sources, proposals and outcome."""
        return self._r("GET", f"{self._base}/ai/conversations/{conversation_id}/runs")

    def gaps(self) -> Any:
        return self._r("GET", f"{self._base}/ai/gaps")


class _Workflows(_Part):
    def list(self) -> list[dict]:
        return self._r("GET", f"{self._base}/workflows")

    def get(self, workflow_id: str) -> dict:
        return self._r("GET", f"{self._base}/workflows/{workflow_id}")

    def runs(self, workflow_id: str, test: bool | None = None, limit: int = 50) -> list[dict]:
        return self._r("GET", f"{self._base}/workflows/{workflow_id}/runs", params={"test": test, "limit": limit})

    def run(self, run_id: str) -> dict:
        """One run's status and step log."""
        return self._r("GET", f"{self._base}/workflow-runs/{run_id}")

    def pause(self, workflow_id: str) -> dict:
        return self._r("POST", f"{self._base}/workflows/{workflow_id}/pause")

    def resume(self, workflow_id: str) -> dict:
        return self._r("POST", f"{self._base}/workflows/{workflow_id}/resume")


class _Integrations(_Part):
    def list(self) -> list[dict]:
        return self._r("GET", f"{self._base}/integrations")

    def health(self, app: str) -> dict:
        """Sign-in status, last success, failures and the workflows affected."""
        return self._r("GET", f"{self._base}/integrations/{app}/health")


class _Reports(_Part):
    def outcomes(self, start: str | None = None, end: str | None = None) -> dict:
        """Outcomes counted from recorded events. `start` and `end` are ISO times."""
        return self._r("GET", f"{self._base}/reports/outcomes", params={"from": start, "to": end})

    def usage(self, start: str | None = None, end: str | None = None) -> dict:
        return self._r("GET", f"{self._base}/reports/usage", params={"from": start, "to": end})

    def limits(self) -> list[dict]:
        return self._r("GET", f"{self._base}/usage-limits")

    def set_limit(self, meter: str, monthly_alert: float | None = None, monthly_hard: float | None = None) -> dict:
        """A budget for a meter (message_out:whatsapp, ai_reply...): an alert and a hard stop."""
        return self._r(
            "PUT",
            f"{self._base}/usage-limits/{meter}",
            json={"monthly_alert": monthly_alert, "monthly_hard": monthly_hard},
        )


class _Voice(_Part):
    def summary(self) -> dict:
        return self._r("GET", f"{self._base}/voice")

    def calls(self, limit: int = 100) -> list[dict]:
        """Call records (CDRs)."""
        return self._r("GET", f"{self._base}/voice/calls", params={"limit": limit})

    def spend(self) -> dict:
        return self._r("GET", f"{self._base}/voice/spend")

    def start_ai_call(self, caller: str = "") -> dict:
        """A browser call with the AI agent: text in, text out."""
        return self._r("POST", f"{self._base}/ai/calls", json={"caller": caller})

    def say(self, conversation_id: str, text: str) -> dict:
        """What the caller said on an AI call; returns the AI's reply."""
        return self._r("POST", f"{self._base}/ai/calls/{conversation_id}/turns", json={"text": text})

    def end_ai_call(self, conversation_id: str) -> dict:
        return self._r("POST", f"{self._base}/ai/calls/{conversation_id}/end")
