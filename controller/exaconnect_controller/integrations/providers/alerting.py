"""Alerting: PagerDuty Events API v2 and Opsgenie Alert API.

A trigger opens an alert keyed on the event's dedup key; the matching
resolve (the path is back up, the node is online again) closes it. News
events go to PagerDuty as change events and are skipped by Opsgenie.
"""

from __future__ import annotations

import urllib.parse

from ..transport import Http, sim_ok
from . import Context, Field, Outcome, Provider, kind_name, link, raise_for, register, summary, title

PD_SEVERITY = {"critical": "critical", "warning": "warning", "info": "info"}


@register
class PagerDuty(Provider):
    key = "pagerduty"
    name = "PagerDuty"
    category = "alerting"
    docs = "https://developer.pagerduty.com/docs/events-api-v2/overview/"
    api = "Events API v2: /v2/enqueue (trigger and resolve with dedup_key) and /v2/change/enqueue"
    live_needs = "An integration (routing) key from a PagerDuty service's Events API v2 integration."
    fields = (
        Field("routing_key", "Integration key", secret=True, required=True),
        Field("base_url", "Events API address", default="https://events.pagerduty.com", kind="url"),
        Field("send_changes", "Send news as change events", default=True, kind="bool"),
    )

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        base = ctx.config.get("base_url", "https://events.pagerduty.com")
        action = ev.get("action", "notify")
        data = ev.get("data") or {}
        if action == "notify":
            if not ctx.config.get("send_changes", True):
                return Outcome(skipped="news events are not sent to PagerDuty")
            body = {
                "routing_key": ctx.secrets["routing_key"],
                "payload": {
                    "summary": summary(ev)[:1024],
                    "timestamp": ev["time"],
                    "source": "ExaCarib Connect",
                    "custom_details": data,
                },
            }
            url = link(ev)
            if url:
                body["links"] = [{"href": url, "text": "Open in Connect"}]
            resp = ctx.http.request("POST", f"{base}/v2/change/enqueue", json_body=body)
            raise_for(resp, "PagerDuty")
            return Outcome(resp.status, {"change": True})
        dedup = ev.get("dedupkey") or ev["id"]
        if action == "resolve":
            body = {"routing_key": ctx.secrets["routing_key"], "event_action": "resolve", "dedup_key": dedup}
        else:
            body = {
                "routing_key": ctx.secrets["routing_key"],
                "event_action": "trigger",
                "dedup_key": dedup,
                "payload": {
                    "summary": summary(ev)[:1024],
                    "source": data.get("site") or "ExaCarib Connect",
                    "severity": PD_SEVERITY.get(ev.get("severity", "info"), "info"),
                    "timestamp": ev["time"],
                    "component": data.get("path") or data.get("node") or "",
                    "group": data.get("site") or "",
                    "class": kind_name(ev),
                    "custom_details": data,
                },
                "client": "ExaCarib Connect",
            }
            url = link(ev)
            if url:
                body["client_url"] = url
                body["links"] = [{"href": url, "text": "Open in Connect"}]
        resp = ctx.http.request("POST", f"{base}/v2/enqueue", json_body=body)
        raise_for(resp, "PagerDuty")
        if action == "resolve":
            ctx.resolved(dedup)
        else:
            ctx.opened(dedup, resp.json().get("dedup_key", dedup))
        return Outcome(resp.status, {"dedup_key": dedup, "event_action": body["event_action"]})

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        import json

        try:
            dedup = json.loads(body).get("dedup_key", "")
        except ValueError:
            dedup = ""
        return sim_ok(202, {"status": "success", "message": "Event processed", "dedup_key": dedup})


OG_PRIORITY = {"critical": "P1", "warning": "P3", "info": "P5"}


@register
class Opsgenie(Provider):
    key = "opsgenie"
    name = "Opsgenie"
    category = "alerting"
    docs = "https://docs.opsgenie.com/docs/alert-api"
    api = "Alert API v2: create alert with alias, close alert by alias"
    live_needs = "An API key from an Opsgenie API integration (GenieKey), and the EU address if your account is in the EU."
    fields = (
        Field("api_key", "API key", secret=True, required=True),
        Field(
            "base_url",
            "API address",
            default="https://api.opsgenie.com",
            kind="url",
            help="https://api.eu.opsgenie.com for EU accounts.",
        ),
        Field("responders", "Responder team names", kind="list"),
    )

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        base = ctx.config.get("base_url", "https://api.opsgenie.com")
        headers = {"Authorization": f"GenieKey {ctx.secrets['api_key']}"}
        action = ev.get("action", "notify")
        alias = (ev.get("dedupkey") or ev["id"])[:512]
        if action == "notify":
            return Outcome(skipped="Opsgenie gets problems and recoveries only")
        if action == "resolve":
            resp = ctx.http.request(
                "POST",
                f"{base}/v2/alerts/{urllib.parse.quote(alias, safe='')}/close?identifierType=alias",
                headers=headers,
                json_body={"source": "ExaCarib Connect", "user": "ExaCarib Connect", "note": summary(ev)[:25000]},
            )
            if resp.status == 404:
                return Outcome(404, skipped="no open alert with this alias")
            raise_for(resp, "Opsgenie")
            ctx.resolved(alias)
            return Outcome(resp.status, {"alias": alias, "closed": True})
        data = ev.get("data") or {}
        body = {
            "message": f"{title(ev)}: {summary(ev)}"[:130],
            "alias": alias,
            "description": summary(ev)[:15000],
            "priority": OG_PRIORITY.get(ev.get("severity", "info"), "P5"),
            "source": "ExaCarib Connect",
            "entity": str(data.get("site") or data.get("node") or "")[:512],
            "tags": [t for t in ("exacarib", kind_name(ev), str(data.get("site") or "")) if t][:20],
            "details": {k: str(v)[:500] for k, v in data.items() if not isinstance(v, (dict, list))},
        }
        if ctx.config.get("responders"):
            body["responders"] = [{"name": n, "type": "team"} for n in ctx.config["responders"]]
        resp = ctx.http.request("POST", f"{base}/v2/alerts", headers=headers, json_body=body)
        raise_for(resp, "Opsgenie")
        ctx.opened(alias, resp.json().get("requestId", alias))
        return Outcome(resp.status, {"alias": alias})

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        return sim_ok(202, {"result": "Request will be processed", "took": 0.01, "requestId": "sim-request"})
