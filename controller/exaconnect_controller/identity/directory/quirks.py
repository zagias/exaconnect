"""SCIM request shapes real directories send, brought to the one shape
`identity.scim` understands (ADR 0036). Provider-agnostic: a quirk found in
one provider is handled for all of them.

Handled here:
- operation names in any case ('Replace', 'ADD');
- paths prefixed with the core schema URN
  (urn:ietf:params:scim:schemas:core:2.0:User:active);
- no-path values keyed by the core schema URN or by URN-prefixed attributes;
- single values wrapped in a list ([{"value": "Ana"}]);
- a members value that is one object instead of a list;
- filter operators in any case (userName Eq "x"), as RFC 7644 allows.

Already handled by `identity.scim` itself (and tested per provider):
"True"/"False" strings for active, members[value eq "id"] paths,
emails[type eq "work"].value paths, and {id, displayName} replace values.
"""

from __future__ import annotations

import re
from typing import Any

CORE_PREFIXES = (
    "urn:ietf:params:scim:schemas:core:2.0:User:",
    "urn:ietf:params:scim:schemas:core:2.0:Group:",
)
CORE_SCHEMAS = ("urn:ietf:params:scim:schemas:core:2.0:User", "urn:ietf:params:scim:schemas:core:2.0:Group")
SINGLE = {"active", "displayName", "externalId", "userName", "name.givenName", "name.familyName"}


def _strip(path: str) -> str:
    for p in CORE_PREFIXES:
        if path.lower().startswith(p.lower()):
            return path[len(p) :]
    return path


def _unwrap(path: str, value: Any) -> Any:
    if path in SINGLE and isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        if "value" in value[0]:
            return value[0]["value"]
    return value


def _flatten(value: dict) -> dict:
    out: dict = {}
    for k, v in value.items():
        if k in CORE_SCHEMAS and isinstance(v, dict):
            out.update(_flatten(v))
            continue
        key = _strip(k)
        out[key] = _unwrap(key, v)
    return out


def normalise_ops(ops: list[Any]) -> list[dict]:
    out = []
    for op in ops:
        if not isinstance(op, dict):
            out.append(op)
            continue
        op = dict(op)
        if isinstance(op.get("op"), str):
            op["op"] = op["op"].strip().lower()
        path = op.get("path")
        value = op.get("value")
        if isinstance(path, str) and path.strip():
            path = _strip(path.strip())
            op["path"] = path
            value = _unwrap(path, value)
            if path == "members" and isinstance(value, dict):
                value = [value]
        elif isinstance(value, dict):
            value = _flatten(value)
            if isinstance(value.get("members"), dict):
                value["members"] = [value["members"]]
        if "value" in op:
            op["value"] = value
        out.append(op)
    return out


_OP = re.compile(r'^(\s*)([A-Za-z.:0-9]+)(\s+)(eq)(\s+")', re.I)


def normalise_filter(expr: str | None) -> str | None:
    """`userName Eq "x"` -> `userName eq "x"`; core-URN-prefixed attribute names stripped."""
    if not expr:
        return expr
    m = _OP.match(expr)
    if not m:
        return expr
    return f"{m.group(1)}{_strip(m.group(2))}{m.group(3)}eq{m.group(5)}{expr[m.end() :]}"
