"""Reading a business's OpenAPI 3 document (ADR 0034).

Only reads: nothing in the document is ever run. We list the operations it
describes (method, path, parameters, body fields, security) so a business can
choose some as actions. An action can only name an operation that is in the
document, so Jibsy never invents an endpoint.

Supported: OpenAPI 3.0.x and 3.1.x, as JSON (or YAML when PyYAML is
installed). Local references (``#/components/...``) are followed; remote
references are refused, because following them would fetch from addresses
the document chooses.
"""

from __future__ import annotations

import json
import re
from typing import Any

METHODS = ("get", "post", "put", "patch", "delete")
KIND = {"get": "read", "post": "create", "put": "update", "patch": "update", "delete": "delete"}
MAX_BYTES = 2_000_000
MAX_OPERATIONS = 500


class OpenAPIError(ValueError):
    pass


def parse(text: str | bytes | dict) -> dict:
    """The document as a dict, checked to be OpenAPI 3.0 or 3.1 with paths."""
    if isinstance(text, dict):
        doc = text
    else:
        raw = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
        if len(raw.encode()) > MAX_BYTES:
            raise OpenAPIError("The document is larger than 2 MB.")
        try:
            doc = json.loads(raw)
        except ValueError:
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError:
                raise OpenAPIError("Paste the document as JSON (YAML needs PyYAML on the server).") from None
            try:
                doc = yaml.safe_load(raw)  # safe_load: no Python objects, no code
            except yaml.YAMLError as e:
                raise OpenAPIError(f"The YAML could not be read: {e}") from None
    if not isinstance(doc, dict):
        raise OpenAPIError("That is not an OpenAPI document.")
    version = str(doc.get("openapi", ""))
    if not re.fullmatch(r"3\.[01]\.\d+(-.+)?", version):
        if "swagger" in doc:
            raise OpenAPIError("That is a Swagger 2.0 document; convert it to OpenAPI 3 first.")
        raise OpenAPIError("Only OpenAPI 3.0 and 3.1 documents can be imported.")
    if not isinstance(doc.get("info"), dict) or not doc["info"].get("title"):
        raise OpenAPIError("The document has no info.title.")
    if not isinstance(doc.get("paths"), dict) or not doc["paths"]:
        raise OpenAPIError("The document describes no paths.")
    return doc


def resolve(doc: dict, node: Any, depth: int = 0) -> Any:
    """Follow a local $ref (at most 20 deep)."""
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        ref = str(node["$ref"])
        if not ref.startswith("#/"):
            raise OpenAPIError(f"Only references inside the document are followed ({ref[:80]}).")
        seen += 1
        if seen + depth > 20:
            raise OpenAPIError("References go round in a loop.")
        target: Any = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise OpenAPIError(f"The reference {ref[:80]} points nowhere.")
            target = target[part]
        node = target
    return node


def _type(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):  # 3.1: ["string", "null"]
        t = next((x for x in t if x != "null"), "string")
    fmt = schema.get("format", "")
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t in ("object", "array"):
        return "json"
    if fmt == "email":
        return "email"
    if fmt in ("date-time", "date"):
        return "datetime"
    return "string"


def _slug(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:60] or "operation"


def operations(doc: dict) -> list[dict]:
    """Every operation as {"id", "method", "path", "summary", "kind", "params",
    "body", "security"}. Each param: {"name", "in", "required", "type", "label"}.
    ``body`` lists the top-level JSON body properties the same way."""
    out: list[dict] = []
    seen: set[str] = set()
    for path, item in doc["paths"].items():
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        item = resolve(doc, item)
        shared = [resolve(doc, p) for p in item.get("parameters") or []]
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            oid = _slug(str(op.get("operationId") or f"{method}_{path}"))
            base, n = oid, 2
            while oid in seen:
                oid, n = f"{base}_{n}", n + 1
            seen.add(oid)
            params: dict[tuple[str, str], dict] = {}
            for p in shared + [resolve(doc, p) for p in op.get("parameters") or []]:
                if not isinstance(p, dict) or p.get("in") not in ("path", "query", "header"):
                    continue
                if p.get("in") == "header" and str(p.get("name", "")).lower() in ("authorization", "content-type"):
                    continue
                schema = resolve(doc, p.get("schema") or {})
                params[(p["in"], p["name"])] = {
                    "name": str(p["name"]),
                    "in": p["in"],
                    "required": bool(p.get("required")) or p["in"] == "path",
                    "type": _type(schema if isinstance(schema, dict) else {}),
                    "label": str(p.get("description") or p["name"])[:80],
                }
            body: list[dict] = []
            rb = resolve(doc, op.get("requestBody") or {})
            content = (rb.get("content") or {}) if isinstance(rb, dict) else {}
            media = next((m for m in content if m == "application/json" or m.endswith("+json")), None)
            if media:
                schema = resolve(doc, (content[media] or {}).get("schema") or {})
                if isinstance(schema, dict) and "allOf" in schema:
                    merged: dict = {"properties": {}, "required": []}
                    for part in schema["allOf"]:
                        part = resolve(doc, part)
                        merged["properties"].update(part.get("properties") or {})
                        merged["required"] += part.get("required") or []
                    schema = merged
                req = set((schema or {}).get("required") or [])
                for name, prop in ((schema or {}).get("properties") or {}).items():
                    prop = resolve(doc, prop)
                    if isinstance(prop, dict) and prop.get("readOnly"):
                        continue
                    body.append(
                        {
                            "name": str(name),
                            "in": "body",
                            "required": name in req,
                            "type": _type(prop if isinstance(prop, dict) else {}),
                            "label": str((prop or {}).get("description") or name)[:80],
                        }
                    )
            out.append(
                {
                    "id": oid,
                    "method": method.upper(),
                    "path": path,
                    "summary": str(op.get("summary") or op.get("description") or f"{method.upper()} {path}")[:120],
                    "kind": KIND[method],
                    "params": list(params.values()),
                    "body": body,
                    "body_media": media or "",
                    "security": op.get("security", doc.get("security")) or [],
                    "deprecated": bool(op.get("deprecated")),
                }
            )
            if len(out) >= MAX_OPERATIONS:
                return out
    return out


