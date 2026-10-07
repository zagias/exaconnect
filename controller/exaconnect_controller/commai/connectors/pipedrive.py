"""Pipedrive connector (ADR 0034): people, leads and deals.

Written from the Pipedrive API v1: ``GET /persons/search?term=&fields=email
&exact_match=true`` (cursor paging through
``additional_data.pagination.next_start``), ``POST /persons`` with email and
phone lists, ``PUT /persons/{id}``, ``POST /leads`` (a lead hangs off a
person) and ``POST /deals``. Sign-in is OAuth 2.0 with the client id and
secret sent as HTTP Basic to the token endpoint; the token answer names the
company's ``api_domain``.

Answers are ``{"success": bool, "data": ..., "error": "..."}``. Rate limits
answer 429 with ``x-ratelimit-reset``. Webhooks (v1) are created with HTTP
Basic credentials of our choosing, checked on every delivery.

Retries never create twice: people are found by email first; deals and
leads carry a short reference in the title, searched before creating.

Not live until ExaCarib registers a Pipedrive Marketplace app
(EXA_PIPEDRIVE_CLIENT_ID, EXA_PIPEDRIVE_CLIENT_SECRET) and switches
``feature/integration-pipedrive`` on; until then a connection runs on a stand-in.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any

import psycopg

from . import ActionSpec, ConnectorError, Field
from .kit import KitConnector, Request, Simulator, header, ref, register, same

API = "/api/v1"
STANDIN = "https://standin.pipedrive.com"


class Pipedrive(KitConnector):
    app = "pipedrive"
    label = "Pipedrive"
    category = "crm"
    description = "Find and create people, leads and deals in Pipedrive."
    needs_from_exacarib = "A Pipedrive Marketplace app (OAuth), private or public."
    webhooks = "Pipedrive webhooks (person, deal and lead changes), checked by their HTTP Basic credentials."
    docs_url = "https://developers.pipedrive.com/docs/api/v1"
    actions = {
        "find_contact": ActionSpec(
            "find_contact", "Look up a person", "read", fields=(Field("email", "Email", "email"),)
        ),
        "create_contact": ActionSpec(
            "create_contact",
            "Create a person",
            "create",
            fields=(Field("name", "Name"), Field("email", "Email", "email"), Field("phone", "Phone", "phone", False)),
        ),
        "update_contact": ActionSpec(
            "update_contact",
            "Update a person",
            "update",
            fields=(
                Field("contact_id", "Person"),
                Field("phone", "Phone", "phone", False),
                Field("name", "Name", required=False),
            ),
        ),
        "create_lead": ActionSpec(
            "create_lead",
            "Create a lead",
            "create",
            fields=(Field("name", "Name"), Field("email", "Email", "email"), Field("notes", "Title", "text", False)),
        ),
        "create_deal": ActionSpec(
            "create_deal",
            "Create a deal",
            "create",
            fields=(
                Field("title", "Deal title"),
                Field("amount", "Value", "number", False),
                Field("currency", "Currency", required=False),
            ),
        ),
    }
    mapping_targets = {
        "contact": ["name", "email", "phone", "org_id", "owner_id"],
        "deal": ["title", "value", "currency", "stage_id", "person_id"],
    }
    golive_criteria = {"sandbox": "Tested in a Pipedrive developer sandbox company, including webhooks."}

    def __init__(self):
        self.simulator = PipedriveStandIn()

    def base_url(self, conn, connection: dict) -> str:
        if self.simulated(connection):
            return STANDIN
        url = (connection.get("settings") or {}).get("api_domain", "")
        if not re.fullmatch(r"https://[a-z0-9-]+\.pipedrive\.com", url or ""):
            raise ConnectorError("Pipedrive did not name the company's address. Sign in again.", "expired_signin")
        return url

    def message(self, status: int, body: Any) -> str:
        if isinstance(body, dict) and body.get("error"):
            return f"Pipedrive answered {status}: {str(body['error'])[:160]}"
        return super().message(status, body)

    def _data(self, r) -> Any:
        if not (isinstance(r.body, dict) and r.body.get("success")):
            raise ConnectorError(self.message(r.status, r.body), "provider")
        return r.body.get("data")

    def _person_by_email(self, conn, connection: dict, email: str) -> dict | None:
        items = self.paginate(
            conn,
            connection,
            f"{API}/persons/search",
            params={"term": email, "fields": "email", "exact_match": "true", "limit": 50},
            items=lambda b: (
                [i.get("item") for i in ((b or {}).get("data") or {}).get("items", [])] if isinstance(b, dict) else []
            ),
            next_page=lambda r: (
                {"start": p["next_start"]}
                if isinstance(r.body, dict)
                and (p := ((r.body.get("additional_data") or {}).get("pagination") or {})).get(
                    "more_items_in_collection"
                )
                else None
            ),
            max_items=1,
        )
        return next(iter(items), None)

    def _search_title(self, conn, connection: dict, kind: str, reference: str) -> str | None:
        r = self.call(
            conn, connection, "GET", f"{API}/{kind}/search", params={"term": reference, "fields": "title", "limit": 1}
        )
        items = ((r.body or {}).get("data") or {}).get("items", []) if isinstance(r.body, dict) else []
        return str(items[0]["item"]["id"]) if items else None

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_contact":
            hit = self._person_by_email(conn, connection, inputs["email"])
            return {"found": bool(hit), "contact": hit}
        if action == "update_contact":
            pid = str(inputs["contact_id"])
            if not pid.isdigit():
                raise ConnectorError("That is not a Pipedrive person id.", "input")
            body: dict = {}
            if inputs.get("name"):
                body["name"] = inputs["name"]
            if inputs.get("phone"):
                body["phone"] = [{"value": inputs["phone"], "primary": True, "label": "work"}]
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"PUT": f"persons/{pid}", "body": body}}
            self._data(self.call(conn, connection, "PUT", f"{API}/persons/{pid}", json_body=body))
            return {"contact_id": pid, "updated": True}
        known = self.known(conn, connection, key)
        if known:
            return {f"{known['object_type']}_id": known["object_id"], "replayed": True}
        if action in ("create_contact", "create_lead"):
            person = {
                "name": inputs["name"],
                "email": [{"value": inputs["email"], "primary": True, "label": "work"}],
            }
            if inputs.get("phone"):
                person["phone"] = [{"value": inputs["phone"], "primary": True, "label": "work"}]
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"persons": person}}
            hit = self._person_by_email(conn, connection, inputs["email"])
            pid = (
                str(hit["id"])
                if hit
                else str(self._data(self.call(conn, connection, "POST", f"{API}/persons", json_body=person))["id"])
            )
            if action == "create_contact":
                self.remember(conn, connection, key, "contact", pid)
                return {"contact_id": pid, "existing": bool(hit)}
            reference = ref(key)
            lid = self._search_title(conn, connection, "leads", reference)
            if not lid:
                title = f"{inputs.get('notes') or 'Enquiry from ' + inputs['name']} [{reference}]"
                lid = str(
                    self._data(
                        self.call(
                            conn, connection, "POST", f"{API}/leads", json_body={"title": title, "person_id": int(pid)}
                        )
                    )["id"]
                )
            self.remember(conn, connection, key, "lead", lid)
            return {"lead_id": lid, "person_id": pid}
        if action == "create_deal":
            reference = ref(key)
            deal: dict = {"title": f"{inputs['title']} [{reference}]"}
            if inputs.get("amount") not in (None, ""):
                deal["value"] = float(inputs["amount"])
                deal["currency"] = inputs.get("currency") or "USD"
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"deals": deal}}
            did = self._search_title(conn, connection, "deals", reference) or str(
                self._data(self.call(conn, connection, "POST", f"{API}/deals", json_body=deal))["id"]
            )
            self.remember(conn, connection, key, "deal", did)
            return {"deal_id": did}
        raise ConnectorError(f"Unknown action {action}.", "input")

    health_path = f"{API}/users/me"

    # Webhooks v1: HTTP Basic credentials chosen when the webhook is made.
    def verify_webhook(self, conn, connection: dict, hook: dict, headers: dict, body: bytes, query: dict) -> bool:
        auth = header(headers, "Authorization")
        if not auth.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except ValueError:
            return False
        return same(user, "commai") and same(pw, hook["secret"])

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        d = json.loads(body)
        meta = d.get("meta") or {}
        obj = meta.get("object") or meta.get("entity") or "object"
        act = meta.get("action") or "change"
        return [
            {
                "type": f"pipedrive.{obj}.{act}",
                "id": str(meta.get("id") or meta.get("request_id") or uuid.uuid4()),
                "data": {"object": obj, "action": act, "id": (d.get("current") or d.get("data") or {}).get("id")},
            }
        ]

    def sample_inputs(self, action: str) -> dict:
        return {"find_contact": {"email": "test@example.com"}}.get(action, {})


class PipedriveStandIn(Simulator):
    app = "pipedrive"

    def error_body(self, status: int, message: str) -> Any:
        return {"success": False, "error": message, "errorCode": status}

    @Simulator.route("GET", r"/api/v1/users/me$")
    def me(self, conn, connection, req: Request, m):
        return 200, {"success": True, "data": {"id": 1, "name": "Stand-in", "company_domain": "standin"}}

    @Simulator.route("GET", r"/api/v1/(persons|deals|leads)/search$")
    def search(self, conn, connection, req: Request, m):
        kind, q = m.group(1), req.query
        term = q.get("term", "").lower()
        start = int(q.get("start", "0"))
        if kind == "persons":
            rows = [
                r for r in self.all(conn, connection, kind) if term in [e["value"].lower() for e in r.get("email", [])]
            ]
        else:
            rows = [r for r in self.all(conn, connection, kind) if term in str(r.get("title", "")).lower()]
        page = rows[start : start + 1]
        more = start + 1 < len(rows)
        return 200, {
            "success": True,
            "data": {"items": [{"result_score": 1, "item": r} for r in page]},
            "additional_data": {
                "pagination": {
                    "start": start,
                    "limit": 1,
                    "more_items_in_collection": more,
                    **({"next_start": start + 1} if more else {}),
                }
            },
        }

    @Simulator.route("POST", r"/api/v1/(persons|deals|leads)$")
    def create(self, conn, connection, req: Request, m):
        kind = m.group(1)
        body = req.body if isinstance(req.body, dict) else {}
        if kind != "persons" and not body.get("title"):
            return 400, {"success": False, "error": "title is required", "errorCode": 400}
        if kind == "leads" and not body.get("person_id"):
            return 400, {"success": False, "error": "A lead needs a person or an organisation", "errorCode": 400}
        oid = str(uuid.uuid4()) if kind == "leads" else int(self.new_id(conn, connection, kind, digits=True))
        self.put(conn, connection, kind, str(oid), {"id": oid, **body})
        return 201, {"success": True, "data": {"id": oid, **body}}

    @Simulator.route("PUT", r"/api/v1/persons/(\d+)$")
    def update(self, conn, connection, req: Request, m):
        cur = self.get(conn, connection, "persons", m.group(1))
        if not cur:
            return 404, {"success": False, "error": "Person not found", "errorCode": 404}
        self.put(conn, connection, "persons", m.group(1), {**cur, **(req.body or {})})
        return 200, {"success": True, "data": {**cur, **(req.body or {})}}


register(Pipedrive())
