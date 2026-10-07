"""Published descriptions of CommAI (ADR 0034).

- OpenAPI 3.1: every /api/v1/commai endpoint, taken from the running app's
  own schema (so it never drifts from the code), with only the components
  those endpoints use.
- AsyncAPI 3.0: the event stream a business receives by webhook, in both
  formats (ExaCarib JSON and CloudEvents 1.0), with the signature headers
  (ExaCarib v1 and Standard Webhooks), and the signed inbound webhooks a
  business can send to CommAI.

``check_openapi`` and ``check_asyncapi`` are structural checks used by the
tests (the full JSON Schema validators are not dependencies here).
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .. import events
from . import webhooks_std

PREFIX = "/api/v1/commai"
TITLE = "ExaCarib Connect: CommAI API"


def _refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        r = node.get("$ref")
        if isinstance(r, str) and r.startswith("#/components/schemas/"):
            out.add(r.rsplit("/", 1)[1])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def openapi(full: dict, server: str = "") -> dict:
    """The CommAI part of the app's OpenAPI document."""
    paths = {p: copy.deepcopy(v) for p, v in full.get("paths", {}).items() if p.startswith(PREFIX)}
    schemas = full.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    _refs(paths, wanted)
    done: set[str] = set()
    while wanted - done:
        name = (wanted - done).pop()
        done.add(name)
        _refs(schemas.get(name, {}), wanted)
    tags = sorted(
        {t for item in paths.values() for op in item.values() if isinstance(op, dict) for t in op.get("tags", [])}
    )
    doc = {
        "openapi": full.get("openapi", "3.1.0"),
        "info": {
            "title": TITLE,
            "version": str(full.get("info", {}).get("version", "1")),
            "description": "Conversations, contacts, channels, AI, automation, integrations and voice for "
            "businesses on ExaCarib Connect. Sign in with a bearer token (a session or an API key "
            "starting exa_). Writes accept an Idempotency-Key header.",
        },
        "servers": [{"url": server.rstrip("/") or "/"}],
        "tags": [{"name": t} for t in tags],
        "paths": paths,
        "components": {
            "schemas": {k: schemas[k] for k in sorted(done) if k in schemas},
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "description": "Session or API key"}},
        },
        "security": [{"bearer": []}],
    }
    return doc


METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def check_openapi(doc: dict) -> list[str]:
    """Problems with an OpenAPI 3.x document's structure ([] when sound)."""
    problems = []
    if not re.fullmatch(r"3\.[01]\.\d+", str(doc.get("openapi", ""))):
        problems.append("openapi must be 3.0.x or 3.1.x")
    info = doc.get("info") or {}
    if not info.get("title") or not info.get("version"):
        problems.append("info needs title and version")
    schemas = (doc.get("components") or {}).get("schemas") or {}
    used: set[str] = set()
    _refs(doc, used)
    problems += [f"$ref to missing schema {n}" for n in sorted(used - set(schemas))]
    op_ids: set[str] = set()
    for path, item in (doc.get("paths") or {}).items():
        if not path.startswith("/"):
            problems.append(f"path {path} must start with /")
        params_in_path = set(re.findall(r"\{([^}]+)\}", path))
        for method, op in item.items():
            if method == "parameters":
                continue
            if method not in METHODS:
                problems.append(f"{path}: unknown method {method}")
                continue
            if not op.get("responses"):
                problems.append(f"{method.upper()} {path}: no responses")
            oid = op.get("operationId")
            if oid in op_ids:
                problems.append(f"duplicate operationId {oid}")
            op_ids.add(oid)
            declared = {
                p.get("name") for p in op.get("parameters", []) + item.get("parameters", []) if p.get("in") == "path"
            }
            if params_in_path - declared:
                problems.append(
                    f"{method.upper()} {path}: path parameters not declared {sorted(params_in_path - declared)}"
                )
    return problems


# ---- AsyncAPI -------------------------------------------------------------------------


def _event_message(t: str, cloud: bool) -> dict:
    name = ("ce_" if cloud else "") + re.sub(r"\W", "_", t)
    payload = {"$ref": "#/components/schemas/CloudEvent" if cloud else "#/components/schemas/ExaCaribEvent"}
    return {
        name: {
            "name": name,
            "title": f"{t} ({'CloudEvents 1.0' if cloud else 'ExaCarib JSON'})",
            "contentType": "application/cloudevents+json" if cloud else "application/json",
            "headers": {"$ref": "#/components/schemas/DeliveryHeaders"},
            "payload": payload,
            "examples": [
                {
                    "name": "example",
                    "summary": "Example data",
                    "payload": (
                        {
                            "specversion": "1.0",
                            "id": "8c1e…",
                            "source": "/commai/customers/{customer_id}",
                            "type": webhooks_std.CE_PREFIX + t,
                            "time": "2026-10-01T12:00:00Z",
                            "datacontenttype": "application/json",
                            "data": {},
                        }
                        if cloud
                        else {
                            "id": "8c1e…",
                            "type": t,
                            "created_at": "2026-10-01T12:00:00Z",
                            "customer_id": "…",
                            "subject": "",
                            "data": {},
                        }
                    ),
                }
            ],
        }
    }


