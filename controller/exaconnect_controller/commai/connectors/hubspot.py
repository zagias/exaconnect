"""HubSpot connector (ADR 0020): contacts, leads, deals and tickets.

Written from HubSpot's public CRM v3 API (objects and search). NOT LIVE
until a HubSpot app is registered (EXA_HUBSPOT_CLIENT_ID/SECRET and
EXA_PUBLIC_URL) or a business enters a private-app token through secure
entry. Tested only against a fake HTTP layer.

Retries never create twice:
1. Every object created is recorded in commai_connector_objects under the
   action's idempotency key; a retry finds it there and returns it.
2. If an attempt created the object but crashed before recording it, the
   retry searches HubSpot first: contacts by email (HubSpot keeps emails
   unique), deals and tickets by a short reference written into their
   description.

Field mapping: connection.mapping {"contact": {"name": "firstname", ...},
"deal": {...}, "ticket": {...}} maps CommAI fields to HubSpot properties.
A property HubSpot doesn't know is reported as a mapping problem.

In test mode, reads run for real and creates are checked and shown but not
sent, unless settings.test_writes is true (a HubSpot sandbox account).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import psycopg

from . import ActionSpec, Connector, ConnectorError, Field, register

API = "https://api.hubapi.com"

DEFAULT_MAPPING = {
    "contact": {"name": "firstname", "email": "email", "phone": "phone", "notes": "message"},
    "deal": {"title": "dealname", "amount": "amount", "description": "description"},
    "ticket": {"subject": "subject", "description": "content", "priority": "hs_ticket_priority"},
}


def _ref(key: str) -> str:
    return "cmai" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _cause(status: int, body: Any) -> str:
    cat = body.get("category", "") if isinstance(body, dict) else ""
    msg = str(body.get("message", "")) if isinstance(body, dict) else ""
    if status == 401:
        return "expired_signin"
    if status == 403 or cat == "MISSING_SCOPES":
        return "permission"
    if status == 400 and ("does not exist" in msg or cat == "VALIDATION_ERROR" and "Property" in msg):
        return "mapping"
    if status in (400, 404, 409):
        return "input"
    return "provider"


def _message(status: int, body: Any) -> str:
    msg = str(body.get("message", ""))[:200] if isinstance(body, dict) else ""
    return f"HubSpot answered {status}" + (f": {msg}" if msg else ".")


class HubSpot(Connector):
    app = "hubspot"
    label = "HubSpot"
    description = "Find and create contacts, leads, deals and tickets in HubSpot CRM."
    auth = "oauth"  # or a private-app token
    actions = {
        "find_contact": ActionSpec(
            "find_contact", "Look up a contact", "read", fields=(Field("email", "Email", "email"),)
        ),
        "create_contact": ActionSpec(
            "create_contact",
            "Create a contact",
            "create",
            fields=(Field("name", "Name"), Field("email", "Email", "email"), Field("phone", "Phone", "phone", False)),
        ),
        "create_lead": ActionSpec(
            "create_lead",
            "Create a lead",
            "create",
            fields=(Field("name", "Name"), Field("email", "Email", "email"), Field("notes", "Notes", "text", False)),
        ),
        "create_deal": ActionSpec(
            "create_deal",
            "Create a deal",
            "create",
            fields=(
                Field("title", "Deal name"),
                Field("amount", "Amount", "number", False),
                Field("description", "Description", "text", False),
            ),
        ),
        "create_ticket": ActionSpec(
            "create_ticket",
            "Create a ticket",
            "create",
            fields=(Field("subject", "Subject"), Field("description", "Description", "text", False)),
        ),
    }
    mapping_targets = {
        "contact": ["firstname", "lastname", "email", "phone", "company", "message", "lifecyclestage"],
        "deal": ["dealname", "amount", "description", "closedate", "pipeline", "dealstage"],
        "ticket": ["subject", "content", "hs_ticket_priority", "hs_pipeline", "hs_pipeline_stage"],
        "company": ["name", "domain", "phone", "industry", "city", "country", "numberofemployees"],
    }

    def _call(self, conn, connection: dict, method: str, path: str, **kw):
        from ..automation import http, oauth

        token = oauth.access_token(conn, connection)
        try:
            return http.request(method, API + path, token=token, **kw)
        except http.NetworkError as e:
            raise ConnectorError(str(e), "provider") from None

    def _mapping(self, connection: dict, obj: str) -> dict:
        m = (connection.get("mapping") or {}).get(obj) or {}
        return {**DEFAULT_MAPPING[obj], **{k: v for k, v in m.items() if v}}

    def _props(self, connection: dict, obj: str, inputs: dict) -> dict:
        m = self._mapping(connection, obj)
        return {m[k]: str(v) for k, v in inputs.items() if k in m and v not in (None, "")}

    def _remember(self, conn, connection: dict, key: str, obj: str, oid: str) -> None:
        conn.execute(
            """INSERT INTO commai_connector_objects (customer_id, app, idempotency_key, object_type, object_id)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (connection["customer_id"], self.app, key, obj, oid),
        )

    def _known(self, conn, connection: dict, key: str) -> dict | None:
        return conn.execute(
            "SELECT object_type, object_id FROM commai_connector_objects WHERE customer_id = %s AND app = %s"
            " AND idempotency_key = %s",
            (connection["customer_id"], self.app, key),
        ).fetchone()

    def _search(self, conn, connection: dict, kind: str, prop: str, op: str, value: str) -> dict | None:
        r = self._call(
            conn,
            connection,
            "POST",
            f"/crm/v3/objects/{kind}/search",
            json_body={
                "filterGroups": [{"filters": [{"propertyName": prop, "operator": op, "value": value}]}],
                "limit": 1,
            },
        )
        if r.status != 200:
            raise ConnectorError(_message(r.status, r.body), _cause(r.status, r.body))
        results = r.body.get("results") or []
        return results[0] if results else None

    def _create(self, conn, connection: dict, kind: str, props: dict) -> dict:
        r = self._call(conn, connection, "POST", f"/crm/v3/objects/{kind}", json_body={"properties": props})
        if r.status == 409 and kind == "contacts":
            m = re.search(r"Existing ID:\s*(\d+)", str(r.body.get("message", "")))
            if m:
                return {"id": m.group(1), "properties": props, "existing": True}
        if r.status not in (200, 201):
            raise ConnectorError(_message(r.status, r.body), _cause(r.status, r.body))
        return r.body

    def _dry(self, connection: dict) -> bool:
        return bool(connection.get("test")) and not (connection.get("settings") or {}).get("test_writes")

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_contact":
            hit = self._search(conn, connection, "contacts", "email", "EQ", inputs["email"])
            return {
                "found": bool(hit),
                "contact": ({"id": hit["id"], **(hit.get("properties") or {})} if hit else None),
            }
        known = self._known(conn, connection, key)
        if known:
            return {f"{known['object_type']}_id": known["object_id"], "replayed": True}
        if action in ("create_contact", "create_lead"):
            props = self._props(connection, "contact", inputs)
            if action == "create_lead":
                props.setdefault("lifecyclestage", "lead")
            if self._dry(connection):
                return {"dry_run": True, "would_send": {"contacts": props}}
            hit = self._search(conn, connection, "contacts", "email", "EQ", inputs["email"])
            obj = hit or self._create(conn, connection, "contacts", props)
            name = "lead" if action == "create_lead" else "contact"
            self._remember(conn, connection, key, name, str(obj["id"]))
            return {f"{name}_id": str(obj["id"]), "existing": bool(hit or obj.get("existing"))}
        if action in ("create_deal", "create_ticket"):
            obj_name, kind, desc_field = (
                ("deal", "deals", "description") if action == "create_deal" else ("ticket", "tickets", "description")
            )
            ref = _ref(key)
            fields = dict(inputs)
            fields[desc_field] = f"{inputs.get(desc_field) or ''}\n\nRef {ref}".strip()
            props = self._props(connection, obj_name, fields)
            if kind == "tickets":
                props.setdefault("hs_pipeline", "0")
                props.setdefault("hs_pipeline_stage", "1")
            if self._dry(connection):
                return {"dry_run": True, "would_send": {kind: props}}
            target = self._mapping(connection, obj_name)[desc_field]
            hit = self._search(conn, connection, kind, target, "CONTAINS_TOKEN", ref)
            obj = hit or self._create(conn, connection, kind, props)
            self._remember(conn, connection, key, obj_name, str(obj["id"]))
            return {f"{obj_name}_id": str(obj["id"]), "existing": bool(hit)}
        raise ConnectorError(f"Unknown action {action}.", "input")

    def health(self, conn, connection: dict) -> dict:
        try:
            r = self._call(conn, connection, "GET", "/crm/v3/objects/contacts", params={"limit": 1})
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        if r.status == 200:
            return {"ok": True, "cause": "", "detail": "HubSpot answering."}
        return {"ok": False, "cause": _cause(r.status, r.body), "detail": _message(r.status, r.body)}

    def sample_inputs(self, action: str) -> dict:
        return {"find_contact": {"email": "test@example.com"}}.get(action, {})


register(HubSpot())
