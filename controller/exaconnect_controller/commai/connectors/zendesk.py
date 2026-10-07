"""Zendesk Support connector (ADR 0035): tickets linked to conversations.

Written from Zendesk's public Support API v2 (tickets, search, webhooks).
Sign-in is OAuth at the business's own subdomain
(https://{subdomain}.zendesk.com/oauth/authorizations/new) with ExaCarib's
global OAuth client (EXA_ZENDESK_CLIENT_ID/SECRET). Until that is set and
ExaCarib switches ``feature/integration-zendesk`` on, a connection runs on the
simulated stand-in below.

- Idempotent creates: the ticket carries ``external_id`` = a reference from
  the idempotency key, looked up before creating, and the create sends
  Zendesk's ``Idempotency-Key`` header too.
- Rate limits: 429 with Retry-After (handled by the kit).
- Status back: a Zendesk webhook subscribed to ticket status changes, signed
  with ``X-Zendesk-Webhook-Signature`` = base64 HMAC-SHA256 of
  timestamp + body under the webhook's signing secret.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from typing import Any

from ..automation import oauth
from . import kit
from .helpdesk import Helpdesk
from .more_common import app_hook, hook_url, set_hook_secret

TO_COMMAI = {
    "new": "new",
    "open": "open",
    "pending": "pending",
    "hold": "on_hold",
    "solved": "solved",
    "closed": "closed",
}
TO_ZENDESK = {v: k for k, v in TO_COMMAI.items()}

oauth.PROVIDERS["zendesk"] = oauth.Provider(
    "zendesk",
    "Zendesk",
    "https://{subdomain}.zendesk.com/oauth/authorizations/new",
    "https://{subdomain}.zendesk.com/oauth/tokens",
    ("read", "write"),
    "EXA_ZENDESK",
    expiring=False,
)


def _ticket(t: dict, subdomain: str) -> dict:
    return {
        "id": str(t.get("id", "")),
        "number": str(t.get("id", "")),
        "subject": t.get("subject") or "",
        "status": TO_COMMAI.get(t.get("status") or "", t.get("status") or ""),
        "priority": t.get("priority") or "",
        "updated_at": t.get("updated_at") or "",
        "url": f"https://{subdomain}.zendesk.com/agent/tickets/{t.get('id')}" if subdomain else "",
    }


class ZendeskSim(kit.Simulator):
    """Answers like Zendesk Support API v2."""

    app = "zendesk"

    def error_body(self, status: int, message: str) -> Any:
        return {"error": "RecordNotFound" if status == 404 else "Error", "description": message}

    @kit.Simulator.route("GET", r"/api/v2/users/me\.json$")
    def me(self, conn, c, req, m):
        return 200, {"user": {"id": 1, "name": "Jibsy", "role": "admin"}}

    @kit.Simulator.route("POST", r"/api/v2/tickets\.json$")
    def create(self, conn, c, req, m):
        t = (req.body or {}).get("ticket") or {}
        if not t.get("subject") or not (t.get("comment") or {}).get("body"):
            return 422, {"error": "RecordInvalid", "description": "Record validation errors"}
        tid = self.new_id(conn, c, "ticket", digits=True)
        now = dt.datetime.now(dt.UTC).isoformat()
        rec = {
            "id": int(tid),
            "subject": t["subject"],
            "description": t["comment"]["body"],
            "status": "new",
            "priority": t.get("priority"),
            "external_id": t.get("external_id"),
            "requester": t.get("requester") or {},
            "tags": t.get("tags") or [],
            "created_at": now,
            "updated_at": now,
        }
        self.put(conn, c, "ticket", tid, rec)
        return 201, {"ticket": rec}

    @kit.Simulator.route("GET", r"/api/v2/tickets\.json$")
    def by_external(self, conn, c, req, m):
        ext = req.query.get("external_id", "")
        return 200, {"tickets": [t for t in self.all(conn, c, "ticket") if ext and t.get("external_id") == ext]}

    @kit.Simulator.route("GET", r"/api/v2/tickets/(\d+)\.json$")
    def show(self, conn, c, req, m):
        t = self.get(conn, c, "ticket", m.group(1))
        return (200, {"ticket": t}) if t else (404, self.error_body(404, "Not found"))

    @kit.Simulator.route("PUT", r"/api/v2/tickets/(\d+)\.json$")
    def update(self, conn, c, req, m):
        t = self.get(conn, c, "ticket", m.group(1))
        if not t:
            return 404, self.error_body(404, "Not found")
        body = (req.body or {}).get("ticket") or {}
        for k in ("status", "priority"):
            if body.get(k):
                t[k] = body[k]
        audit = None
        if body.get("comment"):
            comments = t.setdefault("comments", [])
            audit = {"id": len(comments) + 1, **body["comment"]}
            comments.append(audit)
        t["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
        self.put(conn, c, "ticket", m.group(1), t)
        out: dict = {"ticket": t}
        if audit:
            out["audit"] = {"id": audit["id"], "events": [{"type": "Comment", **audit}]}
        return 200, out

    @kit.Simulator.route("GET", r"/api/v2/search\.json$")
    def search(self, conn, c, req, m):
        q = req.query.get("query", "")
        email = q.split("requester:", 1)[1].strip() if "requester:" in q else ""
        hits = [t for t in self.all(conn, c, "ticket") if (t.get("requester") or {}).get("email", "").lower() == email]
        page = int(req.query.get("page", "1"))
        per = int(req.query.get("per_page", "100"))
        chunk = hits[(page - 1) * per : page * per]
        nxt = None
        if page * per < len(hits):
            nxt = f"{req.url.split('?')[0]}?" + urllib.parse.urlencode({**req.query, "page": page + 1})
        return 200, {"results": [{**t, "result_type": "ticket"} for t in chunk], "next_page": nxt, "count": len(hits)}

    @kit.Simulator.route("POST", r"/api/v2/webhooks$")
    def webhook(self, conn, c, req, m):
        w = {**((req.body or {}).get("webhook") or {}), "id": self.new_id(conn, c, "webhook")}
        self.put(conn, c, "webhook", w["id"], {**w, "secret": "sim-" + w["id"]})
        return 201, {"webhook": w}

    @kit.Simulator.route("GET", r"/api/v2/webhooks/([^/]+)/signing_secret$")
    def signing(self, conn, c, req, m):
        w = self.get(conn, c, "webhook", m.group(1))
        return (200, {"signing_secret": {"algorithm": "SHA256", "secret": w["secret"]}}) if w else (404, {})


class Zendesk(Helpdesk):
    app = "zendesk"
    label = "Zendesk"
    description = "Create, update and look up Zendesk tickets, linked to the conversation they came from."
    auth = "oauth"
    settings_fields = (kit.Setting("subdomain", "Zendesk subdomain", r"[a-z0-9][a-z0-9-]{0,62}", required=True),)
    simulator = ZendeskSim()
    health_path = "/api/v2/users/me.json"
    needs_from_exacarib = (
        "A Zendesk global OAuth client (request one from Zendesk for a multi-account app): "
        "EXA_ZENDESK_CLIENT_ID and EXA_ZENDESK_CLIENT_SECRET, redirect URI "
        "{EXA_PUBLIC_URL}/api/v1/commai/oauth/zendesk/callback."
    )
    webhooks = "A Zendesk webhook for ticket status changes, signed with HMAC-SHA256 (set up from Jibsy)."
    docs_url = "https://developer.zendesk.com/api-reference/"
    mapping_targets = {"ticket": ["subject", "comment.body", "priority", "requester.name", "requester.email", "tags"]}

    def base_url(self, conn, connection: dict) -> str:
        return f"https://{self.settings(connection).get('subdomain', 'example')}.zendesk.com"

    def message(self, status: int, body: Any) -> str:
        if isinstance(body, dict) and isinstance(body.get("description"), str):
            return f"Zendesk answered {status}: {body['description'][:200]}"
        return super().message(status, body)

    def _sub(self, connection: dict) -> str:
        return self.settings(connection).get("subdomain", "")

    def _get(self, conn, connection, ticket_id):
        r = self.call(conn, connection, "GET", f"/api/v2/tickets/{urllib.parse.quote(ticket_id)}.json", allow=(404,))
        return None if r.status == 404 else _ticket(r.body["ticket"], self._sub(connection))

    def _list(self, conn, connection, email):
        items = self.paginate(
            conn,
            connection,
            "/api/v2/search.json",
            params={
                "query": f"type:ticket requester:{email.lower()}",
                "sort_by": "updated_at",
                "sort_order": "desc",
                "per_page": 50,
            },
            items=lambda b: [t for t in b.get("results") or [] if t.get("result_type") == "ticket"],
            next_page=lambda r: r.body.get("next_page"),
            max_items=100,
        )
        return [_ticket(t, self._sub(connection)) for t in items]

    def _find_ref(self, conn, connection, ref):
        r = self.call(conn, connection, "GET", "/api/v2/tickets.json", params={"external_id": ref})
        found = (r.body or {}).get("tickets") or []
        return _ticket(found[0], self._sub(connection)) if found else None

    def _create_body(self, connection, inputs, ref):
        t: dict = {
            "subject": inputs["subject"],
            "comment": {"body": inputs["description"], "public": False},
            "requester": {"email": inputs["email"], **({"name": inputs["name"]} if inputs.get("name") else {})},
            "external_id": ref,
            "tags": ["commai"],
        }
        if inputs.get("priority"):
            t["priority"] = inputs["priority"]
        return {"ticket": t}

    def _create(self, conn, connection, inputs, ref, key):
        r = self.call(
            conn,
            connection,
            "POST",
            "/api/v2/tickets.json",
            json_body=self._create_body(connection, inputs, ref),
            headers={"Idempotency-Key": ref},
        )
        return _ticket(r.body["ticket"], self._sub(connection))

    def _update(self, conn, connection, ticket_id, status, priority):
        body: dict = {}
        if status:
            body["status"] = TO_ZENDESK[status]
        if priority:
            body["priority"] = priority
        r = self.call(
            conn, connection, "PUT", f"/api/v2/tickets/{urllib.parse.quote(ticket_id)}.json", json_body={"ticket": body}
        )
        return _ticket(r.body["ticket"], self._sub(connection))

    def _comment(self, conn, connection, ticket_id, body, public, key):
        r = self.call(
            conn,
            connection,
            "PUT",
            f"/api/v2/tickets/{urllib.parse.quote(ticket_id)}.json",
            json_body={"ticket": {"comment": {"body": body, "public": public}}},
            headers={"Idempotency-Key": kit.ref(key)},
        )
        return {"id": ((r.body or {}).get("audit") or {}).get("id", ""), "public": public}

    # ---- webhooks ----------------------------------------------------------------------

    def register_webhooks(self, conn, connection: dict, actor: str) -> dict:
        """Create the Zendesk webhook for status changes and keep its signing secret."""
        hook = app_hook(conn, connection["customer_id"], self.app, actor)
        r = self.call(
            conn,
            connection,
            "POST",
            "/api/v2/webhooks",
            json_body={
                "webhook": {
                    "name": "Jibsy by ExaCarib: ticket status",
                    "endpoint": hook_url(hook),
                    "http_method": "POST",
                    "request_format": "json",
                    "status": "active",
                    "subscriptions": ["zen:event-type:ticket.status_changed"],
                }
            },
        )
        wid = r.body["webhook"]["id"]
        s = self.call(conn, connection, "GET", f"/api/v2/webhooks/{wid}/signing_secret")
        set_hook_secret(conn, hook, s.body["signing_secret"]["secret"])
        return {"webhook_id": wid, "address": hook_url(hook), "manual": False}

    def verify_webhook(self, conn, connection, hook, headers, body, query) -> bool:
        ts = kit.header(headers, "X-Zendesk-Webhook-Signature-Timestamp")
        sig = kit.header(headers, "X-Zendesk-Webhook-Signature")
        try:
            when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return False
        if abs((dt.datetime.now(dt.UTC) - when).total_seconds()) > 300:
            return False
        return kit.same(sig, kit.hmac_b64(hook.get("secret", ""), ts.encode() + body))

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        try:
            d = json.loads(body)
        except ValueError:
            return []
        if d.get("type") != "zen:event-type:ticket.status_changed":
            return []
        detail = d.get("detail") or {}
        status = str((d.get("event") or {}).get("current") or detail.get("status") or "").lower()
        return [
            {
                "type": "ticket.updated",
                "id": str(d.get("id", "")),
                "data": {"ticket_id": str(detail.get("id", "")), "status": TO_COMMAI.get(status, status)},
            }
        ]


kit.register(Zendesk())