def asyncapi(server: str = "") -> dict:
    types = sorted(events.TYPES)
    messages: dict = {}
    for t in types:
        messages.update(_event_message(t, False))
        messages.update(_event_message(t, True))
    messages["inbound"] = {
        "name": "inbound",
        "title": "A signed event sent to an inbound webhook",
        "contentType": "application/json",
        "headers": {"$ref": "#/components/schemas/InboundHeaders"},
        "payload": {"type": "object", "description": "Any JSON object, or a CloudEvent (structured or binary mode)."},
    }
    host = re.sub(r"^https?://", "", server.rstrip("/")) or "connect.exacarib.com"
    return {
        "asyncapi": "3.0.0",
        "info": {
            "title": "ExaCarib Connect: CommAI events",
            "version": "1.0.0",
            "description": "Events CommAI delivers to a business's webhook addresses, and the signed "
            "inbound webhooks it accepts. Each endpoint chooses ExaCarib JSON or CloudEvents 1.0; every "
            "delivery is signed with X-ExaCarib-Signature (v1=HMAC-SHA256 hex over 'timestamp.body') and, "
            "when switched on, the Standard Webhooks headers (webhook-id, webhook-timestamp, "
            "webhook-signature 'v1,<base64>' over 'id.timestamp.body', key from the whsec_ secret). "
            "Deliveries are retried with backoff; the event id is stable, so receivers can drop repeats.",
        },
        "servers": {
            "subscriber": {
                "host": "your-server.example",
                "protocol": "https",
                "description": "The business's own address, set per webhook endpoint (public HTTPS only).",
            },
            "commai": {"host": host, "protocol": "https", "pathname": "/api/v1/commai"},
        },
        "channels": {
            "events": {
                "address": None,
                "title": "Events out",
                "servers": [{"$ref": "#/servers/subscriber"}],
                "messages": {k: {"$ref": f"#/components/messages/{k}"} for k in messages if k != "inbound"},
            },
            "inboundHook": {
                "address": "/hooks/{token}",
                "title": "Events in (Zapier, Make, n8n, any system)",
                "servers": [{"$ref": "#/servers/commai"}],
                "parameters": {"token": {"description": "The inbound webhook's own token."}},
                "messages": {"inbound": {"$ref": "#/components/messages/inbound"}},
            },
        },
        "operations": {
            "deliverEvent": {
                "action": "send",
                "channel": {"$ref": "#/channels/events"},
                "summary": "CommAI POSTs each subscribed event to the endpoint's address.",
                "messages": [{"$ref": f"#/channels/events/messages/{k}"} for k in messages if k != "inbound"],
            },
            "receiveInbound": {
                "action": "receive",
                "channel": {"$ref": "#/channels/inboundHook"},
                "summary": "CommAI accepts a signed event once (by its id) and starts the business's workflows.",
                "messages": [{"$ref": "#/channels/inboundHook/messages/inbound"}],
            },
        },
        "components": {
            "messages": messages,
            "schemas": {
                "ExaCaribEvent": {
                    "type": "object",
                    "required": ["id", "type", "created_at", "customer_id", "data"],
                    "properties": {
                        "id": {"type": "string", "format": "uuid"},
                        "type": {"type": "string", "enum": types},
                        "created_at": {"type": "string", "format": "date-time"},
                        "customer_id": {"type": "string", "format": "uuid"},
                        "subject": {"type": "string"},
                        "data": {"type": "object"},
                    },
                },
                "CloudEvent": {
                    "type": "object",
                    "required": ["specversion", "id", "source", "type"],
                    "properties": {
                        "specversion": {"const": "1.0"},
                        "id": {"type": "string"},
                        "source": {"type": "string", "format": "uri-reference"},
                        "type": {"type": "string", "enum": [webhooks_std.CE_PREFIX + t for t in types]},
                        "subject": {"type": "string"},
                        "time": {"type": "string", "format": "date-time"},
                        "datacontenttype": {"const": "application/json"},
                        "data": {"type": "object"},
                    },
                },
                "DeliveryHeaders": {
                    "type": "object",
                    "required": ["X-ExaCarib-Event-Id", "X-ExaCarib-Timestamp", "X-ExaCarib-Signature"],
                    "properties": {
                        "X-ExaCarib-Event-Id": {"type": "string"},
                        "X-ExaCarib-Event-Type": {"type": "string"},
                        "X-ExaCarib-Timestamp": {"type": "string", "description": "Unix seconds"},
                        "X-ExaCarib-Signature": {"type": "string", "pattern": "^v1=[0-9a-f]{64}$"},
                        "webhook-id": {"type": "string"},
                        "webhook-timestamp": {"type": "string"},
                        "webhook-signature": {"type": "string", "pattern": "^v1,"},
                    },
                },
                "InboundHeaders": {
                    "type": "object",
                    "description": "Either the Standard Webhooks headers or the ExaCarib v1 pair; five minutes' "
                    "tolerance. CloudEvents binary mode adds ce-* headers.",
                    "properties": {
                        "webhook-id": {"type": "string"},
                        "webhook-timestamp": {"type": "string"},
                        "webhook-signature": {"type": "string"},
                        "X-ExaCarib-Timestamp": {"type": "string"},
                        "X-ExaCarib-Signature": {"type": "string"},
                    },
                },
            },
        },
    }


def check_asyncapi(doc: dict) -> list[str]:
    """Problems with the AsyncAPI document's structure ([] when sound)."""
    problems = []
    if doc.get("asyncapi") != "3.0.0":
        problems.append("asyncapi must be 3.0.0")

    def resolve(ref: str) -> Any:
        node: Any = doc
        for part in ref.removeprefix("#/").split("/"):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            r = node.get("$ref")
            if isinstance(r, str) and resolve(r) is None:
                problems.append(f"$ref points nowhere: {r}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    for name, op in (doc.get("operations") or {}).items():
        if op.get("action") not in ("send", "receive"):
            problems.append(f"operation {name}: action must be send or receive")
    return problems
