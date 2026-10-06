"""SCIM 2.0 user provisioning (RFC 7643/7644) for one business at a time.

People created here are 'customer' accounts of the token's business with an
unusable password: they sign in through the business's SSO. Their rights come
from their directory groups:

- every group maps to a CommAI team (made on first sight) and a seat
  ('agent' replies to customers, 'internal' reads and writes notes only);
- a group can be asked to grant business-admin rights (the full customer
  account: Connect, CommAI settings, sign-in set-up). That stays pending until
  a different business admin, or an ExaCarib admin, approves it.

Without an approved admin group a provisioned person has MEMBER_SCOPES only.
People added by hand before SCIM (provisioned_by NULL) keep their rights."""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from . import sessions, sso

USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
MAX_PAGE = 200
TOKEN_PREFIX = "scim_"


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type

    def body(self) -> dict:
        out = {"schemas": [ERROR], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            out["scimType"] = self.scim_type
        return out


def _iso(t: dt.datetime | None) -> str | None:
    return t.isoformat() if t else None


# ---- rights -------------------------------------------------------------------


def refresh_rights(conn: psycopg.Connection, customer_id: Any, user_id: Any) -> None:
    """Recompute seat, teams and scopes of a provisioned person from their groups."""
    u = conn.execute("SELECT provisioned_by FROM users WHERE id = %s", (user_id,)).fetchone()
    groups = conn.execute(
        """SELECT g.* FROM scim_group_members m JOIN scim_groups g ON g.id = m.group_id
           WHERE m.user_id = %s AND g.customer_id = %s""",
        (user_id, customer_id),
    ).fetchall()
    # Team membership follows groups for everyone in the directory.
    for g in groups:
        if g["team_id"]:
            conn.execute(
                "INSERT INTO commai_team_members (team_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (g["team_id"], user_id),
            )
    if u is None or u["provisioned_by"] is None:
        return
    admin = any(g["admin_requested"] and g["admin_approved_at"] for g in groups)
    seat = "agent" if not groups or any(g["seat"] == "agent" for g in groups) else "internal"
    conn.execute(
        "UPDATE users SET access_scopes = %s, updated_at = now() WHERE id = %s",
        (None if admin else sso.MEMBER_SCOPES, user_id),
    )
    conn.execute(
        """INSERT INTO commai_members (customer_id, user_id, seat) VALUES (%s, %s, %s)
           ON CONFLICT (customer_id, user_id) DO UPDATE SET seat = EXCLUDED.seat""",
        (customer_id, user_id, seat),
    )


def _leave_team(conn: psycopg.Connection, group: dict, user_id: Any) -> None:
    """Drop the group's team membership unless another of their groups maps to the same team."""
    if not group["team_id"]:
        return
    other = conn.execute(
        """SELECT 1 FROM scim_group_members m JOIN scim_groups g ON g.id = m.group_id
           WHERE m.user_id = %s AND g.team_id = %s AND g.id <> %s""",
        (user_id, group["team_id"], group["id"]),
    ).fetchone()
    if other is None:
        conn.execute("DELETE FROM commai_team_members WHERE team_id = %s AND user_id = %s", (group["team_id"], user_id))


def switch_off(conn: psycopg.Connection, user_id: Any) -> None:
    """Deactivate at once: no sign-in, every session ended, every API key revoked."""
    conn.execute(
        "UPDATE users SET disabled_at = coalesce(disabled_at, now()), updated_at = now() WHERE id = %s", (user_id,)
    )
    sessions.end_all(conn, user_id, revoke_keys=True)


# ---- users ----------------------------------------------------------------------


def user_resource(conn: psycopg.Connection, row: dict, base: str) -> dict:
    groups = conn.execute(
        """SELECT g.id, g.display_name FROM scim_group_members m JOIN scim_groups g ON g.id = m.group_id
           WHERE m.user_id = %s ORDER BY g.display_name""",
        (row["id"],),
    ).fetchall()
    formatted = " ".join(p for p in (row["given_name"], row["family_name"]) if p)
    out = {
        "schemas": [USER],
        "id": str(row["id"]),
        "userName": row["email"],
        "active": row["disabled_at"] is None,
        "displayName": row["display_name"] or formatted or row["email"],
        "name": {"givenName": row["given_name"], "familyName": row["family_name"], "formatted": formatted},
        "emails": [{"value": row["email"], "type": "work", "primary": True}],
        "groups": [{"value": str(g["id"]), "display": g["display_name"]} for g in groups],
        "meta": {
            "resourceType": "User",
            "created": _iso(row["created_at"]),
            "lastModified": _iso(row["updated_at"]),
            "location": f"{base}/Users/{row['id']}",
        },
    }
    if row["scim_external_id"]:
        out["externalId"] = row["scim_external_id"]
    return out


def _bool(v: Any) -> bool:
    if isinstance(v, str):  # Entra ID sends "False"/"True"
        return v.strip().lower() == "true"
    return bool(v)


def _email_from(body: dict) -> str:
    name = str(body.get("userName") or "").strip().lower()
    if "@" not in name:
        emails = body.get("emails") or []
        prim = next((e for e in emails if e.get("primary")), emails[0] if emails else {})
        name = str(prim.get("value") or "").strip().lower()
    if "@" not in name or len(name) > 255 or " " in name:
        raise ScimError(400, "userName must be an email address.", "invalidValue")
    return name


def get_user(conn: psycopg.Connection, customer_id: Any, user_id: str) -> dict:
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id = %s::uuid AND customer_id = %s AND scim_deleted_at IS NULL",
            (user_id, customer_id),
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None:
        raise ScimError(404, "User not found.")
    return row


def _apply_user_fields(conn: psycopg.Connection, row: dict, body: dict, actor: str) -> dict:
    name = body.get("name") or {}
    fields = {
        "display_name": str((body["displayName"] if "displayName" in body else row["display_name"]) or "")[:200],
        "given_name": str(name.get("givenName", row["given_name"]) or "")[:100],
        "family_name": str(name.get("familyName", row["family_name"]) or "")[:100],
        "scim_external_id": body.get("externalId", row["scim_external_id"]),
    }
    if "userName" in body or "emails" in body:
        email = _email_from({**{"userName": row["email"]}, **body})
        if email != row["email"]:
            clash = conn.execute("SELECT 1 FROM users WHERE lower(email) = %s AND id <> %s", (email, row["id"]))
            if clash.fetchone():
                raise ScimError(409, "Another account already uses that userName.", "uniqueness")
            fields["email"] = email
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    row = conn.execute(
        f"UPDATE users SET {sets}, updated_at = now() WHERE id = %(id)s RETURNING *", {**fields, "id": row["id"]}
    ).fetchone()
    if "active" in body:
        row = set_active(conn, row, _bool(body["active"]))
    return row


def set_active(conn: psycopg.Connection, row: dict, active: bool) -> dict:
    if not active and row["disabled_at"] is None:
        switch_off(conn, row["id"])
    elif active and row["disabled_at"] is not None:
        conn.execute("UPDATE users SET disabled_at = NULL, updated_at = now() WHERE id = %s", (row["id"],))
    return conn.execute("SELECT * FROM users WHERE id = %s", (row["id"],)).fetchone()


def create_user(conn: psycopg.Connection, customer_id: Any, body: dict) -> tuple[dict, bool]:
    """(row, created). An existing hand-made account of the same business is adopted, not duplicated."""
    email = _email_from(body)
    existing = conn.execute("SELECT * FROM users WHERE lower(email) = %s", (email,)).fetchone()
    if existing is not None:
        linked = existing["scim_linked"] and existing["scim_deleted_at"] is None
        if existing["role"] != "customer" or str(existing["customer_id"]) != str(customer_id) or linked:
            raise ScimError(409, "A user with that userName already exists.", "uniqueness")
        was_deleted = existing["scim_deleted_at"] is not None
        conn.execute("UPDATE users SET scim_deleted_at = NULL, scim_linked = true WHERE id = %s", (existing["id"],))
        fields = {k: v for k, v in body.items() if k not in ("userName", "emails")}
        if was_deleted:
            fields.setdefault("active", True)
        row = _apply_user_fields(conn, {**existing, "scim_deleted_at": None}, fields, "scim")
        return row, False
    row = conn.execute(
        """INSERT INTO users (email, password_hash, role, customer_id, access_scopes, provisioned_by, scim_linked)
           VALUES (%s, %s, 'customer', %s, %s, 'scim', true) RETURNING *""",
        (email, sso.UNUSABLE_PASSWORD, customer_id, sso.MEMBER_SCOPES),
    ).fetchone()
    conn.execute(
        "INSERT INTO commai_members (customer_id, user_id, seat) VALUES (%s, %s, 'agent') ON CONFLICT DO NOTHING",
        (customer_id, row["id"]),
    )
    row = _apply_user_fields(conn, row, {k: v for k, v in body.items() if k not in ("userName", "emails")}, "scim")
    return row, True


def replace_user(conn: psycopg.Connection, customer_id: Any, user_id: str, body: dict) -> dict:
    row = get_user(conn, customer_id, user_id)
    name = body.get("name") or {}
    body = {
        "displayName": "",
        "externalId": None,
        **body,
        "name": {"givenName": name.get("givenName", ""), "familyName": name.get("familyName", "")},
    }
    return _apply_user_fields(conn, row, body, "scim")


def patch_user(conn: psycopg.Connection, customer_id: Any, user_id: str, ops: list[dict]) -> dict:
    row = get_user(conn, customer_id, user_id)
    for op in ops:
        kind = str(op.get("op", "")).lower()
        path = op.get("path")
        value = op.get("value")
        if kind not in ("add", "replace", "remove"):
            raise ScimError(400, f"Unsupported op {op.get('op')!r}.", "invalidSyntax")
        if not path:
            if not isinstance(value, dict):
                raise ScimError(400, "A patch without a path needs an object value.", "invalidValue")
            update = {}
            for k, v in value.items():
                if k.startswith("name."):
                    update.setdefault("name", {})[k[5:]] = v
                else:
                    update[k] = v
            row = _apply_user_fields(conn, row, update, "scim")
            continue
        p = str(path)
        if kind == "remove":
            value = "" if p != "active" else False
        if p == "active":
            row = set_active(conn, row, _bool(value))
        elif p in ("displayName", "externalId", "userName"):
            row = _apply_user_fields(conn, row, {p: value}, "scim")
        elif p in ("name.givenName", "name.familyName"):
            row = _apply_user_fields(conn, row, {"name": {p[5:]: value}}, "scim")
        elif p == "name" and isinstance(value, dict):
            row = _apply_user_fields(conn, row, {"name": value}, "scim")
        elif p.startswith("emails"):
            vals = value if isinstance(value, list) else [{"value": value, "primary": True}]
            if kind != "remove" and vals:
                row = _apply_user_fields(conn, row, {"emails": vals}, "scim")
        else:
            # Attributes we don't keep (title, phone numbers...) are accepted and ignored.
            continue
    return row


def delete_user(conn: psycopg.Connection, customer_id: Any, user_id: str) -> None:
    """Deprovision. The account is switched off and hidden from SCIM, but kept, so
    the audit trail and conversation history still name who did what."""
    row = get_user(conn, customer_id, user_id)
    switch_off(conn, row["id"])
    conn.execute("UPDATE users SET scim_deleted_at = now() WHERE id = %s", (row["id"],))
    conn.execute("DELETE FROM scim_group_members WHERE user_id = %s", (row["id"],))


# ---- groups -----------------------------------------------------------------------


def group_resource(conn: psycopg.Connection, g: dict, base: str) -> dict:
    members = conn.execute(
        """SELECT u.id, u.email FROM scim_group_members m JOIN users u ON u.id = m.user_id
           WHERE m.group_id = %s ORDER BY u.email""",
        (g["id"],),
    ).fetchall()
    out = {
        "schemas": [GROUP],
        "id": str(g["id"]),
        "displayName": g["display_name"],
        "members": [{"value": str(m["id"]), "display": m["email"], "type": "User"} for m in members],
        "meta": {
            "resourceType": "Group",
            "created": _iso(g["created_at"]),
            "lastModified": _iso(g["updated_at"]),
            "location": f"{base}/Groups/{g['id']}",
        },
    }
    if g["external_id"]:
        out["externalId"] = g["external_id"]
    return out


def get_group(conn: psycopg.Connection, customer_id: Any, group_id: str) -> dict:
    try:
        g = conn.execute(
            "SELECT * FROM scim_groups WHERE id = %s::uuid AND customer_id = %s", (group_id, customer_id)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        g = None
    if g is None:
        raise ScimError(404, "Group not found.")
    return g


def _team_for(conn: psycopg.Connection, customer_id: Any, name: str) -> Any:
    row = conn.execute(
        """INSERT INTO commai_teams (customer_id, name) VALUES (%s, %s)
           ON CONFLICT (customer_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id""",
        (customer_id, name),
    ).fetchone()
    return row["id"]


def create_group(conn: psycopg.Connection, customer_id: Any, body: dict) -> dict:
    name = str(body.get("displayName") or "").strip()[:120]
    if not name:
        raise ScimError(400, "displayName is required.", "invalidValue")
    if conn.execute(
        "SELECT 1 FROM scim_groups WHERE customer_id = %s AND display_name = %s", (customer_id, name)
    ).fetchone():
        raise ScimError(409, "A group with that displayName already exists.", "uniqueness")
    g = conn.execute(
        """INSERT INTO scim_groups (customer_id, display_name, external_id, team_id)
           VALUES (%s, %s, %s, %s) RETURNING *""",
        (customer_id, name, body.get("externalId"), _team_for(conn, customer_id, name)),
    ).fetchone()
    set_members(conn, customer_id, g, [m.get("value") for m in body.get("members") or []])
    return g


def _member_ids(conn: psycopg.Connection, customer_id: Any, values: list) -> list:
    out = []
    for v in values:
        out.append(get_user(conn, customer_id, str(v))["id"])
    return out


def add_members(conn: psycopg.Connection, customer_id: Any, g: dict, values: list) -> None:
    for uid in _member_ids(conn, customer_id, values):
        conn.execute(
            "INSERT INTO scim_group_members (group_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (g["id"], uid)
        )
        refresh_rights(conn, customer_id, uid)


def remove_members(conn: psycopg.Connection, customer_id: Any, g: dict, values: list | None) -> None:
    if values is None:
        values = [
            r["user_id"]
            for r in conn.execute("SELECT user_id FROM scim_group_members WHERE group_id = %s", (g["id"],)).fetchall()
        ]
    for v in values:
        r = conn.execute(
            "DELETE FROM scim_group_members WHERE group_id = %s AND user_id::text = %s RETURNING user_id",
            (g["id"], str(v)),
        ).fetchone()
        if r:
            _leave_team(conn, g, r["user_id"])
            refresh_rights(conn, customer_id, r["user_id"])


def set_members(conn: psycopg.Connection, customer_id: Any, g: dict, values: list) -> None:
    want = {str(u) for u in _member_ids(conn, customer_id, values)}
    have = {
        str(r["user_id"])
        for r in conn.execute("SELECT user_id FROM scim_group_members WHERE group_id = %s", (g["id"],)).fetchall()
    }
    remove_members(conn, customer_id, g, sorted(have - want))
    add_members(conn, customer_id, g, sorted(want - have))


def _member_filter(path: str) -> str | None:
    # members[value eq "id"]
    import re

    m = re.match(r'^members\[\s*value\s+eq\s+"([^"]+)"\s*\]$', path.strip(), re.I)
    return m.group(1) if m else None


def patch_group(conn: psycopg.Connection, customer_id: Any, g: dict, ops: list[dict]) -> dict:
    for op in ops:
        kind = str(op.get("op", "")).lower()
        path = str(op.get("path") or "")
        value = op.get("value")
        if kind not in ("add", "replace", "remove"):
            raise ScimError(400, f"Unsupported op {op.get('op')!r}.", "invalidSyntax")
        if path == "members" or (not path and isinstance(value, dict) and "members" in value):
            vals = value if path else value["members"]
            ids = [m.get("value") for m in (vals or [])] if isinstance(vals, list) else None
            if kind == "add":
                add_members(conn, customer_id, g, ids or [])
            elif kind == "replace":
                set_members(conn, customer_id, g, ids or [])
            else:
                remove_members(conn, customer_id, g, ids)
        elif _member_filter(path) and kind == "remove":
            remove_members(conn, customer_id, g, [_member_filter(path)])
        elif path == "displayName" or (not path and isinstance(value, dict) and "displayName" in value):
            name = str(value if path else value["displayName"]).strip()[:120]
            if name:
                g = conn.execute(
                    "UPDATE scim_groups SET display_name = %s, updated_at = now() WHERE id = %s RETURNING *",
                    (name, g["id"]),
                ).fetchone()
        elif path == "externalId":
            g = conn.execute(
                "UPDATE scim_groups SET external_id = %s, updated_at = now() WHERE id = %s RETURNING *",
                (value if kind != "remove" else None, g["id"]),
            ).fetchone()
        else:
            raise ScimError(400, f"Unsupported path {path!r}.", "invalidPath")
    conn.execute("UPDATE scim_groups SET updated_at = now() WHERE id = %s", (g["id"],))
    return conn.execute("SELECT * FROM scim_groups WHERE id = %s", (g["id"],)).fetchone()


def delete_group(conn: psycopg.Connection, customer_id: Any, g: dict) -> None:
    remove_members(conn, customer_id, g, None)
    conn.execute("DELETE FROM scim_groups WHERE id = %s", (g["id"],))


# ---- discovery documents ---------------------------------------------------------


def service_provider_config(base: str) -> dict:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "documentationUri": "https://www.exacarib.com",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_PAGE},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "A SCIM token made by the business admin in ExaCarib CommAI settings.",
                "primary": True,
            }
        ],
        "meta": {"resourceType": "ServiceProviderConfig", "location": f"{base}/ServiceProviderConfig"},
    }


