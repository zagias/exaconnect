"""ServiceNow connector (ADR 0035): incidents linked to conversations.

Written from ServiceNow's REST Table API (``/api/now/table/incident`` and
``sys_user``). Each business has its own instance; it creates an API key
for an integration user (REST API Key, header ``x-sn-apikey``) with the
itil role and pastes it through secure entry, and sets its instance name.
ExaCarib registers nothing with ServiceNow; ``feature/integration-servicenow``
must be switched on before a connection leaves the simulated stand-in.

- Idempotent creates: the incident's ``correlation_id`` is a reference from
  the idempotency key, queried before creating.
- Pages: ``sysparm_offset`` and the ``Link: <...>;rel="next"`` header. Rate
  limits: 429 with Retry-After.
- Status back: a business rule on incident sends an outbound REST message
  to Jibsy, signed with ``X-Jibsy-Signature`` = base64 HMAC-SHA256 of the
  body under a secret Jibsy gives the business.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from ..automation import oauth
from . import kit
from .helpdesk import Helpdesk
from .more_common import app_hook, hook_url

STATE_IN = {"1": "new", "2": "open", "3": "on_hold", "6": "solved", "7": "closed", "8": "closed"}
STATE_OUT = {"new": "1", "open": "2", "pending": "3", "on_hold": "3", "solved": "6", "closed": "7"}
# Jibsy priority -> (urgency, impact); ServiceNow derives priority from both.
URGENCY = {"urgent": ("1", "1"), "high": ("1", "2"), "normal": ("2", "2"), "low": ("3", "3")}
PRIORITY_IN = {"1": "urgent", "2": "high", "3": "normal", "4": "low", "5": "low"}
FIELDS = "sys_id,number,short_description,state,priority,sys_updated_on,correlation_id"

BUSINESS_RULE = """(function executeRule(current, previous) {
  var body = JSON.stringify({id: current.sys_id + '-' + current.sys_mod_count, sys_id: '' + current.sys_id,
                             number: '' + current.number, state: '' + current.state});
  var mac = new GlideCertificateEncryption().generateMac(gs.base64Encode('SECRET'), 'HmacSHA256', body);
  var r = new sn_ws.RESTMessageV2();
  r.setEndpoint('ADDRESS'); r.setHttpMethod('post');
  r.setRequestHeader('Content-Type', 'application/json'); r.setRequestHeader('X-Jibsy-Signature', mac);
  r.setRequestBody(body); r.executeAsync();
})(current, previous);"""


def _next_link(headers: dict) -> str | None:
    for part in kit.header(headers, "Link").split(","):
        m = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


class ServiceNowSim(kit.Simulator):
    app = "servicenow"

    def error_body(self, status: int, message: str) -> Any:
        return {"error": {"message": message, "detail": ""}, "status": "failure"}

    def _match(self, rec: dict, query: str) -> bool:
        for cond in query.split("^"):
            if not cond or cond.startswith("ORDERBY"):
                continue
            k, _, v = cond.partition("=")
            if str(rec.get(k, "")).lower() != v.lower():
                return False
        return True

    @kit.Simulator.route("GET", r"/api/now/table/sys_user$")
    def users(self, conn, c, req, m):
        return 200, {
            "result": [u for u in self.all(conn, c, "user") if self._match(u, req.query.get("sysparm_query", ""))]
        }

    @kit.Simulator.route("GET", r"/api/now/table/incident$")
    def query(self, conn, c, req, m):
        conds = re.split(r"\^OR(?!DERBY)", req.query.get("sysparm_query", ""))
        hits = [i for i in self.all(conn, c, "incident") if any(self._match(i, x) for x in conds)]
        off, lim = int(req.query.get("sysparm_offset", "0")), int(req.query.get("sysparm_limit", "100"))
        h = {}
        if off + lim < len(hits):
            nxt = urllib.parse.urlencode({**req.query, "sysparm_offset": off + lim})
            h["Link"] = f'<{req.url.split("?")[0]}?{nxt}>;rel="next"'
        return 200, {"result": hits[off : off + lim]}, h

    @kit.Simulator.route("POST", r"/api/now/table/incident$")
    def create(self, conn, c, req, m):
        b = req.body or {}
        if not b.get("short_description"):
            return 400, self.error_body(400, "short_description is mandatory")
        n = self.new_id(conn, c, "incident", digits=True)
        sid = self.new_id(conn, c, "incident").replace("inc", "")
        mail = re.search(r"<([^>]+@[^>]+)>", b.get("description", ""))
        rec = {
            **b,
            "caller_id.email": mail.group(1).lower() if mail else "",
            "sys_id": sid + "0" * (32 - len(sid)),
            "number": f"INC00{n}",
            "state": "1",
            "priority": "3",
            "sys_updated_on": "2026-10-07 10:00:00",
            "sys_mod_count": "0",
        }
        self.put(conn, c, "incident", rec["sys_id"], rec)
        return 201, {"result": rec}

    @kit.Simulator.route("PATCH", r"/api/now/table/incident/([0-9a-f]{32})$")
    def update(self, conn, c, req, m):
        rec = self.get(conn, c, "incident", m.group(1))
        if not rec:
            return 404, self.error_body(404, "No Record found")
        b = dict(req.body or {})
        if b.get("state") == "6" and not (b.get("close_code") and b.get("close_notes")):
            return 403, self.error_body(403, "Data Policy Exception: Resolution code and notes are mandatory")
        rec.update(b)
        rec["sys_mod_count"] = str(int(rec.get("sys_mod_count", "0")) + 1)
        self.put(conn, c, "incident", m.group(1), rec)
        return 200, {"result": rec}


class ServiceNow(Helpdesk):
    app = "servicenow"
    label = "ServiceNow"
    description = "Create, update and look up ServiceNow incidents, linked to the conversation they came from."
    auth = "credentials"
    credentials = (kit.Credential("api_key", "REST API key", pattern=r"[A-Za-z0-9_.-]{16,200}"),)
    settings_fields = (
        kit.Setting(
            "instance", "Instance name (the part before .service-now.com)", r"[a-z0-9][a-z0-9-]{0,62}", required=True
        ),
    )
    simulator = ServiceNowSim()
    health_path = "/api/now/table/incident?sysparm_limit=1&sysparm_fields=sys_id"
    needs_from_exacarib = "Nothing to register: each business creates a REST API key for an integration user."
    webhooks = "A business rule sends signed incident changes to Jibsy (script given in Jibsy)."
    docs_url = "https://developer.servicenow.com/dev.do#!/reference/api/latest/rest/c_TableAPI"
    mapping_targets = {
        "ticket": ["short_description", "description", "urgency", "impact", "caller_id", "assignment_group", "category"]
    }

    def base_url(self, conn, connection: dict) -> str:
        return f"https://{self.settings(connection).get('instance', 'example')}.service-now.com"

    def live_auth_headers(self, conn, connection: dict) -> dict:
        return {"x-sn-apikey": oauth.credentials(conn, connection)["api_key"]}

    def _t(self, connection: dict, i: dict) -> dict:
        return {
            "id": i.get("sys_id", ""),
            "number": i.get("number", ""),
            "subject": i.get("short_description", ""),
            "status": STATE_IN.get(str(i.get("state", "")), str(i.get("state", ""))),
            "priority": PRIORITY_IN.get(str(i.get("priority", "")), ""),
            "updated_at": i.get("sys_updated_on", ""),
            "url": f"{self.base_url(None, connection)}/nav_to.do?uri=incident.do?sys_id={i.get('sys_id', '')}",
        }

    def _query(self, conn, connection, query: str, limit: int = 1) -> list[dict]:
        r = self.call(
            conn,
            connection,
            "GET",
            "/api/now/table/incident",
            params={"sysparm_query": query, "sysparm_limit": limit, "sysparm_fields": FIELDS},
        )
        return (r.body or {}).get("result") or []

    def _sys_id(self, conn, connection, ticket_id: str) -> str:
        if re.fullmatch(r"[0-9a-f]{32}", ticket_id):
            return ticket_id
        if not re.fullmatch(r"[A-Za-z0-9]{3,40}", ticket_id):
            raise kit.ConnectorError("That doesn't look like an incident number.", "input")
        hits = self._query(conn, connection, f"number={ticket_id.upper()}")
        if not hits:
            raise kit.ConnectorError(f"No incident {ticket_id}.", "input")
        return hits[0]["sys_id"]

    def _get(self, conn, connection, ticket_id):
        if not re.fullmatch(r"[A-Za-z0-9]{3,40}", ticket_id):
            return None
        hits = self._query(conn, connection, f"number={ticket_id.upper()}^ORsys_id={ticket_id}")
        return self._t(connection, hits[0]) if hits else None

    def _list(self, conn, connection, email):
        items = self.paginate(
            conn,
            connection,
            "/api/now/table/incident",
            params={
                "sysparm_query": f"caller_id.email={email.lower()}^ORDERBYDESCsys_updated_on",
                "sysparm_limit": 50,
                "sysparm_fields": FIELDS,
            },
            items=lambda b: (b or {}).get("result") or [],
            next_page=lambda r: _next_link(r.headers),
            max_items=100,
        )
        return [self._t(connection, i) for i in items]

    def _find_ref(self, conn, connection, ref):
        hits = self._query(conn, connection, f"correlation_id={ref}")
        return self._t(connection, hits[0]) if hits else None

    def _create_body(self, connection, inputs, ref):
        urgency, impact = URGENCY[inputs.get("priority") or "normal"]
        who = f"{inputs.get('name', '')} <{inputs['email']}>".strip()
        return {
            "short_description": inputs["subject"][:160],
            "description": f"{inputs['description']}\n\nRequester: {who}\nRaised through Jibsy by ExaCarib.",
            "contact_type": "chat",
            "urgency": urgency,
            "impact": impact,
            "correlation_id": ref,
            "correlation_display": "Jibsy by ExaCarib",
        }

    def _create(self, conn, connection, inputs, ref, key):
        body = self._create_body(connection, inputs, ref)
        r = self.call(
            conn,
            connection,
            "GET",
            "/api/now/table/sys_user",
            params={
                "sysparm_query": f"email={inputs['email'].lower()}",
                "sysparm_limit": 1,
                "sysparm_fields": "sys_id",
            },
        )
        users = (r.body or {}).get("result") or []
        if users:
            body["caller_id"] = users[0]["sys_id"]
        r = self.call(conn, connection, "POST", "/api/now/table/incident", json_body=body)
        return self._t(connection, r.body["result"])

    def _update(self, conn, connection, ticket_id, status, priority):
        sid = self._sys_id(conn, connection, ticket_id)
        body: dict = {}
        if status:
            body["state"] = STATE_OUT[status]
            if status in ("solved", "closed"):
                body.update(close_code="Solved (Permanently)", close_notes="Resolved through Jibsy by ExaCarib.")
        if priority:
            body["urgency"], body["impact"] = URGENCY[priority]
        r = self.call(conn, connection, "PATCH", f"/api/now/table/incident/{sid}", json_body=body)
        return self._t(connection, r.body["result"])

    def _comment(self, conn, connection, ticket_id, body, public, key):
        sid = self._sys_id(conn, connection, ticket_id)
        r = self.call(
            conn,
            connection,
            "PATCH",
            f"/api/now/table/incident/{sid}",
            json_body={("comments" if public else "work_notes"): body},
        )
        return {"id": f"{sid}:{r.body['result'].get('sys_mod_count', '')}", "public": public}

    # ---- webhooks ----------------------------------------------------------------------

    def register_webhooks(self, conn, connection: dict, actor: str) -> dict:
        """ServiceNow can't be set up from here: the answer is a business rule
        script (after update, on incident, when State changes) with the address
        and secret filled in. Shown this once."""
        hook = app_hook(conn, connection["customer_id"], self.app, actor)
        script = BUSINESS_RULE.replace("SECRET", hook["secret"]).replace("ADDRESS", hook_url(hook))
        return {
            "manual": True,
            "address": hook_url(hook),
            "business_rule": script,
            "when": "After update on Incident, condition: State changes",
        }

    def verify_webhook(self, conn, connection, hook, headers, body, query) -> bool:
        return kit.same(kit.header(headers, "X-Jibsy-Signature"), kit.hmac_b64(hook.get("secret", ""), body))

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        try:
            d = json.loads(body)
        except ValueError:
            return []
        if not d.get("sys_id"):
            return []
        status = STATE_IN.get(str(d.get("state", "")), "")
        return [
            {
                "type": "ticket.updated",
                "id": str(d.get("id") or ""),
                "data": {"ticket_id": str(d["sys_id"]), "status": status},
            }
        ]


kit.register(ServiceNow())
