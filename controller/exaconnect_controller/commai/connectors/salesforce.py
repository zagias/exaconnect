"""Salesforce connector (ADR 0028): contacts, leads, opportunities and cases.

Written from Salesforce's REST API (v61.0): SOQL queries
(``/query?q=``, following ``nextRecordsUrl``), sObject create
(``POST /sobjects/{Type}``) and update (``PATCH /sobjects/{Type}/{id}``),
and SOSL search (``/search?q=FIND {...}``). Sign-in is the OAuth 2.0 web
server flow with a connected app; the token answer names the org's own
``instance_url``, which becomes the API host.

Retries never create twice: created records are kept by idempotency key;
an attempt that created a record but crashed before recording it is found
again by email (contacts, leads) or by a short reference written into the
description (opportunities, cases; SOSL searches long text).

Salesforce errors are a list of {"errorCode", "message"}: INVALID_SESSION_ID
is an expired sign-in, INSUFFICIENT_ACCESS a missing permission,
INVALID_FIELD a bad mapping, REQUEST_LIMIT_EXCEEDED the daily API limit.

Not live until ExaCarib registers a connected app (EXA_SALESFORCE_CLIENT_ID,
EXA_SALESFORCE_CLIENT_SECRET) and switches ``feature/integration-salesforce``
on. Until then a connection runs on a stand-in that answers like Salesforce.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any

import psycopg

from . import ActionSpec, ConnectorError, Field
from .kit import KitConnector, Request, Setting, Simulator, ref, register

API = "/services/data/v61.0"
STANDIN = "https://standin.my.salesforce.com"

DEFAULT_MAPPING = {
    "contact": {"name": "LastName", "email": "Email", "phone": "Phone", "company": "AccountId", "notes": "Description"},
    "lead": {"name": "LastName", "email": "Email", "phone": "Phone", "company": "Company", "notes": "Description"},
    "opportunity": {"title": "Name", "amount": "Amount", "description": "Description"},
    "case": {"subject": "Subject", "description": "Description", "priority": "Priority"},
}


def soql_quote(v: str) -> str:
    """A SOQL string literal: backslash and single quote escaped."""
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split()
    if len(parts) < 2:
        return "", name.strip() or "Unknown"
    return " ".join(parts[:-1]), parts[-1]


class Salesforce(KitConnector):
    app = "salesforce"
    label = "Salesforce"
    category = "crm"
    description = "Find and create contacts, leads, opportunities and cases in Salesforce."
    settings_fields = (
        Setting("login_host", "Login host", r"(login|test)\.salesforce\.com|[a-z0-9-]+\.my\.salesforce\.com"),
    )
    needs_from_exacarib = "A Salesforce connected app (OAuth web server flow, scopes api and refresh_token)."
    webhooks = "Salesforce changes reach CommAI through Flow or Apex callouts to a generic inbound webhook."
    docs_url = "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/"
    actions = {
        "find_contact": ActionSpec(
            "find_contact", "Look up a contact", "read", fields=(Field("email", "Email", "email"),)
        ),
        "search_contacts": ActionSpec(
            "search_contacts", "Search contacts by name", "read", fields=(Field("name", "Name"),)
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
            "Create an opportunity",
            "create",
            fields=(
                Field("title", "Opportunity name"),
                Field("amount", "Amount", "number", False),
                Field("description", "Description", "text", False),
            ),
        ),
        "create_ticket": ActionSpec(
            "create_ticket",
            "Create a case",
            "create",
            fields=(Field("subject", "Subject"), Field("description", "Description", "text", False)),
        ),
    }
    mapping_targets = {
        "contact": ["FirstName", "LastName", "Email", "Phone", "MobilePhone", "Description", "Title"],
        "lead": ["FirstName", "LastName", "Email", "Phone", "Company", "Description", "LeadSource"],
        "opportunity": ["Name", "Amount", "Description", "CloseDate", "StageName"],
        "case": ["Subject", "Description", "Priority", "Origin", "Status"],
    }
    golive_criteria = {
        "sandbox": "Tested in a Salesforce sandbox org: create, update, duplicate rules and the daily API limit.",
    }

    def __init__(self):
        self.simulator = SalesforceStandIn()

    def base_url(self, conn, connection: dict) -> str:
        if self.simulated(connection):
            return STANDIN
        url = (connection.get("settings") or {}).get("instance_url", "")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+\.(salesforce|force)\.com", url or ""):
            raise ConnectorError("Salesforce did not name the org's address. Sign in again.", "expired_signin")
        return url

    def cause(self, status: int, body: Any) -> str:
        code = body[0].get("errorCode", "") if isinstance(body, list) and body and isinstance(body[0], dict) else ""
        if status == 401 or code == "INVALID_SESSION_ID":
            return "expired_signin"
        if code == "REQUEST_LIMIT_EXCEEDED":
            return "provider"
        if status == 403 or code in ("INSUFFICIENT_ACCESS", "INSUFFICIENT_ACCESS_OR_READONLY", "API_DISABLED_FOR_ORG"):
            return "permission"
        if code in ("INVALID_FIELD", "INVALID_FIELD_FOR_INSERT_UPDATE", "INVALID_TYPE"):
            return "mapping"
        if status in (400, 404, 409):
            return "input"
        return "provider"

    def message(self, status: int, body: Any) -> str:
        if isinstance(body, list) and body and isinstance(body[0], dict):
            return (
                f"Salesforce answered {status}: {body[0].get('errorCode', '')} {str(body[0].get('message', ''))[:160]}"
            )
        return super().message(status, body)

    def _fields(self, connection: dict, obj: str, inputs: dict) -> dict:
        m = {
            **DEFAULT_MAPPING[obj],
            **{k: v for k, v in ((connection.get("mapping") or {}).get(obj) or {}).items() if v},
        }
        out = {}
        for k, v in inputs.items():
            if v in (None, "") or k not in m:
                continue
            if k == "name" and m[k] == "LastName":
                first, last = split_name(str(v))
                out["LastName"] = last
                if first:
                    out["FirstName"] = first
                continue
            if obj == "contact" and k == "company":
                continue  # AccountId needs an account record; companies go on leads
            out[m[k]] = v
        return out

    def query(self, conn, connection: dict, soql: str, limit: int = 20) -> list[dict]:
        return list(
            self.paginate(
                conn,
                connection,
                f"{API}/query",
                params={"q": soql},
                items=lambda b: b.get("records", []) if isinstance(b, dict) else [],
                next_page=lambda r: r.body.get("nextRecordsUrl") if isinstance(r.body, dict) else None,
                max_items=limit,
            )
        )

    def _by_email(self, conn, connection: dict, obj: str, email: str) -> dict | None:
        rows = self.query(
            conn,
            connection,
            f"SELECT Id, FirstName, LastName, Email, Phone FROM {obj} WHERE Email = {soql_quote(email)} LIMIT 1",
            1,
        )
        return rows[0] if rows else None

    def _by_ref(self, conn, connection: dict, obj: str, reference: str) -> str | None:
        r = self.call(
            conn,
            connection,
            "GET",
            f"{API}/search",
            params={"q": f"FIND {{{reference}}} IN ALL FIELDS RETURNING {obj}(Id)"},
        )
        hits = (r.body or {}).get("searchRecords", []) if isinstance(r.body, dict) else []
        return hits[0]["Id"] if hits else None

    def _create(self, conn, connection: dict, obj: str, fields: dict) -> str:
        r = self.call(conn, connection, "POST", f"{API}/sobjects/{obj}", json_body=fields, ok=(200, 201))
        if not (isinstance(r.body, dict) and r.body.get("success") and r.body.get("id")):
            raise ConnectorError("Salesforce did not confirm the record.", "provider")
        return r.body["id"]

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_contact":
            hit = self._by_email(conn, connection, "Contact", inputs["email"])
            return {
                "found": bool(hit),
                "contact": ({k: v for k, v in hit.items() if k != "attributes"} if hit else None),
            }
        if action == "search_contacts":
            like = soql_quote("%" + inputs["name"].replace("%", "").replace("_", "") + "%")
            rows = self.query(
                conn, connection, f"SELECT Id, Name, Email, Phone FROM Contact WHERE Name LIKE {like} ORDER BY Name", 20
            )
            return {"contacts": [{k: v for k, v in r.items() if k != "attributes"} for r in rows]}
        if action == "update_contact":
            cid = str(inputs["contact_id"])
            if not re.fullmatch(r"[A-Za-z0-9]{15,18}", cid):
                raise ConnectorError("That is not a Salesforce record id.", "input")
            fields = {k: v for k, v in {"Phone": inputs.get("phone"), "Description": inputs.get("notes")}.items() if v}
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"PATCH": f"Contact/{cid}", "fields": fields}}
            self.call(conn, connection, "PATCH", f"{API}/sobjects/Contact/{cid}", json_body=fields, ok=(204, 200))
            return {"contact_id": cid, "updated": True}
        known = self.known(conn, connection, key)
        if known:
            return {f"{known['object_type']}_id": known["object_id"], "replayed": True}
        if action in ("create_contact", "create_lead"):
            obj, name = ("Contact", "contact") if action == "create_contact" else ("Lead", "lead")
            fields = self._fields(connection, name, inputs)
            if obj == "Lead":
                fields.setdefault("Company", inputs.get("company") or "Not given")
                fields.setdefault("LeadSource", "CommAI")
            if self.dry(connection):
                return {"dry_run": True, "would_send": {obj: fields}}
            hit = self._by_email(conn, connection, obj, inputs["email"])
            oid = hit["Id"] if hit else self._create(conn, connection, obj, fields)
            self.remember(conn, connection, key, name, oid)
            return {f"{name}_id": oid, "existing": bool(hit)}
        if action in ("create_deal", "create_ticket"):
            obj, name, mobj = (
                ("Opportunity", "deal", "opportunity") if action == "create_deal" else ("Case", "ticket", "case")
            )
            reference = ref(key)
            data = dict(inputs)
            data["description"] = f"{inputs.get('description') or ''}\n\nRef {reference}".strip()
            fields = self._fields(connection, mobj, data)
            if obj == "Opportunity":
                fields.setdefault("StageName", "Prospecting")
                fields.setdefault("CloseDate", dt.date.today().isoformat())
            else:
                fields.setdefault("Origin", "Web")
            if self.dry(connection):
                return {"dry_run": True, "would_send": {obj: fields}}
            oid = self._by_ref(conn, connection, obj, reference) or self._create(conn, connection, obj, fields)
            self.remember(conn, connection, key, name, oid)
            return {f"{name}_id": oid}
        raise ConnectorError(f"Unknown action {action}.", "input")

    health_path = f"{API}/limits"

    def sample_inputs(self, action: str) -> dict:
        return {"find_contact": {"email": "test@example.com"}, "search_contacts": {"name": "Test"}}.get(action, {})


class SalesforceStandIn(Simulator):
    app = "salesforce"

    def error_body(self, status: int, message: str) -> Any:
        code = {401: "INVALID_SESSION_ID", 403: "INSUFFICIENT_ACCESS", 404: "NOT_FOUND", 429: "REQUEST_LIMIT_EXCEEDED"}
        return [{"errorCode": code.get(status, "UNKNOWN_EXCEPTION"), "message": message}]

    def _id(self, conn, connection, obj: str) -> str:
        prefix = {"Contact": "003", "Lead": "00Q", "Opportunity": "006", "Case": "500"}.get(obj, "001")
        n = self.new_id(conn, connection, obj, digits=True)
        return f"{prefix}5g00000{int(n):08d}"[:18]

    @Simulator.route("GET", r"/services/data/v61\.0/limits$")
    def limits(self, conn, connection, req: Request, m):
        return 200, {"DailyApiRequests": {"Max": 15000, "Remaining": 14999}}

    @Simulator.route("GET", r"/services/data/v61\.0/query$")
    def query(self, conn, connection, req: Request, m):
        q = req.query.get("q", "")
        mm = re.search(r"FROM (\w+)(?: WHERE (\w+) (=|LIKE) '((?:[^'\\]|\\.)*)')?", q)
        if not mm:
            return 400, [{"errorCode": "MALFORMED_QUERY", "message": "unexpected token"}]
        obj, fld, op, val = mm.groups()
        val = (val or "").replace("\\'", "'").replace("\\\\", "\\")
        rows = []
        for r in self.all(conn, connection, obj):
            if fld == "Name":
                r = {**r, "Name": f"{r.get('FirstName', '')} {r.get('LastName', '')}".strip()}
            v = str(r.get(fld or "", ""))
            if fld and op == "=" and v.lower() != val.lower():
                continue
            if fld and op == "LIKE" and val.strip("%").lower() not in v.lower():
                continue
            rows.append({"attributes": {"type": obj}, **r})
        # Two records a page, to exercise nextRecordsUrl.
        start = int(req.query.get("offset", "0"))
        page = rows[start : start + 2]
        body: dict = {"totalSize": len(rows), "done": start + 2 >= len(rows), "records": page}
        if start + 2 < len(rows):
            body["nextRecordsUrl"] = f"/services/data/v61.0/query?q={urllib.parse.quote(q)}&offset={start + 2}"
        return 200, body

    @Simulator.route("GET", r"/services/data/v61\.0/search$")
    def search(self, conn, connection, req: Request, m):
        mm = re.search(r"FIND \{([^}]*)\} IN ALL FIELDS RETURNING (\w+)", req.query.get("q", ""))
        if not mm:
            return 400, [{"errorCode": "MALFORMED_SEARCH", "message": "bad SOSL"}]
        term, obj = mm.groups()
        hits = [
            {"attributes": {"type": obj}, "Id": r["Id"]}
            for r in self.all(conn, connection, obj)
            if any(term in str(v) for v in r.values())
        ]
        return 200, {"searchRecords": hits}

    @Simulator.route("POST", r"/services/data/v61\.0/sobjects/(\w+)$")
    def create(self, conn, connection, req: Request, m):
        obj = m.group(1)
        body = req.body if isinstance(req.body, dict) else {}
        allowed = set(Salesforce.mapping_targets.get(obj.lower(), [])) | set(
            Salesforce.mapping_targets.get({"Opportunity": "opportunity", "Case": "case"}.get(obj, ""), [])
        )
        allowed |= {
            "FirstName",
            "LastName",
            "Email",
            "Phone",
            "Description",
            "Company",
            "LeadSource",
            "Name",
            "Amount",
            "StageName",
            "CloseDate",
            "Subject",
            "Priority",
            "Origin",
            "Status",
        }
        bad = [k for k in body if k not in allowed]
        if bad:
            return 400, [
                {"errorCode": "INVALID_FIELD", "message": f"No such column '{bad[0]}' on sobject of type {obj}"}
            ]
        if obj in ("Contact", "Lead") and not body.get("LastName"):
            return 400, [{"errorCode": "REQUIRED_FIELD_MISSING", "message": "Required fields are missing: [LastName]"}]
        oid = self._id(conn, connection, obj)
        self.put(conn, connection, obj, oid, {"Id": oid, **body})
        return 201, {"id": oid, "success": True, "errors": []}

    @Simulator.route("PATCH", r"/services/data/v61\.0/sobjects/(\w+)/(\w+)$")
    def update(self, conn, connection, req: Request, m):
        obj, oid = m.groups()
        cur = self.get(conn, connection, obj, oid)
        if cur is None:
            return 404, [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
        self.put(conn, connection, obj, oid, {**cur, **(req.body or {})})
        return 204, ""


register(Salesforce())
