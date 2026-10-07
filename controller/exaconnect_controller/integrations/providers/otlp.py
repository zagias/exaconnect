"""OpenTelemetry: OTLP/HTTP (JSON encoding) export of logs and metrics.

Events go out as log records on /v1/logs as they happen; the metrics
behind the Prometheus endpoint go out as gauges on /v1/metrics every
minute. The Grafana Cloud profile is the same exporter with Grafana's
gateway address and Basic authentication (instance ID and an access policy
token).
"""

from __future__ import annotations

import base64
import datetime as dt
import time
from typing import Any

from ... import __version__
from . import Context, Field, Outcome, Provider, kind_name, raise_for, register, summary

SEVERITY = {"info": (9, "INFO"), "warning": (13, "WARN"), "critical": (17, "ERROR")}


def _attr(key: str, value: Any) -> dict:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def resource() -> dict:
    return {
        "attributes": [
            _attr("service.name", "exacarib-connect"),
            _attr("service.version", __version__),
            _attr("telemetry.sdk.name", "exacarib-connect"),
        ]
    }


def _nanos(t: str | dt.datetime | float) -> str:
    if isinstance(t, (int, float)):
        return str(int(t * 1e9))
    if isinstance(t, dt.datetime):
        return str(int(t.timestamp() * 1e9))
    try:
        return str(int(dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1e9))
    except ValueError:
        return str(int(time.time() * 1e9))


def logs_payload(events: list[dict]) -> dict:
    records = []
    for ev in events:
        num, text = SEVERITY.get(ev.get("severity", "info"), (9, "INFO"))
        data = ev.get("data") or {}
        attrs = [
            _attr("event.name", kind_name(ev)),
            _attr("event.id", ev["id"]),
            _attr("cloudevents.event_type", ev["type"]),
            _attr("cloudevents.event_source", ev["source"]),
            _attr("exacarib.action", ev.get("action", "notify")),
        ]
        if ev.get("dedupkey"):
            attrs.append(_attr("exacarib.dedup_key", ev["dedupkey"]))
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool)) and k != "summary":
                attrs.append(_attr(f"exacarib.{k}", v))
        records.append(
            {
                "timeUnixNano": _nanos(ev["time"]),
                "observedTimeUnixNano": _nanos(time.time()),
                "severityNumber": num,
                "severityText": text,
                "body": {"stringValue": summary(ev)},
                "attributes": attrs,
            }
        )
    return {
        "resourceLogs": [
            {
                "resource": resource(),
                "scopeLogs": [{"scope": {"name": "exacarib.connect.events", "version": "1"}, "logRecords": records}],
            }
        ]
    }


def metrics_payload(families: list[dict], at: float | None = None) -> dict:
    """Prometheus-style families (metrics.collect) as OTLP gauges."""
    ts = _nanos(at if at is not None else time.time())
    metrics = []
    for f in families:
        points = [
            {"attributes": [_attr(k, v) for k, v in labels.items()], "timeUnixNano": ts, "asDouble": float(value)}
            for labels, value in f["samples"]
        ]
        if not points:
            continue
        metrics.append(
            {
                "name": f.get("otel") or f["name"],
                "description": f["help"],
                "unit": f.get("unit", ""),
                "gauge": {"dataPoints": points},
            }
        )
    return {
        "resourceMetrics": [
            {
                "resource": resource(),
                "scopeMetrics": [{"scope": {"name": "exacarib.connect", "version": "1"}, "metrics": metrics}],
            }
        ]
    }


class _Otlp(Provider):
    category = "logs"
    docs = "https://opentelemetry.io/docs/specs/otlp/#otlphttp"
    api = "OTLP/HTTP with JSON encoding: POST /v1/logs and /v1/metrics"

    def endpoint(self, ctx: Context) -> str:
        return str(ctx.config.get("endpoint", "")).rstrip("/")

    def headers(self, ctx: Context) -> dict:
        raise NotImplementedError

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        resp = ctx.http.request(
            "POST", f"{self.endpoint(ctx)}/v1/logs", headers=self.headers(ctx), json_body=logs_payload([ev])
        )
        raise_for(resp, "The OTLP endpoint")
        return Outcome(resp.status)

    def export_metrics(self, ctx: Context, families: list[dict]) -> int:
        resp = ctx.http.request(
            "POST", f"{self.endpoint(ctx)}/v1/metrics", headers=self.headers(ctx), json_body=metrics_payload(families)
        )
        raise_for(resp, "The OTLP endpoint")
        return resp.status


@register
class Otlp(_Otlp):
    key = "otlp"
    name = "OpenTelemetry (OTLP/HTTP)"
    live_needs = "An OTLP/HTTP endpoint (a collector or a vendor's gateway) and, if it needs one, an auth header."
    fields = (
        Field("endpoint", "OTLP/HTTP endpoint", required=True, kind="url", help="Without /v1/logs, e.g. https://otel.example.org:4318"),
        Field("header_name", "Auth header name", default="Authorization"),
        Field("header_value", "Auth header value", secret=True),
        Field("export_metrics", "Export metrics every minute", default=True, kind="bool"),
    )

    def headers(self, ctx: Context) -> dict:
        if ctx.secrets.get("header_value"):
            return {ctx.config.get("header_name") or "Authorization": ctx.secrets["header_value"]}
        return {}


@register
class GrafanaCloud(_Otlp):
    key = "grafana_cloud"
    name = "Grafana Cloud (OTLP)"
    docs = "https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/"
    live_needs = (
        "Your Grafana Cloud stack's OTLP endpoint and instance ID, and an access policy token with "
        "metrics:write and logs:write."
    )
    fields = (
        Field(
            "endpoint",
            "OTLP endpoint",
            default="https://otlp-gateway-prod-us-east-0.grafana.net/otlp",
            kind="url",
            required=True,
        ),
        Field("instance_id", "Instance ID", required=True),
        Field("token", "Access policy token", secret=True, required=True),
        Field("export_metrics", "Export metrics every minute", default=True, kind="bool"),
    )

    def headers(self, ctx: Context) -> dict:
        raw = f"{ctx.config.get('instance_id', '')}:{ctx.secrets.get('token', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