def servers(doc: dict) -> list[str]:
    """Server URLs with their variables' defaults filled in."""
    out = []
    for s in doc.get("servers") or []:
        if not isinstance(s, dict) or not s.get("url"):
            continue
        url = str(s["url"])
        for name, var in (s.get("variables") or {}).items():
            url = url.replace("{" + name + "}", str((var or {}).get("default", "")))
        out.append(url)
    return out


def security_schemes(doc: dict) -> list[dict]:
    """The schemes the document declares that Jibsy can sign in with."""
    out = []
    for name, s in ((doc.get("components") or {}).get("securitySchemes") or {}).items():
        s = resolve(doc, s)
        t = s.get("type")
        if t == "apiKey" and s.get("in") in ("header", "query"):
            out.append({"scheme": name, "type": "api_key", "in": s["in"], "name": s.get("name", "")})
        elif t == "http" and str(s.get("scheme", "")).lower() in ("basic", "bearer"):
            out.append({"scheme": name, "type": str(s["scheme"]).lower()})
        elif t == "oauth2":
            flows = s.get("flows") or {}
            if "clientCredentials" in flows:
                f = flows["clientCredentials"]
                out.append(
                    {
                        "scheme": name,
                        "type": "oauth2_client_credentials",
                        "token_url": f.get("tokenUrl", ""),
                        "scopes": sorted(f.get("scopes") or {}),
                    }
                )
            if "authorizationCode" in flows:
                f = flows["authorizationCode"]
                out.append(
                    {
                        "scheme": name,
                        "type": "oauth2_auth_code",
                        "authorize_url": f.get("authorizationUrl", ""),
                        "token_url": f.get("tokenUrl", ""),
                        "scopes": sorted(f.get("scopes") or {}),
                    }
                )
    return out


# Words in parameter names that say which Jibsy field fits.
HINTS = {
    "email": ("email", "e_mail", "mail"),
    "phone": ("phone", "mobile", "tel", "msisdn"),
    "name": ("name", "full_name", "fullname", "display_name"),
    "subject": ("subject", "title", "summary"),
    "notes": ("notes", "note", "description", "body", "message", "comment"),
    "amount": ("amount", "total", "value", "price"),
    "starts_at": ("start", "starts_at", "start_time", "datetime", "date"),
    "reference": ("reference", "ref", "external_id", "order_id", "id"),
}


def suggest_mapping(op: dict) -> dict[str, str]:
    """Jibsy field -> the operation's parameter that most likely holds it."""
    names = {f["name"]: _slug(f["name"]) for f in op["params"] + op["body"]}
    out: dict[str, str] = {}
    for field, words in HINTS.items():
        for w in words:
            hit = next((n for n, s in names.items() if s == w), None) or next(
                (n for n, s in names.items() if s.endswith("_" + w) or s.startswith(w + "_")), None
            )
            if hit and hit not in out.values():
                out[field] = hit
                break
    return out


def draft_actions(ops: list[dict], limit: int = 8) -> list[dict]:
    """A first draft for a person to review: the document's non-deprecated
    operations, reads first, each with suggested field mapping. Only operations
    in the document can appear."""
    pick = [o for o in ops if not o["deprecated"]]
    pick.sort(key=lambda o: (o["kind"] != "read", o["kind"] == "delete", o["path"]))
    return [
        {
            "operation": o["id"],
            "name": o["id"],
            "label": o["summary"][:80],
            "sensitive": o["kind"] == "delete",
            "mapping": suggest_mapping(o),
        }
        for o in pick[:limit]
    ]
