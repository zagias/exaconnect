"""Real-shaped SCIM request sequences per provider (ADR 0030), as each one
sends them: create, look up, update, group membership, switch off. Used by
the connection test (a dry run rolled back) and by the tests through the
real SCIM endpoints. {email}, {user_id} and {group_id} are filled in.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import psycopg

from .. import scim
from . import quirks

USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _patch(*ops: dict) -> dict:
    return {"schemas": [PATCH], "Operations": list(ops)}


ENTRA = [
    ("GET", '/Users?filter=userName eq "{email}"', None),
    (
        "POST",
        "/Users",
        {
            "schemas": [USER, ENTERPRISE],
            "externalId": "ana.lee",
            "userName": "{email}",
            "active": True,
            "displayName": "Ana Lee",
            "emails": [{"primary": True, "type": "work", "value": "{email}"}],
            "meta": {"resourceType": "User"},
            "name": {"formatted": "Ana Lee", "familyName": "Lee", "givenName": "Ana"},
            ENTERPRISE: {"department": "Customer Care", "employeeNumber": "1001"},
        },
    ),
    (
        "PATCH",
        "/Users/{user_id}",
        _patch(
            {"op": "Replace", "path": "displayName", "value": "Ana M. Lee"},
            {"op": "Replace", "path": "name.givenName", "value": "Ana M."},
            {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "{email}"},
            {"op": "Add", "path": f"{ENTERPRISE}:department", "value": "Sales"},
        ),
    ),
    (
        "POST",
        "/Groups",
        {
            "schemas": [GROUP],
            "externalId": "8aa1a5c0-0000-4000-8000-000000000001",
            "displayName": "ExaCarib Agents",
            "meta": {"resourceType": "Group"},
        },
    ),
    ("PATCH", "/Groups/{group_id}", _patch({"op": "Add", "path": "members", "value": [{"value": "{user_id}"}]})),
    ("PATCH", "/Groups/{group_id}", _patch({"op": "Remove", "path": "members", "value": [{"value": "{user_id}"}]})),
    ("PATCH", "/Groups/{group_id}", _patch({"op": "Add", "path": "members", "value": [{"value": "{user_id}"}]})),
    ("PATCH", "/Groups/{group_id}", _patch({"op": "Remove", "path": 'members[value eq "{user_id}"]'})),
    ("PATCH", "/Users/{user_id}", _patch({"op": "Replace", "path": "active", "value": "False"})),
]

OKTA = [
    ("GET", '/Users?filter=userName eq "{email}"&startIndex=1&count=100', None),
    (
        "POST",
        "/Users",
        {
            "schemas": [USER],
            "userName": "{email}",
            "name": {"givenName": "Ana", "familyName": "Lee"},
            "emails": [{"primary": True, "value": "{email}", "type": "work"}],
            "displayName": "Ana Lee",
            "locale": "en-US",
            "externalId": "00u1abcdEFGH2345ijk6",
            "groups": [],
            "password": "{password}",
            "active": True,
        },
    ),
    (
        "PUT",
        "/Users/{user_id}",
        {
            "schemas": [USER],
            "id": "{user_id}",
            "userName": "{email}",
            "name": {"givenName": "Ana", "familyName": "Lee-Brown"},
            "emails": [{"primary": True, "value": "{email}", "type": "work"}],
            "displayName": "Ana Lee-Brown",
            "active": True,
        },
    ),
    ("POST", "/Groups", {"schemas": [GROUP], "displayName": "ExaCarib Agents", "members": []}),
    (
        "PATCH",
        "/Groups/{group_id}",
        _patch({"op": "replace", "value": {"id": "{group_id}", "displayName": "ExaCarib Support"}}),
    ),
    (
        "PATCH",
        "/Groups/{group_id}",
        _patch({"op": "add", "path": "members", "value": [{"value": "{user_id}", "display": "{email}"}]}),
    ),
    (
        "PATCH",
        "/Groups/{group_id}",
        _patch({"op": "remove", "path": "members", "value": [{"value": "{user_id}", "display": "{email}"}]}),
    ),
    ("PATCH", "/Users/{user_id}", _patch({"op": "replace", "value": {"active": False}})),
]

JUMPCLOUD = [
    ("GET", '/Users?filter=userName eq "test.connection@{domain}"', None),
    (
        "POST",
        "/Users",
        {
            "schemas": [USER],
            "userName": "{email}",
            "externalId": "5f1e2d3c4b5a69788796a5b4",
            "name": {"givenName": "Ana", "familyName": "Lee"},
            "displayName": "Ana Lee",
            "emails": [{"value": "{email}", "type": "work", "primary": True}],
            "active": True,
        },
    ),
    ("POST", "/Groups", {"schemas": [GROUP], "displayName": "ExaCarib Agents"}),
    ("PATCH", "/Groups/{group_id}", _patch({"op": "add", "path": "members", "value": [{"value": "{user_id}"}]})),
    ("PATCH", "/Users/{user_id}", _patch({"op": "replace", "path": "active", "value": False})),
]

ONELOGIN = [
    ("GET", '/Users?filter=userName eq "{email}"', None),
    (
        "POST",
        "/Users",
        {
            "schemas": [USER],
            "userName": "{email}",
            "name": {"givenName": "Ana", "familyName": "Lee"},
            "emails": [{"value": "{email}", "primary": True}],
            "displayName": "Ana Lee",
            "externalId": "123456789",
            "active": True,
        },
    ),
    (
        "PUT",
        "/Users/{user_id}",
        {
            "schemas": [USER],
            "userName": "{email}",
            "name": {"givenName": "Ana", "familyName": "Lee"},
            "emails": [{"value": "{email}", "primary": True}],
            "displayName": "Ana Lee",
            "externalId": "123456789",
            "active": True,
        },
    ),
    ("PATCH", "/Users/{user_id}", _patch({"op": "replace", "path": "displayName", "value": [{"value": "Ana J. Lee"}]})),
    ("POST", "/Groups", {"schemas": [GROUP], "displayName": "ExaCarib Agents", "members": [{"value": "{user_id}"}]}),
    ("PATCH", "/Groups/{group_id}", _patch({"op": "remove", "path": "members", "value": {"value": "{user_id}"}})),
    ("PATCH", "/Users/{user_id}", _patch({"op": "replace", "path": "active", "value": False})),
]

PING = [
    ("GET", '/Users?filter=userName Eq "{email}"', None),
    (
        "POST",
        "/Users",
        {
            "schemas": [USER],
            "userName": "{email}",
            "name": {"givenName": "Ana", "familyName": "Lee"},
            "emails": [{"value": "{email}", "type": "work", "primary": True}],
            "active": True,
            "externalId": "44444444-4444-4444-4444-444444444444",
        },
    ),
    (
        "PATCH",
        "/Users/{user_id}",
        _patch(
            {"op": "replace", "path": f"{USER}:name.familyName", "value": "Lee-Brown"},
            {"op": "replace", "value": {USER: {"displayName": "Ana Lee-Brown"}}},
        ),
    ),
    ("POST", "/Groups", {"schemas": [GROUP], "displayName": "ExaCarib Agents"}),
    ("PATCH", "/Groups/{group_id}", _patch({"op": "add", "path": "members", "value": [{"value": "{user_id}"}]})),
    ("PATCH", "/Users/{user_id}", _patch({"op": "replace", "path": f"{USER}:active", "value": False})),
]

FIXTURES: dict[str, list] = {"entra": ENTRA, "okta": OKTA, "jumpcloud": JUMPCLOUD, "onelogin": ONELOGIN, "ping": PING}


def _fill(obj: Any, values: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, list):
        return [_fill(o, values) for o in obj]
    if isinstance(obj, dict):
        return {_fill(k, values): _fill(v, values) for k, v in obj.items()}
    return obj


def requests_for(provider: str, values: dict[str, str]) -> list[tuple[str, str, Any]]:
    return [(m, _fill(p, values), _fill(copy.deepcopy(b), values)) for m, p, b in FIXTURES.get(provider, [])]


_FILTER = re.compile(r'^\s*(userName|externalId|displayName|emails\.value|id)\s+eq\s+"[^"]*"\s*$')


class _DryRun(Exception):
    pass


def dry_run(conn: psycopg.Connection, provider: str, customer_id: Any, domain: str) -> dict:
    """Run the provider's request sequence through the SCIM code in a savepoint
    that is always rolled back. Says which request failed and why."""
    import secrets as _s

    seq = FIXTURES.get(provider)
    if not seq:
        return {"check": "SCIM requests", "ok": True, "detail": "This provider does not send SCIM."}
    values = {
        "email": f"scim-test-{_s.token_hex(4)}@{domain or 'example.invalid'}",
        "domain": domain or "example.invalid",
        "password": _s.token_urlsafe(12),
    }
    step = ""
    outcome: dict = {}
    try:
        with conn.transaction():
            user = group = None
            for i, (method, path, body) in enumerate(seq, 1):
                path = _fill(
                    path,
                    {
                        **values,
                        "user_id": str(user["id"]) if user else "",
                        "group_id": str(group["id"]) if group else "",
                    },
                )
                body = _fill(
                    copy.deepcopy(body),
                    {
                        **values,
                        "user_id": str(user["id"]) if user else "",
                        "group_id": str(group["id"]) if group else "",
                    },
                )
                step = f"request {i} ({method} {path.split('?')[0]})"
                if method == "GET":
                    f = quirks.normalise_filter(re.search(r"filter=([^&]+)", path).group(1))
                    if not _FILTER.match(f or ""):
                        raise scim.ScimError(400, f"Filter not understood: {f}")
                elif path == "/Users" and method == "POST":
                    user, _ = scim.create_user(conn, customer_id, body)
                elif path.startswith("/Users/") and method == "PUT":
                    user = scim.replace_user(conn, customer_id, str(user["id"]), body)
                elif path.startswith("/Users/") and method == "PATCH":
                    user = scim.patch_user(conn, customer_id, str(user["id"]), quirks.normalise_ops(body["Operations"]))
                elif path == "/Groups" and method == "POST":
                    group = scim.create_group(conn, customer_id, body)
                elif path.startswith("/Groups/") and method == "PATCH":
                    group = scim.patch_group(conn, customer_id, group, quirks.normalise_ops(body["Operations"]))
            ended = user is not None and user["disabled_at"] is not None
            outcome = {
                "check": "SCIM requests",
                "ok": ended,
                "detail": f"{len(seq)} requests shaped as this provider sends them all worked; switching someone "
                "off ends their sessions. Nothing was kept."
                if ended
                else "The requests ran but the last one did not switch the person off.",
            }
            raise _DryRun
    except _DryRun:
        return outcome
    except scim.ScimError as e:
        return {"check": "SCIM requests", "ok": False, "detail": f"{step} was refused: {e.detail}"}
