"""Microsoft Dynamics 365 (Dataverse Web API v9.2) connector (ADR 0028):
contacts, leads, opportunities and cases (incidents).

Written from the Dataverse Web API (OData v4): ``GET /contacts?$filter=``
with ``@odata.nextLink`` paging, and creates as an *upsert to a key we
choose*: ``PATCH /contacts(<id>)`` with ``If-None-Match: *``, where the id
is a UUID derived from the action's idempotency key. Dataverse creates the
record, or answers 412 when it exists, so a retry never creates twice, even
after a crash. Updates use ``If-Match: *`` so they never create.

Sign-in is Microsoft identity platform OAuth with the scope
``https://{org_host}/user_impersonation``. Errors carry
``{"error": {"code", "message"}}``; service protection limits answer 429
with Retry-After.

Not live until ExaCarib registers an Entra ID app (EXA_DYNAMICS_CLIENT_ID,
EXA_DYNAMICS_CLIENT_SECRET) and switches ``feature/integration-dynamics365``
on; until then a connection runs on a stand-in.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg

from . import ActionSpec, ConnectorError, Field
from .kit import KitConnector, Request, Setting, Simulator, register

API = "/api/data/v9.2"
STANDIN = "https://standin.crm.dynamics.com"
NS = uuid.UUID("7f1b6c2e-2a51-4b8e-9d65-0b5a3c1e9a10")

ENTITY = {"contact": "contacts", "lead": "leads", "deal": "opportunities", "ticket": "incidents"}
KEY = {"contacts": "contactid", "leads": "leadid", "opportunities": "opportunityid", "incidents": "incidentid"}

DEFAULT_MAPPING = {
    "contact": {"name": "fullname", "email": "emailaddress1", "phone": "telephone1", "notes": "description"},
    "lead": {"name": "fullname", "email": "emailaddress1", "company": "companyname", "notes": "description"},
    "deal": {"title": "name", "amount": "estimatedvalue", "description": "description"},
    "ticket": {"subject": "title", "description": "description"},
}


def record_id(key: str) -> str:
    return str(uuid.uuid5(NS, key))


def odata_quote(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


class Dynamics365(KitConnector):
    app = "dynamics365"
    label = "Microsoft Dynamics 365"
    category = "crm"
    description = "Find and create contacts, leads, opportunities and cases in Dynamics 365 (Dataverse)."
    settings_fields = (
        Setting("org_host", "Organisation address", r"[a-z0-9-]+\.crm[0-9]*\.dynamics\.com", required=True),
        Setting("tenant", "Microsoft tenant", r"[A-Za-z0-9.-]{1,100}"),
    )
    needs_from_exacarib = "An Entra ID app registration with the Dynamics CRM user_impersonation permission."
    webhooks = "Dataverse webhooks or Power Automate can call a generic inbound webhook."
    docs_url = "https://learn.microsoft.com/power-apps/developer/data-platform/webapi/overview"
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
            "Create an opportunity",
            "create",
            fields=(Field("title", "Name"), Field("amount", "Amount", "number", False)),
        ),
        "create_ticket": ActionSpec(
            "create_ticket",
            "Create a case",
            "create",
            fields=(
                Field("subject", "Title"),
                Field("contact_id", "Customer (contact)"),
                Field("description", "Description", "text", False),
            ),
        ),
    }
    mapping_targets = {
        "contact": ["firstname", "lastname", "fullname", "emailaddress1", "telephone1", "mobilephone", "description"],
        "lead": ["firstname", "lastname", "fullname", "emailaddress1", "companyname", "subject", "description"],
        "deal": ["name", "estimatedvalue", "description", "estimatedclosedate"],
        "ticket": ["title", "description", "prioritycode"],
    }
    golive_criteria = {"sandbox": "Tested in a Dynamics 365 sandbox environment, including service protection limits."}

    def __init__(self):
        self.simulator = DynamicsStandIn()

    def base_url(self, conn, connection: dict) -> str:
        if self.simulated(connection):
            return STANDIN
        host = (connection.get("settings") or {}).get("org_host", "")
        if not re.fullmatch(r"[a-z0-9-]+\.crm[0-9]*\.dynamics\.com", host or ""):
            raise ConnectorError("Set the Dynamics 365 organisation address first.", "mapping")
        return f"https://{host}"

    def cause(self, status: int, body: Any) -> str:
        msg = str(((body or {}).get("error") or {}).get("message", "")) if isinstance(body, dict) else ""
        if status == 400 and ("Could not find a property" in msg or "property named" in msg):
            return "mapping"
        return super().cause(status, body)

    def _fields(self, connection: dict, obj: str, inputs: dict) -> dict:
        m = {
            **DEFAULT_MAPPING[obj],
            **{k: v for k, v in ((connection.get("mapping") or {}).get(obj) or {}).items() if v},
        }
        out: dict[str, Any] = {}
        for k, v in inputs.items():
            if v in (None, "") or k not in m:
                continue
            if m[k] == "fullname":  # computed in Dataverse: write first and last name
                parts = str(v).split()
                out["lastname"] = parts[-1] if parts else str(v)
                if len(parts) > 1:
                    out["firstname"] = " ".join(parts[:-1])
                continue
            out[m[k]] = float(v) if m[k] == "estimatedvalue" else v
        return out

    def _upsert_new(self, conn, connection: dict, entity: str, rid: str, fields: dict) -> bool:
        """Create with our own id; False when it already exists (412)."""
        r = self.call(
            conn,
            connection,
            "PATCH",
            f"{API}/{entity}({rid})",
            json_body=fields,
            headers={"If-None-Match": "*", "OData-MaxVersion": "4.0", "OData-Version": "4.0"},
            ok=(200, 201, 204),
            allow=(412,),
        )
        return r.status != 412

    def _find(self, conn, connection: dict, entity: str, email: str) -> dict | None:
        rows = list(
            self.paginate(
                conn,
                connection,
                f"{API}/{entity}",
                params={
                    "$select": f"{KEY[entity]},fullname,emailaddress1,telephone1",
                    "$filter": f"emailaddress1 eq {odata_quote(email)}",
                },
                items=lambda b: b.get("value", []) if isinstance(b, dict) else [],
                next_page=lambda r: r.body.get("@odata.nextLink") if isinstance(r.body, dict) else None,
                max_items=1,
            )
        )
        return rows[0] if rows else None

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_contact":
            hit = self._find(conn, connection, "contacts", inputs["email"])
            return {"found": bool(hit), "contact": hit}
        if action == "update_contact":
            cid = str(inputs["contact_id"])
            try:
                uuid.UUID(cid)
            except ValueError:
                raise ConnectorError("That is not a Dynamics 365 record id.", "input") from None
            fields = {
                k: v for k, v in {"telephone1": inputs.get("phone"), "description": inputs.get("notes")}.items() if v
            }
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"PATCH": f"contacts({cid})", "fields": fields}}
            r = self.call(
                conn,
                connection,
                "PATCH",
                f"{API}/contacts({cid})",
                json_body=fields,
                headers={"If-Match": "*"},
                allow=(404, 412),
            )
            if r.status in (404, 412):
                raise ConnectorError("No such contact.", "input")
            return {"contact_id": cid, "updated": True}
        obj = {
            "create_contact": "contact",
            "create_lead": "lead",
            "create_deal": "deal",
            "create_ticket": "ticket",
        }.get(action)
        if not obj:
            raise ConnectorError(f"Unknown action {action}.", "input")
        entity = ENTITY[obj]
        fields = self._fields(connection, obj, inputs)
        if obj == "ticket":
            cid = str(inputs["contact_id"])
            try:
                uuid.UUID(cid)
            except ValueError:
                raise ConnectorError("That is not a Dynamics 365 contact id.", "input") from None
            fields["customerid_contact@odata.bind"] = f"/contacts({cid})"
        if obj == "lead":
            fields.setdefault("subject", f"Enquiry from {inputs['name']}")
        rid = record_id(key)
        if self.dry(connection):
            return {
                "dry_run": True,
                "would_send": {"PATCH": f"{entity}({rid})", "If-None-Match": "*", "fields": fields},
            }
        known = self.known(conn, connection, key)
        if known:
            return {f"{obj}_id": known["object_id"], "replayed": True}
        if obj in ("contact", "lead"):
            hit = self._find(conn, connection, entity, inputs["email"])
            if hit:
                self.remember(conn, connection, key, obj, hit[KEY[entity]])
                if hit[KEY[entity]] == rid:
                    return {f"{obj}_id": rid, "replayed": True}
                return {f"{obj}_id": hit[KEY[entity]], "existing": True}
        created = self._upsert_new(conn, connection, entity, rid, fields)
        self.remember(conn, connection, key, obj, rid)
        return {f"{obj}_id": rid, **({} if created else {"replayed": True})}

    health_path = f"{API}/WhoAmI"

    def sample_inputs(self, action: str) -> dict:
        return {"find_contact": {"email": "test@example.com"}}.get(action, {})


class DynamicsStandIn(Simulator):
    app = "dynamics365"

    def error_body(self, status: int, message: str) -> Any:
        return {"error": {"code": f"0x8004{status:04d}", "message": message}}

    @Simulator.route("GET", r"/api/data/v9\.2/WhoAmI$")
    def whoami(self, conn, connection, req: Request, m):
        return 200, {"UserId": "00000000-0000-0000-0000-000000000001", "OrganizationId": "standin"}

    @Simulator.route("GET", r"/api/data/v9\.2/(\w+)$")
    def list_(self, conn, connection, req: Request, m):
        entity = m.group(1)
        q = req.query
        mm = re.search(r"emailaddress1 eq '((?:[^']|'')*)'", q.get("$filter", ""))
        rows = self.all(conn, connection, entity)
        if mm:
            want = mm.group(1).replace("''", "'").lower()
            rows = [r for r in rows if str(r.get("emailaddress1", "")).lower() == want]
        skip = int(q.get("$skiptoken", "0"))
        page = rows[skip : skip + 2]
        body: dict = {"@odata.context": f"{STANDIN}{API}/$metadata#{entity}", "value": page}
        if skip + 2 < len(rows):
            body["@odata.nextLink"] = f"{STANDIN}{API}/{entity}?$filter={q.get('$filter', '')}&$skiptoken={skip + 2}"
        return 200, body

    @Simulator.route("PATCH", r"/api/data/v9\.2/(\w+)\(([0-9a-f-]{36})\)$")
    def upsert(self, conn, connection, req: Request, m):
        entity, rid = m.groups()
        cur = self.get(conn, connection, entity, rid)
        h = {k.lower(): v for k, v in req.headers.items()}
        if h.get("if-none-match") == "*" and cur:
            return 412, self.error_body(412, "A record with matching key values already exists.")
        if h.get("if-match") == "*" and not cur:
            return 404, self.error_body(404, f"{entity} With Id = {rid} Does Not Exist")
        body = req.body if isinstance(req.body, dict) else {}
        known = set(sum(Dynamics365.mapping_targets.values(), [])) | {"subject", "customerid_contact@odata.bind"}
        bad = [k for k in body if k not in known]
        if bad:
            return 400, self.error_body(
                400, f"Could not find a property named '{bad[0]}' on type 'Microsoft.Dynamics.CRM.{entity[:-1]}'."
            )
        rec = {**(cur or {}), **body, KEY.get(entity, "id"): rid}
        if "lastname" in rec:
            rec["fullname"] = f"{rec.get('firstname', '')} {rec['lastname']}".strip()
        self.put(conn, connection, entity, rid, rec)
        return 204, "", {"OData-EntityId": f"{STANDIN}{API}/{entity}({rid})"}


register(Dynamics365())
