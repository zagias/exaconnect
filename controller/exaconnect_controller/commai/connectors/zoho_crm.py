"""Zoho CRM connector (ADR 0034): contacts, leads and deals.

Written from the Zoho CRM API v6: ``GET /crm/v6/{Module}/search?email=``
(204 when nothing matches), ``POST /crm/v6/{Module}/upsert`` with
``duplicate_check_fields: ["Email"]`` for contacts and leads (Zoho updates
the existing record instead of making a duplicate), ``POST /crm/v6/Deals``
with a reference in the description found again through COQL on a retry,
and ``PUT /crm/v6/Contacts/{id}``. The auth header is
``Zoho-oauthtoken <token>``; the token answer names the account's data
centre API host (``api_domain``), which becomes the API base.

Zoho answers in ``{"data": [{"code", "details", "message", "status"}]}``;
INVALID_TOKEN and AUTHENTICATION_FAILURE mean the sign-in expired,
OAUTH_SCOPE_MISMATCH a missing permission, INVALID_DATA on an api_name a bad
mapping. Notification channels (Zoho's watch API) send a ``token`` we set,
checked on each delivery.

Not live until ExaCarib registers a Zoho server-based app
(EXA_ZOHO_CLIENT_ID, EXA_ZOHO_CLIENT_SECRET) and switches
``feature/integration-zoho_crm`` on; until then a connection runs on a stand-in.
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg

from . import ActionSpec, ConnectorError, Field
from .kit import KitConnector, Request, Setting, Simulator, ref, register, same

API = "/crm/v6"
STANDIN = "https://standin.zohoapis.com"

DEFAULT_MAPPING = {
    "contact": {"name": "Last_Name", "email": "Email", "phone": "Phone", "notes": "Description"},
    "lead": {"name": "Last_Name", "email": "Email", "company": "Company", "notes": "Description"},
    "deal": {"title": "Deal_Name", "amount": "Amount", "description": "Description"},
}


class ZohoCRM(KitConnector):
    app = "zoho_crm"
    label = "Zoho CRM"
    category = "crm"
    description = "Find, create and update contacts, leads and deals in Zoho CRM."
    settings_fields = (Setting("dc", "Zoho data centre", r"com|eu|in|com\.au|jp|ca|sa|com\.cn"),)
    needs_from_exacarib = "A Zoho API console server-based app (one per data centre the businesses use)."
    webhooks = "Zoho notification channels (watch) send changes; each delivery carries the token CommAI set."
    docs_url = "https://www.zoho.com/crm/developer/docs/api/v6/"
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
        "update_contact": ActionSpec(
            "update_contact",
            "Update a contact",
            "update",
            fields=(
                Field("contact_id", "Contact"),
                Field("phone", "Phone", "phone", False),
                Field("notes", "Notes", "text", False),
            ),
        ),
        "create_lead": ActionSpec(
            "create_lead",
            "Create a lead",
            "create",
            fields=(
                Field("name", "Name"),
                Field("email", "Email", "email"),
                Field("company", "Company", required=False),
                Field("notes", "Notes", "text", False),
            ),
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
    }
    mapping_targets = {
        "contact": ["First_Name", "Last_Name", "Email", "Phone", "Mobile", "Description", "Lead_Source"],
        "lead": ["First_Name", "Last_Name", "Email", "Phone", "Company", "Description", "Lead_Source"],
        "deal": ["Deal_Name", "Amount", "Description", "Stage", "Closing_Date"],
    }
    golive_criteria = {"sandbox": "Tested in a Zoho CRM sandbox, including the API credit limit."}

    def __init__(self):
        self.simulator = ZohoStandIn()

    def base_url(self, conn, connection: dict) -> str:
        if self.simulated(connection):
            return STANDIN
        url = (connection.get("settings") or {}).get("api_domain", "")
        if not re.fullmatch(r"https://www\.zohoapis\.[a-z.]{2,8}", url or ""):
            raise ConnectorError("Zoho did not name the account's API address. Sign in again.", "expired_signin")
        return url

    def auth_headers(self, conn, connection: dict) -> dict:
        from ..automation import oauth

        token = "standin" if self.simulated(connection) else oauth.access_token(conn, connection)
        return {"Authorization": f"Zoho-oauthtoken {token}"}

    def _code(self, body: Any) -> str:
        if isinstance(body, dict):
            if body.get("code"):
                return str(body["code"])
            data = body.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return str(data[0].get("code", ""))
        return ""

    def cause(self, status: int, body: Any) -> str:
        code = self._code(body)
        if code in ("INVALID_TOKEN", "AUTHENTICATION_FAILURE") or (status == 401 and code != "OAUTH_SCOPE_MISMATCH"):
            return "expired_signin"
        if code in ("OAUTH_SCOPE_MISMATCH", "NO_PERMISSION") or status == 403:
            return "permission"
        if code == "INVALID_DATA" and "api_name" in json.dumps(body):
            return "mapping"
        if code == "TOO_MANY_REQUESTS":
            return "provider"
        return super().cause(status, body)

    def message(self, status: int, body: Any) -> str:
        code = self._code(body)
        msg = ""
        if isinstance(body, dict):
            msg = str(body.get("message") or ((body.get("data") or [{}])[0] or {}).get("message", ""))
        return f"Zoho CRM answered {status}" + (f": {code} {msg[:160]}" if code or msg else ".")

    def _record(self, connection: dict, obj: str, inputs: dict) -> dict:
        m = {
            **DEFAULT_MAPPING[obj],
            **{k: v for k, v in ((connection.get("mapping") or {}).get(obj) or {}).items() if v},
        }
        out: dict[str, Any] = {}
        for k, v in inputs.items():
            if v in (None, "") or k not in m:
                continue
            if m[k] == "Last_Name":
                parts = str(v).split()
                out["Last_Name"] = parts[-1] if parts else str(v)
                if len(parts) > 1:
                    out["First_Name"] = " ".join(parts[:-1])
                continue
            out[m[k]] = float(v) if m[k] == "Amount" else v
        return out

    def _first_id(self, r) -> str:
        data = (r.body or {}).get("data") if isinstance(r.body, dict) else None
        item = data[0] if isinstance(data, list) and data else {}
        if item.get("status") != "success":
            raise ConnectorError(self.message(400, r.body), self.cause(400, {"data": [item]}))
        return str((item.get("details") or {}).get("id", ""))

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_contact":
            r = self.call(conn, connection, "GET", f"{API}/Contacts/search", params={"email": inputs["email"]})
            rows = (r.body or {}).get("data", []) if r.status == 200 and isinstance(r.body, dict) else []
            hit = rows[0] if rows else None
            return {
                "found": bool(hit),
                "contact": {k: hit.get(k) for k in ("id", "Full_Name", "Email", "Phone")} if hit else None,
            }
        if action == "update_contact":
            cid = str(inputs["contact_id"])
            if not cid.isdigit():
                raise ConnectorError("That is not a Zoho record id.", "input")
            rec = {k: v for k, v in {"Phone": inputs.get("phone"), "Description": inputs.get("notes")}.items() if v}
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"PUT": f"Contacts/{cid}", "data": rec}}
            r = self.call(conn, connection, "PUT", f"{API}/Contacts/{cid}", json_body={"data": [rec]})
            self._first_id(r)
            return {"contact_id": cid, "updated": True}
        known = self.known(conn, connection, key)
        if known:
            return {f"{known['object_type']}_id": known["object_id"], "replayed": True}
        if action in ("create_contact", "create_lead"):
            obj, module = ("contact", "Contacts") if action == "create_contact" else ("lead", "Leads")
            rec = self._record(connection, obj, inputs)
            if obj == "lead":
                rec.setdefault("Company", inputs.get("company") or "Not given")
                rec.setdefault("Lead_Source", "CommAI")
            body = {"data": [rec], "duplicate_check_fields": ["Email"]}
            if self.dry(connection):
                return {"dry_run": True, "would_send": {f"{module}/upsert": body}}
            r = self.call(conn, connection, "POST", f"{API}/{module}/upsert", json_body=body)
            oid = self._first_id(r)
            action_done = ((r.body.get("data") or [{}])[0] or {}).get("action", "insert")
            self.remember(conn, connection, key, obj, oid)
            return {f"{obj}_id": oid, "existing": action_done == "update"}
        if action == "create_deal":
            reference = ref(key)
            data = {**inputs, "description": f"{inputs.get('description') or ''}\n\nRef {reference}".strip()}
            rec = self._record(connection, "deal", data)
            rec.setdefault("Stage", "Qualification")
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"Deals": {"data": [rec]}}}
            q = self.call(
                conn,
                connection,
                "POST",
                f"{API}/coql",
                json_body={"select_query": f"select id from Deals where Description like '%{reference}%' limit 1"},
            )
            rows = (q.body or {}).get("data", []) if q.status == 200 and isinstance(q.body, dict) else []
            if rows:
                oid = str(rows[0]["id"])
            else:
                oid = self._first_id(self.call(conn, connection, "POST", f"{API}/Deals", json_body={"data": [rec]}))
            self.remember(conn, connection, key, "deal", oid)
            return {"deal_id": oid}
        raise ConnectorError(f"Unknown action {action}.", "input")

    health_path = f"{API}/org"

    def verify_webhook(self, conn, connection: dict, hook: dict, headers: dict, body: bytes, query: dict) -> bool:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return False
        return same(str(data.get("token", "")), hook["secret"][:50])

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        d = json.loads(body)
        return [
            {
                "type": "zoho_crm."
                + f"{str(d.get('module', 'record')).lower()}.{str(d.get('operation', 'change')).lower()}",
                "id": ":".join(
                    [str(d.get("channel_id", "")), ",".join(map(str, d.get("ids", []))), str(d.get("operation", ""))]
                ),
                "data": {"module": d.get("module"), "operation": d.get("operation"), "ids": d.get("ids", [])},
            }
        ]

    def sample_inputs(self, action: str) -> dict:
        return {"find_contact": {"email": "test@example.com"}}.get(action, {})


class ZohoStandIn(Simulator):
    app = "zoho_crm"

    def error_body(self, status: int, message: str) -> Any:
        code = {401: "INVALID_TOKEN", 403: "OAUTH_SCOPE_MISMATCH", 429: "TOO_MANY_REQUESTS"}.get(
            status, "INTERNAL_ERROR"
        )
        return {"code": code, "details": {}, "message": message, "status": "error"}

    def _ok(self, oid: str, action: str = "insert") -> dict:
        return {
            "data": [
                {
                    "code": "SUCCESS",
                    "duplicate_field": None,
                    "action": action,
                    "details": {"id": oid},
                    "message": "record added",
                    "status": "success",
                }
            ]
        }

    def _check(self, module: str, rec: dict) -> dict | None:
        known = set(sum(ZohoCRM.mapping_targets.values(), []))
        bad = [k for k in rec if k not in known]
        if bad:
            return {
                "data": [
                    {
                        "code": "INVALID_DATA",
                        "details": {"api_name": bad[0]},
                        "message": "invalid data",
                        "status": "error",
                    }
                ]
            }
        return None

    @Simulator.route("GET", r"/crm/v6/org$")
    def org(self, conn, connection, req: Request, m):
        return 200, {"org": [{"company_name": "Stand-in", "id": "1"}]}

    @Simulator.route("GET", r"/crm/v6/(\w+)/search$")
    def search(self, conn, connection, req: Request, m):
        email = req.query.get("email", "").lower()
        rows = [r for r in self.all(conn, connection, m.group(1)) if str(r.get("Email", "")).lower() == email]
        if not rows:
            return 204, ""
        return 200, {"data": rows, "info": {"per_page": 200, "count": len(rows), "page": 1, "more_records": False}}

    @Simulator.route("POST", r"/crm/v6/(\w+)/upsert$")
    def upsert(self, conn, connection, req: Request, m):
        module = m.group(1)
        rec = ((req.body or {}).get("data") or [{}])[0]
        bad = self._check(module, rec)
        if bad:
            return 400, bad
        hit = next(
            (
                r
                for r in self.all(conn, connection, module)
                if str(r.get("Email", "")).lower() == str(rec.get("Email", "")).lower()
            ),
            None,
        )
        if hit:
            self.put(conn, connection, module, hit["id"], {**hit, **rec})
            return 200, self._ok(hit["id"], "update")
        oid = "5725767000000" + self.new_id(conn, connection, module, digits=True)
        full = f"{rec.get('First_Name', '')} {rec.get('Last_Name', '')}".strip()
        self.put(conn, connection, module, oid, {"id": oid, "Full_Name": full, **rec})
        return 201, self._ok(oid)

    @Simulator.route("POST", r"/crm/v6/coql$")
    def coql(self, conn, connection, req: Request, m):
        q = str((req.body or {}).get("select_query", ""))
        mm = re.search(r"from (\w+) where Description like '%([^%']+)%'", q)
        if not mm:
            return 400, {"code": "SYNTAX_ERROR", "message": "bad query", "status": "error"}
        rows = [
            {"id": r["id"]}
            for r in self.all(conn, connection, mm.group(1))
            if mm.group(2) in str(r.get("Description", ""))
        ]
        if not rows:
            return 204, ""
        return 200, {"data": rows, "info": {"count": len(rows), "more_records": False}}

    @Simulator.route("POST", r"/crm/v6/(\w+)$")
    def create(self, conn, connection, req: Request, m):
        module = m.group(1)
        rec = ((req.body or {}).get("data") or [{}])[0]
        bad = self._check(module, rec)
        if bad:
            return 400, bad
        oid = "5725767000000" + self.new_id(conn, connection, module, digits=True)
        self.put(conn, connection, module, oid, {"id": oid, **rec})
        return 201, self._ok(oid)

    @Simulator.route("PUT", r"/crm/v6/(\w+)/(\d+)$")
    def update(self, conn, connection, req: Request, m):
        module, oid = m.groups()
        cur = self.get(conn, connection, module, oid)
        if not cur:
            return 400, {
                "data": [
                    {
                        "code": "INVALID_DATA",
                        "details": {"id": oid},
                        "message": "the related id given seems to be invalid",
                        "status": "error",
                    }
                ]
            }
        rec = ((req.body or {}).get("data") or [{}])[0]
        self.put(conn, connection, module, oid, {**cur, **rec})
        return 200, self._ok(oid, "update")


register(ZohoCRM())
