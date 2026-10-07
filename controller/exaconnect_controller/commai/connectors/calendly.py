"""Calendly connector through the Calendly API v2 (ADR 0034).

- Read: event types (``GET /event_types?user=``) and a customer's scheduled
  events (``GET /scheduled_events?user=&invitee_email=``), following
  ``pagination.next_page``.
- Create: a single-use booking link (``POST /scheduling_links`` with
  ``max_event_count: 1``) the AI sends to the customer, who picks a time in
  Calendly. Links are kept by idempotency key so a retry sends the same link.
- Cancel: ``POST /scheduled_events/{uuid}/cancellation`` (sensitive: a person
  approves); an event already cancelled counts as done.
- Webhooks: ``invitee.created`` and ``invitee.canceled`` arrive signed with
  ``Calendly-Webhook-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">``
  using the signing key Jibsy gave when subscribing; older than three
  minutes is refused.

Sign-in is OAuth 2.0; the token answer names the user (``owner``) and
organisation URIs. Not live until ExaCarib registers a Calendly developer app
(EXA_CALENDLY_CLIENT_ID, EXA_CALENDLY_CLIENT_SECRET) and switches
``feature/integration-calendly`` on; until then a connection runs on a stand-in.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import psycopg

from . import ActionSpec, ConnectorError, Field
from .kit import KitConnector, Request, Simulator, fresh, header, hmac_hex, register, same

API = "https://api.calendly.com"
STANDIN = "https://standin.api.calendly.com"
STANDIN_USER = f"{STANDIN}/users/STANDINUSER"


class Calendly(KitConnector):
    app = "calendly"
    label = "Calendly"
    category = "calendar"
    description = "Send a customer a one-time Calendly booking link, see their scheduled events and cancel one."
    needs_from_exacarib = "A Calendly developer app (OAuth) and a webhook signing key per business (made by Jibsy)."
    webhooks = "Calendly webhooks (invitee created and cancelled), checked by Calendly-Webhook-Signature."
    docs_url = "https://developer.calendly.com/api-docs"
    actions = {
        "list_event_types": ActionSpec("list_event_types", "List event types", "read"),
        "find_events": ActionSpec(
            "find_events", "Find a customer's events", "read", fields=(Field("email", "Customer email", "email"),)
        ),
        "create_booking_link": ActionSpec(
            "create_booking_link",
            "Create a one-time booking link",
            "create",
            fields=(Field("event_type", "Event type"),),
        ),
        "cancel_event": ActionSpec(
            "cancel_event",
            "Cancel an event",
            "cancel",
            sensitive=True,
            fields=(Field("event_id", "Event"), Field("reason", "Reason", required=False)),
        ),
    }
    golive_criteria = {
        "webhooks": "Webhook subscriptions created and signature checks tested on a Calendly test account."
    }

    def __init__(self):
        self.simulator = CalendlyStandIn()

    def base_url(self, conn, connection: dict) -> str:
        return STANDIN if self.simulated(connection) else API

    def _owner(self, connection: dict) -> str:
        if self.simulated(connection):
            return STANDIN_USER
        owner = (connection.get("settings") or {}).get("owner", "")
        if not owner.startswith(f"{API}/users/"):
            raise ConnectorError("Calendly did not name the signed-in user. Sign in again.", "expired_signin")
        return owner

    def cause(self, status: int, body: Any) -> str:
        if isinstance(body, dict) and body.get("title") == "Permission Denied":
            return "permission"
        return super().cause(status, body)

    def message(self, status: int, body: Any) -> str:
        if isinstance(body, dict) and (body.get("title") or body.get("message")):
            return f"Calendly answered {status}: {body.get('title', '')} {str(body.get('message', ''))[:160]}".strip()
        return super().message(status, body)

    def _pages(self, conn, connection, path, params, limit=50):
        return list(
            self.paginate(
                conn,
                connection,
                path,
                params=params,
                items=lambda b: b.get("collection", []) if isinstance(b, dict) else [],
                next_page=lambda r: (
                    (r.body.get("pagination") or {}).get("next_page") if isinstance(r.body, dict) else None
                ),
                max_items=limit,
            )
        )

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "list_event_types":
            rows = self._pages(
                conn, connection, "/event_types", {"user": self._owner(connection), "active": "true", "count": 20}
            )
            return {
                "event_types": [{"uri": r["uri"], "name": r.get("name"), "duration": r.get("duration")} for r in rows]
            }
        if action == "find_events":
            rows = self._pages(
                conn,
                connection,
                "/scheduled_events",
                {"user": self._owner(connection), "invitee_email": inputs["email"], "status": "active", "count": 20},
            )
            return {
                "events": [
                    {
                        "id": r["uri"].rsplit("/", 1)[-1],
                        "name": r.get("name"),
                        "start": r.get("start_time"),
                        "status": r.get("status"),
                    }
                    for r in rows
                ]
            }
        if action == "create_booking_link":
            et = str(inputs["event_type"])
            if not et.startswith(("https://api.calendly.com/event_types/", f"{STANDIN}/event_types/")):
                raise ConnectorError("Choose one of the business's Calendly event types.", "input")
            body = {"max_event_count": 1, "owner": et, "owner_type": "EventType"}
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"POST /scheduling_links": body}}
            known = self.known(conn, connection, key)
            if known:
                return {"booking_url": known["object_id"], "replayed": True}
            r = self.call(conn, connection, "POST", "/scheduling_links", json_body=body)
            url = (r.body.get("resource") or {}).get("booking_url", "")
            self.remember(conn, connection, key, "link", url)
            return {"booking_url": url}
        if action == "cancel_event":
            eid = str(inputs["event_id"])
            if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", eid):
                raise ConnectorError("That is not a Calendly event id.", "input")
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"POST": f"/scheduled_events/{eid}/cancellation"}}
            r = self.call(
                conn,
                connection,
                "POST",
                f"/scheduled_events/{eid}/cancellation",
                json_body={"reason": inputs.get("reason") or "Cancelled through Jibsy by ExaCarib."},
                allow=(403,),
            )
            if r.status == 403 and "already" not in json.dumps(r.body).lower():
                raise self.fail(r)
            return {"event_id": eid, "cancelled": True}
        raise ConnectorError(f"Unknown action {action}.", "input")

    health_path = "/users/me"

    def verify_webhook(self, conn, connection: dict, hook: dict, headers: dict, body: bytes, query: dict) -> bool:
        sig = header(headers, "Calendly-Webhook-Signature")
        parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
        t, v1 = parts.get("t", ""), parts.get("v1", "")
        if not fresh(t, 180):
            return False
        return same(hmac_hex(hook["secret"], f"{t}.".encode() + body), v1)

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        d = json.loads(body)
        p = d.get("payload") or {}
        return [
            {
                "type": f"calendly.{d.get('event', 'unknown')}",
                "id": f"{d.get('event')}:{p.get('uri', '')}",
                "data": {
                    "email": p.get("email"),
                    "name": p.get("name"),
                    "event": (p.get("scheduled_event") or {}).get("uri")
                    if isinstance(p.get("scheduled_event"), dict)
                    else p.get("event"),
                    "start": (p.get("scheduled_event") or {}).get("start_time")
                    if isinstance(p.get("scheduled_event"), dict)
                    else None,
                    "status": p.get("status"),
                },
            }
        ]

    def sample_inputs(self, action: str) -> dict:
        return {"find_events": {"email": "test@example.com"}}.get(action, {})


class CalendlyStandIn(Simulator):
    app = "calendly"

    def error_body(self, status: int, message: str) -> Any:
        title = {401: "Unauthenticated", 403: "Permission Denied", 404: "Resource Not Found", 429: "Too Many Requests"}
        return {"title": title.get(status, "Internal Server Error"), "message": message}

    @Simulator.route("GET", r"^/users/me$")
    def me(self, conn, connection, req: Request, m):
        return 200, {
            "resource": {
                "uri": STANDIN_USER,
                "name": "Stand-in",
                "current_organization": f"{STANDIN}/organizations/STANDIN",
            }
        }

    @Simulator.route("GET", r"^/event_types$")
    def event_types(self, conn, connection, req: Request, m):
        types = [
            {
                "uri": f"{STANDIN}/event_types/CONSULT30",
                "name": "30 minute consultation",
                "duration": 30,
                "active": True,
            },
            {"uri": f"{STANDIN}/event_types/VIEWING60", "name": "Viewing", "duration": 60, "active": True},
            {"uri": f"{STANDIN}/event_types/CALL15", "name": "Quick call", "duration": 15, "active": True},
        ]
        tok = int(req.query.get("page_token", "0"))
        page = types[tok : tok + 2]
        nxt = (
            f"{STANDIN}/event_types?user={req.query.get('user', '')}&page_token={tok + 2}"
            if tok + 2 < len(types)
            else None
        )
        return 200, {
            "collection": page,
            "pagination": {"count": len(page), "next_page": nxt, "next_page_token": str(tok + 2) if nxt else None},
        }

    @Simulator.route("GET", r"^/scheduled_events$")
    def scheduled(self, conn, connection, req: Request, m):
        email = req.query.get("invitee_email", "").lower()
        rows = [
            e
            for e in self.all(conn, connection, "event")
            if e.get("invitee", "").lower() == email and e["status"] == "active"
        ]
        return 200, {"collection": rows, "pagination": {"count": len(rows), "next_page": None}}

    @Simulator.route("POST", r"^/scheduling_links$")
    def link(self, conn, connection, req: Request, m):
        b = req.body or {}
        if b.get("owner_type") != "EventType" or "/event_types/" not in str(b.get("owner", "")):
            return 400, {
                "title": "Invalid Argument",
                "message": "owner must be an event type",
                "details": [{"parameter": "owner"}],
            }
        lid = uuid.uuid4().hex[:12]
        return 201, {
            "resource": {
                "booking_url": f"https://calendly.com/d/{lid}/standin",
                "owner": b["owner"],
                "owner_type": "EventType",
            }
        }

    @Simulator.route("POST", r"^/scheduled_events/([\w-]+)/cancellation$")
    def cancel(self, conn, connection, req: Request, m):
        ev = self.get(conn, connection, "event", m.group(1))
        if not ev:
            return 404, self.error_body(404, "The server could not find the requested resource.")
        if ev["status"] == "canceled":
            return 403, {"title": "Permission Denied", "message": "Event is already canceled"}
        self.put(conn, connection, "event", m.group(1), {**ev, "status": "canceled"})
        return 201, {"resource": {"canceled_by": "Stand-in", "reason": (req.body or {}).get("reason", "")}}


register(Calendly())
