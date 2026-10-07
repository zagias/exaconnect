"""Freshdesk connector (ADR 0029): tickets linked to conversations.

Written from Freshdesk's public API v2 (tickets, notes, replies, search).
Freshdesk has no OAuth for API access: the business pastes an agent's API
key through secure entry (HTTP Basic, key as the user name, "X" as the
password), and sets its Freshdesk domain. ExaCarib registers nothing with
Freshdesk; ``feature/integration-freshdesk`` must be switched on before a
connection leaves the simulated stand-in.

- Idempotent creates: the ticket is tagged with a reference from the
  idempotency key, searched for before creating.
- Pages: the ``Link: <...>; rel="next"`` header. Rate limits: 429 with
  Retry-After.
- Status back: Freshdesk automation webhooks are not signed, so CommAI gives
  the business a secret to send in an ``X-CommAI-Token`` header (compared in
  constant time) along with a per-business address.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import Any

from ..automation import oauth
from . import kit
from .helpdesk import Helpdesk
from .more_common import app_hook, hook_url

STATUS_IN = {2: "open", 3: "pending", 4: "solved", 5: "closed"}
STATUS_OUT = {"new": 2, "open": 2, "pending": 3, "on_hold": 3, "solved": 4, "closed": 5}
STATUS_WORDS = {
    "open": "open",
    "pending": "pending",
    "resolved": "solved",
    "closed": "closed",
    "waiting on customer": "pending",
    "waiting on third party": "on_hold",
}
PRIORITY_IN = {1: "low", 2: "normal", 3: "high", 4: "urgent"}
PRIORITY_OUT = {v: k for k, v in PRIORITY_IN.items()}


def _next_link(headers: dict) -> str | None:
    m = re.search(r'<([^>]+)>;\s*rel="next"', kit.header(headers, "Link"))
    return m.group(1) if m else None


class FreshdeskSim(kit.Simulator):
    app = "freshdesk"

    def error_body(self, status: int, message: str) -> Any:
        return {"code": "error", "message": message}

    @kit.Simulator.route("GET", r"/api/v2/agents/me$")
    def me(self, conn, c, req, m):
        return 200, {"id": 1, "contact": {"name": "CommAI"}}

    @kit.Simulator.route("POST", r"/api/v2/tickets$")
    def create(self, conn, c, req, m):
        b = req.body or {}
        if not b.get("subject") or not b.get("email"):
            return 400, {"description": "Validation failed", "errors": [{"field": "email", "code": "missing_field"}]}
        tid = self.new_id(conn, c, "ticket", digits=True)
        t = {
            "id": int(tid),
            "subject": b["subject"],
            "description": b.get("description", ""),
            "email": b["email"].lower(),
            "status": b.get("status", 2),
            "priority": b.get("priority", 1),
            "tags": b.get("tags", []),
            "updated_at": "2026-10-07T10:00:00Z",
        }
        self.put(conn, c, "ticket", tid, t)
        return 201, t

    @kit.Simulator.route("GET", r"/api/v2/search/tickets$")
    def search(self, conn, c, req, m):
        mt = re.search(r"tag:'([^']+)'", req.query.get("query", ""))
        hits = [t for t in self.all(conn, c, "ticket") if mt and mt.group(1) in t.get("tags", [])]
        return 200, {"results": hits, "total": len(hits)}

    @kit.Simulator.route("GET", r"/api/v2/tickets$")
    def list(self, conn, c, req, m):
        email = req.query.get("email", "").lower()
        hits = [t for t in self.all(conn, c, "ticket") if t["email"] == email]
        page, per = int(req.query.get("page", "1")), int(req.query.get("per_page", "30"))
        h = {}
        if page * per < len(hits):
            h["Link"] = (
                f'<{req.url.split("?")[0]}?{urllib.parse.urlencode({**req.query, "page": page + 1})}>; rel="next"'
            )
        return 200, hits[(page - 1) * per : page * per], h

    @kit.Simulator.route("GET", r"/api/v2/tickets/(\d+)$")
    def show(self, conn, c, req, m):
        t = self.get(conn, c, "ticket", m.group(1))
        return (200, t) if t else (404, self.error_body(404, "Ticket not found"))

    @kit.Simulator.route("PUT", r"/api/v2/tickets/(\d+)$")
    def update(self, conn, c, req, m):
        t = self.get(conn, c, "ticket", m.group(1))
        if not t:
            return 404, self.error_body(404, "Ticket not found")
        t.update({k: v for k, v in (req.body or {}).items() if k in ("status", "priority")})
        self.put(conn, c, "ticket", m.group(1), t)
        return 200, t

    @kit.Simulator.route("POST", r"/api/v2/tickets/(\d+)/(notes|reply)$")
    def note(self, conn, c, req, m):
        if not self.get(conn, c, "ticket", m.group(1)):
            return 404, self.error_body(404, "Ticket not found")
        nid = self.new_id(conn, c, "note", digits=True)
        n = {"id": int(nid), "body": (req.body or {}).get("body", ""), "private": m.group(2) == "notes"}
        self.put(conn, c, "note", nid, n)
        return 201, n


class Freshdesk(Helpdesk):
    app = "freshdesk"
    label = "Freshdesk"
    description = "Create, update and look up Freshdesk tickets, linked to the conversation they came from."
    auth = "credentials"
    credentials = (kit.Credential("api_key", "API key", pattern=r"[A-Za-z0-9]{10,64}"),)
    settings_fields = (
        kit.Setting(
            "domain", "Freshdesk domain (the part before .freshdesk.com)", r"[a-z0-9][a-z0-9-]{0,62}", required=True
        ),
    )
    simulator = FreshdeskSim()
    health_path = "/api/v2/agents/me"
    needs_from_exacarib = "Nothing to register: each business pastes an agent's Freshdesk API key."
    webhooks = "A Freshdesk automation rule calls CommAI with a secret header when a ticket's status changes."
    docs_url = "https://developers.freshdesk.com/api/"
    mapping_targets = {"ticket": ["subject", "description", "email", "name", "priority", "tags"]}

    def base_url(self, conn, connection: dict) -> str:
        return f"https://{self.settings(connection).get('domain', 'example')}.freshdesk.com"

    def live_auth_headers(self, conn, connection: dict) -> dict:
        key = oauth.credentials(conn, connection)["api_key"]
        return {"Authorization": "Basic " + base64.b64encode(f"{key}:X".encode()).decode()}

    def message(self, status: int, body: Any) -> str:
        if isinstance(body, dict) and (body.get("description") or body.get("message")):
            errs = body.get("errors") or []
            extra = f" ({errs[0].get('field')}: {errs[0].get('code')})" if errs and isinstance(errs[0], dict) else ""
            return f"Freshdesk answered {status}: {str(body.get('description') or body.get('message'))[:150]}{extra}"
        return super().message(status, body)

    def _t(self, connection: dict, t: dict) -> dict:
        return {
            "id": str(t.get("id", "")),
            "number": str(t.get("id", "")),
            "subject": t.get("subject") or "",
            "status": STATUS_IN.get(t.get("status"), str(t.get("status", ""))),
            "priority": PRIORITY_IN.get(t.get("priority"), ""),
            "updated_at": t.get("updated_at") or "",
            "url": f"{self.base_url(None, connection)}/a/tickets/{t.get('id')}",
        }

    def _get(self, conn, connection, ticket_id):
        if not ticket_id.isdigit():
            return None
        r = self.call(conn, connection, "GET", f"/api/v2/tickets/{ticket_id}", allow=(404,))
        return None if r.status == 404 else self._t(connection, r.body)

    def _list(self, conn, connection, email):
        items = self.paginate(
            conn,
            connection,
            "/api/v2/tickets",
            params={"email": email.lower(), "order_by": "updated_at", "order_type": "desc", "per_page": 30},
            items=lambda b: b if isinstance(b, list) else [],
            next_page=lambda r: _next_link(r.headers),
            max_items=100,
        )
        return [self._t(connection, t) for t in items]

    def _find_ref(self, conn, connection, ref):
        r = self.call(conn, connection, "GET", "/api/v2/search/tickets", params={"query": f"\"tag:'{ref}'\""})
        hits = (r.body or {}).get("results") or []
        return self._t(connection, hits[0]) if hits else None

    def _create_body(self, connection, inputs, ref):
        body = {
            "subject": inputs["subject"],
            "description": inputs["description"],
            "email": inputs["email"],
            "status": 2,
            "priority": PRIORITY_OUT.get(inputs.get("priority") or "normal", 2),
            "source": 7,  # chat
            "tags": ["commai", ref],
        }
        if inputs.get("name"):
            body["name"] = inputs["name"]
        return body

    def _create(self, conn, connection, inputs, ref, key):
        r = self.call(conn, connection, "POST", "/api/v2/tickets", json_body=self._create_body(connection, inputs, ref))
        return self._t(connection, r.body)

    def _update(self, conn, connection, ticket_id, status, priority):
        body: dict = {}
        if status:
            body["status"] = STATUS_OUT[status]
        if priority:
            body["priority"] = PRIORITY_OUT[priority]
        r = self.call(conn, connection, "PUT", f"/api/v2/tickets/{urllib.parse.quote(ticket_id)}", json_body=body)
        return self._t(connection, r.body)

    def _comment(self, conn, connection, ticket_id, body, public, key):
        path = f"/api/v2/tickets/{urllib.parse.quote(ticket_id)}/" + ("reply" if public else "notes")
        payload = {"body": body} if public else {"body": body, "private": True}
        r = self.call(conn, connection, "POST", path, json_body=payload)
        return {"id": (r.body or {}).get("id", ""), "public": public}

    # ---- webhooks ----------------------------------------------------------------------

    def register_webhooks(self, conn, connection: dict, actor: str) -> dict:
        """Freshdesk can't be set up from here: the answer is what to paste into
        an automation rule (Admin > Automations > Ticket updates). The secret
        is shown this once."""
        hook = app_hook(conn, connection["customer_id"], self.app, actor)
        return {
            "manual": True,
            "address": hook_url(hook),
            "method": "POST",
            "headers": {"X-CommAI-Token": hook["secret"]},
            "body": json.dumps(
                {"ticket_id": "{{ticket.id}}", "status": "{{ticket.status}}", "id": "{{ticket.id}}-{{ticket.status}}"}
            ),
            "when": "Ticket is updated: status is changed",
        }

    def verify_webhook(self, conn, connection, hook, headers, body, query) -> bool:
        return kit.same(kit.header(headers, "X-CommAI-Token"), hook.get("secret", ""))

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        try:
            d = json.loads(body)
        except ValueError:
            return []
        status = str(d.get("status", "")).strip().lower()
        if not d.get("ticket_id") or not status:
            return []
        return [
            {
                "type": "ticket.updated",
                "id": str(d.get("id") or ""),
                "data": {"ticket_id": str(d["ticket_id"]), "status": STATUS_WORDS.get(status, status)},
            }
        ]


kit.register(Freshdesk())
