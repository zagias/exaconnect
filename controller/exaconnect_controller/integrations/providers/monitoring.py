"""Monitoring and SIEM: Datadog, Splunk HEC, Elastic or OpenSearch, and
Microsoft Sentinel through the Azure Monitor Logs Ingestion API."""

from __future__ import annotations

import base64
import datetime as dt
import json
import threading
import time
import urllib.parse

from ..transport import Http, sim_ok
from . import Context, Field, Outcome, Provider, ProviderError, kind_name, raise_for, register, summary, title

DD_ALERT = {"critical": "error", "warning": "warning", "info": "info"}


def _epoch(t: str) -> float:
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _tags(ev: dict) -> list[str]:
    data = ev.get("data") or {}
    tags = ["source:exacarib-connect", f"event:{kind_name(ev)}", f"severity:{ev.get('severity', 'info')}"]
    for k in ("site", "path", "class", "carrier", "node"):
        if data.get(k):
            tags.append(f"{k}:{str(data[k]).lower().replace(' ', '_')}")
    return tags


@register
class Datadog(Provider):
    key = "datadog"
    name = "Datadog"
    category = "monitoring"
    docs = "https://docs.datadoghq.com/api/latest/events/#post-an-event"
    api = "Events API v1 (POST /api/v1/events) with DD-API-KEY; metrics by scraping the OpenMetrics endpoint"
    live_needs = "A Datadog API key and your Datadog site (datadoghq.com, datadoghq.eu, us3.datadoghq.com...)."
    fields = (
        Field("api_key", "API key", secret=True, required=True),
        Field("site", "Datadog site", default="datadoghq.com"),
        Field("base_url", "API address (optional)", kind="url", help="Overrides https://api.<site>."),
    )

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        base = ctx.config.get("base_url") or f"https://api.{ctx.config.get('site', 'datadoghq.com')}"
        body = {
            "title": title(ev)[:100],
            "text": summary(ev)[:4000],
            "alert_type": "success" if ev.get("action") == "resolve" else DD_ALERT.get(ev.get("severity"), "info"),
            "date_happened": int(_epoch(ev["time"])),
            "aggregation_key": (ev.get("dedupkey") or ev["id"])[:100],
            "source_type_name": "exacarib connect",
            "tags": _tags(ev),
        }
        resp = ctx.http.request(
            "POST", f"{base}/api/v1/events", headers={"DD-API-KEY": ctx.secrets["api_key"]}, json_body=body
        )
        raise_for(resp, "Datadog")
        return Outcome(resp.status, {"id": (resp.json().get("event") or {}).get("id")})

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        return sim_ok(202, {"status": "ok", "event": {"id": 1}})


@register
class SplunkHec(Provider):
    key = "splunk"
    name = "Splunk (HTTP Event Collector)"
    category = "siem"
    docs = "https://docs.splunk.com/Documentation/Splunk/latest/Data/HECRESTendpoints"
    api = "HEC: POST /services/collector/event with 'Authorization: Splunk <token>'"
    live_needs = "Your HEC address (https://<host>:8088 or Splunk Cloud's) and an HEC token; optionally an index."
    fields = (
        Field("url", "HEC address", required=True, kind="url"),
        Field("token", "HEC token", secret=True, required=True),
        Field("index", "Index"),
        Field("sourcetype", "Source type", default="exacarib:connect"),
    )

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        body = {
            "time": round(_epoch(ev["time"]), 3),
            "host": "exacarib-connect",
            "source": "exacarib:connect",
            "sourcetype": ctx.config.get("sourcetype", "exacarib:connect"),
            "event": ev,
        }
        if ctx.config.get("index"):
            body["index"] = ctx.config["index"]
        resp = ctx.http.request(
            "POST",
            f"{ctx.config['url']}/services/collector/event",
            headers={"Authorization": f"Splunk {ctx.secrets['token']}"},
            json_body=body,
        )
        raise_for(resp, "Splunk")
        if resp.json().get("code", 0) != 0:
            raise ProviderError(f"Splunk said {resp.json().get('text')}.", resp.status, retry=False)
        return Outcome(resp.status)

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        return sim_ok(200, {"text": "Success", "code": 0})