def resource_types(base: str) -> list[dict]:
    return [
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": USER,
            "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/User"},
        },
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "schema": GROUP,
            "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/Group"},
        },
    ]


def _attr(name: str, typ: str = "string", **kw) -> dict:
    return {
        "name": name,
        "type": typ,
        "multiValued": False,
        "required": False,
        "caseExact": False,
        "mutability": "readWrite",
        "returned": "default",
        "uniqueness": "none",
        **kw,
    }


def schemas(base: str) -> list[dict]:
    return [
        {
            "id": USER,
            "name": "User",
            "description": "An ExaCarib account",
            "attributes": [
                _attr("userName", required=True, uniqueness="server"),
                _attr("displayName"),
                _attr("active", "boolean"),
                _attr("externalId"),
                {**_attr("name", "complex"), "subAttributes": [_attr("givenName"), _attr("familyName")]},
                {
                    **_attr("emails", "complex", multiValued=True),
                    "subAttributes": [_attr("value"), _attr("type"), _attr("primary", "boolean")],
                },
                {
                    **_attr("groups", "complex", multiValued=True, mutability="readOnly"),
                    "subAttributes": [_attr("value"), _attr("display")],
                },
            ],
            "meta": {"resourceType": "Schema", "location": f"{base}/Schemas/{USER}"},
        },
        {
            "id": GROUP,
            "name": "Group",
            "description": "A directory group; maps to a CommAI team",
            "attributes": [
                _attr("displayName", required=True, uniqueness="server"),
                _attr("externalId"),
                {
                    **_attr("members", "complex", multiValued=True),
                    "subAttributes": [_attr("value"), _attr("display"), _attr("type")],
                },
            ],
            "meta": {"resourceType": "Schema", "location": f"{base}/Schemas/{GROUP}"},
        },
    ]
