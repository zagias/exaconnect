"""ITSM: ServiceNow incidents and Jira Service Management requests.

A trigger opens a ticket, or adds a note to the open one with the same
dedup key; the resolve closes it. The ticket's id is kept in
connect_incidents so the recovery finds it.
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.parse

from ..transport import Http, sim_ok
from . import Context, Field, Outcome, Provider, ProviderError, facts, raise_for, register, summary, title

SN_URGENCY = {"critical": "1", "warning": "2", "info": "3"}


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _describe(ev: dict) -> str:
    lines = [summary(ev), ""] + [f"{k}: {v}" for k, v in facts(ev)] + ["", f"Event id: {ev['id']}"]
    return "\n".join(lines)


@register
class ServiceNow(Provider):
    key = "servicenow"
    name = "ServiceNow"
    category = "itsm"
    docs = "https://docs.servicenow.com/bundle/washingtondc-api-reference/page/integrate/inbound-rest/concept/c_TableAPI.html"
    api = "Table API: POST /api/now/table/incident, PATCH to add work notes and resolve (state 6)"
    live_needs = "The instance address and an integration user with the itil role (user name and password)."
    fields = (
        Field("instance_url", "Instance address", required=True, kind="url", help="https://<name>.service-now.com"),
        Field("username", "Integration user", required=True),
        Field("password", "Password", secret=True, required=True),
        Field("assignment_group", "Assignment group (sys_id or name)"),
        Field("category", "Category", default="network"),
        Field("close_code", "Close code", default="Solution provided"),
    )

    def _headers(self, ctx: Context) -> dict:
        return {
            "Authorization": _basic(ctx.config.get("username", ""), ctx.secrets.get("password", "")),
            "Accept": "application/json",
        }

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        base = ctx.config["instance_url"]
        action = ev.get("action", "notify")
        dedup = ev.get("dedupkey") or ev["id"]
        open_ = ctx.incident(dedup)
        if action == "notify":
            return Outcome(skipped="ServiceNow gets problems and recoveries only")
        if action == "resolve":
            if open_ is None or open_["state"] != "open":
                return Outcome(skipped="no open incident for this problem")
            resp = ctx.http.request(
                "PATCH",
                f"{base}/api/now/table/incident/{open_['external_id']}",
                headers=self._headers(ctx),
                json_body={
                    "state": "6",
                    "incident_state": "6",
                    "close_code": ctx.config.get("close_code", "Solution provided"),
                    "close_notes": f"Resolved by ExaCarib Connect: {summary(ev)}",
                },
            )
            raise_for(resp, "ServiceNow")
            ctx.resolved(dedup)
            return Outcome(resp.status, {"sys_id": open_["external_id"], "resolved": True})
        if open_ is not None and open_["state"] == "open":
            resp = ctx.http.request(
                "PATCH",
                f"{base}/api/now/table/incident/{open_['external_id']}",
                headers=self._headers(ctx),
                json_body={"work_notes": _describe(ev)},
            )
            raise_for(resp, "ServiceNow")
            return Outcome(resp.status, {"sys_id": open_["external_id"], "updated": True})
        body = {
            "short_description": f"{title(ev)}: {summary(ev)}"[:160],
            "description": _describe(ev),
            "urgency": SN_URGENCY.get(ev.get("severity", "info"), "3"),
            "impact": SN_URGENCY.get(ev.get("severity", "info"), "3"),
            "category": ctx.config.get("category", "network"),
            "correlation_id": dedup[:100],
            "correlation_display": "ExaCarib Connect",
            "contact_type": "monitoring",
        }
        if ctx.config.get("assignment_group"):
            body["assignment_group"] = ctx.config["assignment_group"]
        resp = ctx.http.request(
            "POST",
            f"{base}/api/now/table/incident?sysparm_fields=sys_id,number,state",
            headers=self._headers(ctx),
            json_body=body,
        )
        raise_for(resp, "ServiceNow")
        result = resp.json().get("result") or {}
        sys_id = result.get("sys_id")
        if not sys_id:
            raise ProviderError("ServiceNow did not return the incident's sys_id.", resp.status, retry=False)
        ctx.opened(dedup, sys_id)
        return Outcome(resp.status, {"sys_id": sys_id, "number": result.get("number", "")})

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        if method == "POST":
            return sim_ok(
                201, {"result": {"sys_id": secrets.token_hex(16), "number": "INC0010001", "state": "1"}}
            )
        return sim_ok(200, {"result": {"sys_id": url.rsplit("/", 1)[-1], "state": "6"}})


@register
class JiraServiceManagement(Provider):
    key = "jira"
    name = "Jira Service Management"
    category = "itsm"
    docs = "https://developer.atlassian.com/cloud/jira/service-desk/rest/api-group-request/"
    api = "Service Desk REST API: create request, add a comment, transition to resolve"
    live_needs = "Your Atlassian site, an account email with an API token, the service desk ID and request type ID."
    fields = (
        Field("site_url", "Site address", required=True, kind="url", help="https://<name>.atlassian.net"),
        Field("email", "Account email", required=True),
        Field("api_token", "API token", secret=True, required=True),
        Field("service_desk_id", "Service desk ID", required=True),
        Field("request_type_id", "Request type ID", required=True),
        Field("resolve_transition_id", "Resolve transition ID", help="Found from the transitions list if empty."),
    )

    def _headers(self, ctx: Context) -> dict:
        return {
            "Authorization": _basic(ctx.config.get("email", ""), ctx.secrets.get("api_token", "")),
            "Accept": "application/json",
        }

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        base = ctx.config["site_url"]
        action = ev.get("action", "notify")
        dedup = ev.get("dedupkey") or ev["id"]
        open_ = ctx.incident(dedup)
        if action == "notify":
            return Outcome(skipped="Jira gets problems and recoveries only")
        if action == "resolve":
            if open_ is None or open_["state"] != "open":
                return Outcome(skipped="no open request for this problem")
            key = open_["external_id"]
            tid = ctx.config.get("resolve_transition_id")
            if not tid:
                resp = ctx.http.request(
                    "GET", f"{base}/rest/servicedeskapi/request/{key}/transition", headers=self._headers(ctx)
                )
                raise_for(resp, "Jira")
                values = resp.json().get("values") or []
                pick = next(
                    (v for v in values if str(v.get("name", "")).lower() in ("resolve", "resolve this issue", "done")),
                    values[0] if values else None,
                )
                if pick is None:
                    raise ProviderError("Jira offered no transition to resolve the request.", resp.status, retry=False)
                tid = str(pick["id"])
            resp = ctx.http.request(
                "POST",
                f"{base}/rest/servicedeskapi/request/{key}/transition",
                headers=self._headers(ctx),
                json_body={"id": tid, "additionalComment": {"body": f"Resolved by ExaCarib Connect: {summary(ev)}"}},
            )
            raise_for(resp, "Jira")
            ctx.resolved(dedup)
            return Outcome(resp.status, {"issue_key": key, "resolved": True})
        if open_ is not None and open_["state"] == "open":
            resp = ctx.http.request(
                "POST",
                f"{base}/rest/servicedeskapi/request/{open_['external_id']}/comment",
                headers=self._headers(ctx),
                json_body={"body": _describe(ev), "public": False},
            )
            raise_for(resp, "Jira")
            return Outcome(resp.status, {"issue_key": open_["external_id"], "updated": True})
        resp = ctx.http.request(
            "POST",
            f"{base}/rest/servicedeskapi/request",
            headers=self._headers(ctx),
            json_body={
                "serviceDeskId": str(ctx.config["service_desk_id"]),
                "requestTypeId": str(ctx.config["request_type_id"]),
                "requestFieldValues": {"summary": f"{title(ev)}: {summary(ev)}"[:250], "description": _describe(ev)},
            },
        )
        raise_for(resp, "Jira")
        key = resp.json().get("issueKey")
        if not key:
            raise ProviderError("Jira did not return the request's issue key.", resp.status, retry=False)
        ctx.opened(dedup, key)
        return Outcome(resp.status, {"issue_key": key})

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        path = urllib.parse.urlsplit(url).path
        if method == "GET" and path.endswith("/transition"):
            return sim_ok(200, {"values": [{"id": "761", "name": "Resolve this issue"}]})
        if path.endswith("/rest/servicedeskapi/request"):
            return sim_ok(201, {"issueId": "10001", "issueKey": "NET-1"})
        if path.endswith("/transition"):
            return Http(204, "")
        return sim_ok(201, json.loads(body or b"{}") if body else {})
