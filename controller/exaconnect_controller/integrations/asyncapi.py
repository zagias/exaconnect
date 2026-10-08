"""AsyncAPI 3.0 document for Connect's webhooks, built from the catalogue."""

from __future__ import annotations

from typing import Any

from .. import __version__
from .catalogue import KINDS, SOURCE, type_of


def _msg_name(name: str) -> str:
    return "".join(p.capitalize() for p in name.replace("_", ".").split("."))


def document() -> dict[str, Any]:
    messages: dict[str, Any] = {}
    for k in KINDS.values():
        messages[_msg_name(k.name)] = {
            "name": type_of(k.name),
            "title": k.title,
            "summary": k.description,
            "contentType": "application/cloudevents+json",
            "headers": {"$ref": "#/components/schemas/StandardWebhooksHeaders"},
            "payload": {
                "allOf": [
                    {"$ref": "#/components/schemas/CloudEvent"},
                    {
                        "type": "object",
                        "properties": {
                            "type": {"const": type_of(k.name)},
                            "severity": {"default": k.severity},
                            "action": {"const": k.action},
                        },
                    },
                ]
            },
            "examples": [
                {
                    "name": k.name,
                    "payload": {
                        "specversion": "1.0",
                        "id": f"evt-example-{k.name}",
                        "source": SOURCE + "/organisations/00000000-0000-0000-0000-000000000000",
                        "type": type_of(k.name),
                        "time": "2026-10-07T12:00:00Z",
                        "datacontenttype": "application/json",
                        "severity": k.severity,
                        "action": k.action,
                        "data": {**k.example, "summary": k.title},
                    },
                }
            ],
            "x-sent-to-carriers": k.carrier,
            "x-in-wildcard": k.in_wildcard,
        }
    return {
        "asyncapi": "3.0.0",
        "info": {
            "title": "ExaCarib Connect events",
            "version": __version__,
            "description": (
                "Events Connect sends to your webhooks, as CloudEvents 1.0 over HTTP (structured mode shown; binary "
                "mode sends `data` as the body and the other attributes as `ce-*` headers). Every delivery is signed "
                "per Standard Webhooks. Subscribe in the portal, with POST /api/v1/integrations, or as a REST hook "
                "with POST /api/v1/hooks. The same events feed Slack, Teams, PagerDuty, Opsgenie, ServiceNow, Jira, "
                "Datadog, Splunk, Elastic, Sentinel, OTLP, syslog and SNMP connectors."
            ),
        },
        "servers": {
            "your-endpoint": {
                "host": "your.endpoint.example",
                "protocol": "https",
                "description": "Your HTTPS endpoint. Connect POSTs to it.",
            }
        },
        "channels": {
            "webhook": {
                "address": "/",
                "description": "Your endpoint. One POST per event; retried with backoff on 5xx, 429 or time-out.",
                "messages": {n: {"$ref": f"#/components/messages/{n}"} for n in messages},
            }
        },
        "operations": {
            "receiveEvent": {
                "action": "receive",
                "channel": {"$ref": "#/channels/webhook"},
                "summary": "Receive a Connect event.",
            }
        },
        "components": {
            "messages": messages,
            "schemas": {
                "CloudEvent": {
                    "type": "object",
                    "required": ["specversion", "id", "source", "type", "time", "data"],
                    "properties": {
                        "specversion": {"type": "string", "const": "1.0"},
                        "id": {"type": "string", "description": "Stable; the same on every retry."},
                        "source": {"type": "string", "format": "uri-reference"},
                        "type": {"type": "string"},
                        "subject": {"type": "string"},
                        "time": {"type": "string", "format": "date-time"},
                        "datacontenttype": {"type": "string", "const": "application/json"},
                        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                        "action": {"type": "string", "enum": ["trigger", "resolve", "notify"]},
                        "dedupkey": {
                            "type": "string",
                            "description": "A trigger and the resolve that closes it share this key.",
                        },
                        "organisationid": {"type": "string", "format": "uuid"},
                        "data": {
                            "type": "object",
                            "properties": {"summary": {"type": "string"}},
                            "additionalProperties": True,
                        },
                    },
                },
                "StandardWebhooksHeaders": {
                    "type": "object",
                    "properties": {
                        "webhook-id": {"type": "string", "description": "The event id."},
                        "webhook-timestamp": {"type": "string", "description": "Unix seconds of this attempt."},
                        "webhook-signature": {
                            "type": "string",
                            "description": "v1,<base64 HMAC-SHA256 of '<id>.<timestamp>.<body>' keyed with the "
                            "base64-decoded part of your whsec_ secret>",
                        },
                    },
                },
            },
        },
    }