@register
class ElasticBulk(Provider):
    key = "elastic"
    name = "Elastic or OpenSearch"
    category = "siem"
    docs = "https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-bulk.html"
    api = "Bulk API: POST /_bulk with NDJSON create actions (the event id is the document id)"
    live_needs = "Your cluster address and an API key (Elastic) or a user name and password (OpenSearch)."
    fields = (
        Field("url", "Cluster address", required=True, kind="url"),
        Field("index", "Index or data stream", default="exacarib-connect-events"),
        Field("username", "User name (OpenSearch)"),
        Field("password", "Password (OpenSearch)", secret=True),
        Field("api_key", "API key (Elastic, base64 id:key)", secret=True),
    )

    def credentials_present(self, config: dict, secrets: dict) -> bool:
        return bool(config.get("url") and (secrets.get("api_key") or secrets.get("password")))

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        index = ctx.config.get("index", "exacarib-connect-events")
        doc = {"@timestamp": ev["time"], "event": {"kind": "event", "dataset": "exacarib.connect"}, **ev}
        body = (
            json.dumps({"create": {"_index": index, "_id": ev["id"]}})
            + "\n"
            + json.dumps(doc, default=str)
            + "\n"
        ).encode()
        headers = {"Content-Type": "application/x-ndjson"}
        if ctx.secrets.get("api_key"):
            headers["Authorization"] = f"ApiKey {ctx.secrets['api_key']}"
        else:
            raw = f"{ctx.config.get('username', '')}:{ctx.secrets.get('password', '')}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        resp = ctx.http.request("POST", f"{ctx.config['url']}/_bulk", headers=headers, body=body)
        raise_for(resp, "The cluster")
        out = resp.json()
        if out.get("errors"):
            item = ((out.get("items") or [{}])[0].get("create") or {})
            if item.get("status") != 409:  # 409: already indexed (a retry), which is fine
                err = (item.get("error") or {}).get("reason", "unknown")
                raise ProviderError(f"The cluster refused the document: {err}", item.get("status"), retry=False)
        return Outcome(resp.status)

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        return sim_ok(200, {"took": 1, "errors": False, "items": [{"create": {"status": 201, "result": "created"}}]})


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


@register
class Sentinel(Provider):
    key = "sentinel"
    name = "Microsoft Sentinel"
    category = "siem"
    docs = "https://learn.microsoft.com/en-us/azure/azure-monitor/logs/logs-ingestion-api-overview"
    api = "Azure Monitor Logs Ingestion API: client-credentials token, then POST to a data collection rule stream"
    live_needs = (
        "An Entra app registration (tenant ID, client ID, client secret) with Monitoring Metrics Publisher on the "
        "data collection rule, the data collection endpoint, the rule's immutable ID and the stream name."
    )
    fields = (
        Field("tenant_id", "Tenant ID", required=True),
        Field("client_id", "Client ID", required=True),
        Field("client_secret", "Client secret", secret=True, required=True),
        Field("endpoint", "Data collection endpoint", required=True, kind="url"),
        Field("dcr_id", "Data collection rule immutable ID", required=True),
        Field("stream", "Stream name", default="Custom-ExaCaribConnect_CL"),
        Field("login_url", "Sign-in address", default="https://login.microsoftonline.com", kind="url"),
    )

    def _token(self, ctx: Context) -> str:
        key = f"{ctx.integration['id']}:{ctx.config['client_id']}"
        with _tokens_lock:
            tok = _tokens.get(key)
            if tok and tok[1] > time.time() + 60 and ctx.http.live:
                return tok[0]
        resp = ctx.http.request(
            "POST",
            f"{ctx.config.get('login_url', 'https://login.microsoftonline.com')}/"
            f"{urllib.parse.quote(ctx.config['tenant_id'], safe='')}/oauth2/v2.0/token",
            form={
                "grant_type": "client_credentials",
                "client_id": ctx.config["client_id"],
                "client_secret": ctx.secrets["client_secret"],
                "scope": "https://monitor.azure.com//.default",
            },
        )
        raise_for(resp, "Microsoft sign-in")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise ProviderError("Microsoft sign-in returned no token.", resp.status, retry=False)
        ctx.http.secret_values.append(token)
        with _tokens_lock:
            _tokens[key] = (token, time.time() + float(body.get("expires_in", 3600)))
        return token

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        token = self._token(ctx)
        data = ev.get("data") or {}
        record = {
            "TimeGenerated": ev["time"],
            "EventId": ev["id"],
            "EventType": kind_name(ev),
            "Severity": ev.get("severity", "info"),
            "Action": ev.get("action", "notify"),
            "Summary": summary(ev),
            "Site": str(data.get("site") or ""),
            "Path": str(data.get("path") or ""),
            "Carrier": str(data.get("carrier") or ""),
            "OrganisationId": str(ev.get("organisationid") or ""),
            "Data": data,
        }
        url = (
            f"{ctx.config['endpoint']}/dataCollectionRules/{urllib.parse.quote(ctx.config['dcr_id'], safe='')}"
            f"/streams/{urllib.parse.quote(ctx.config.get('stream', 'Custom-ExaCaribConnect_CL'), safe='')}"
            "?api-version=2023-01-01"
        )
        resp = ctx.http.request("POST", url, headers={"Authorization": f"Bearer {token}"}, json_body=[record])
        if resp.status == 401:
            with _tokens_lock:
                _tokens.pop(f"{ctx.integration['id']}:{ctx.config['client_id']}", None)
        raise_for(resp, "Azure Monitor")
        return Outcome(resp.status)

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        if "/oauth2/" in url:
            return sim_ok(200, {"token_type": "Bearer", "expires_in": 3599, "access_token": "simulated-token"})
        return Http(204, "")
