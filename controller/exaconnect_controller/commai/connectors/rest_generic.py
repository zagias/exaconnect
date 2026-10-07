"""A business's own REST API as a connector, from its OpenAPI document (ADR 0028).

How it is made:
1. The business pastes or uploads its OpenAPI 3.0/3.1 document. We read it
   (standards/openapi_import.py) and list its operations. Nothing in it runs.
2. It chooses operations as actions. The kind comes from the HTTP method
   (GET read, POST create, PUT/PATCH update, DELETE delete); deletes always
   need a person's approval. Each action's fields are the operation's own
   parameters; CommAI fields (email, phone, name...) map onto them, with
   suggestions. A draft can be suggested for it, but only from operations the
   document has: an action naming anything else is refused.
3. A person approves the app (commai_rest_apps.status = 'approved'). Any
   change afterwards sends it back to draft. Only then does the app appear
   as "rest_<id>" in that business's catalogue (and only that business's).

Sign-in: the document's own security schemes. API key (header or query),
HTTP Basic, bearer token, or OAuth 2.0 client credentials are entered through
secure entry and kept in the vault; OAuth 2.0 authorisation code uses the
business's own client id and secret through the usual sign-in flow.

Calls go only to the base URL the business set (a public HTTPS address,
checked like webhook addresses); path values are escaped so they can't leave
the operation's path. Writes carry an Idempotency-Key header (the IETF
httpapi draft many APIs follow) and the created object's id is remembered,
so a retried action never creates twice.

Until the business enters credentials, or while ExaCarib has the generic
REST connector ("integration-rest") switched off, a connection runs on a
stand-in built from the document: it answers each chosen operation with the
document's own example (or one shaped by its schema) and keeps what was
created, so the business can try its actions safely.
"""

from __future__ import annotations

import base64
import re
import threading
import time
import urllib.parse
import uuid
from typing import Any

import psycopg

from .. import golive
from ..standards import openapi_import as oai
from . import ActionSpec, ConnectorError, Field, kit
from . import resolvers as connector_resolvers

FEATURE = "integration-rest"
BLOCKED_HEADERS = {"host", "cookie", "authorization", "content-length", "transfer-encoding", "connection", "upgrade"}

golive.declare(
    "feature",
    FEATURE,
    "Your own REST API (OpenAPI import)",
    {
        "live-test": "A real API imported from its OpenAPI document ran a read, a create (retried) and a delete.",
        "ssrf": "Calls reach public HTTPS addresses only; path values cannot leave the chosen operation's path.",
        "approval": "No app runs until a person approved its actions; any change sends it back to draft.",
    },
    {"category": "custom", "env": ["EXA_SECRETS_KEY"]},
)

CREDENTIALS = {
    "api_key": (kit.Credential("api_key", "API key"),),
    "basic": (kit.Credential("username", "User name", secret=False), kit.Credential("password", "Password")),
    "bearer": (kit.Credential("token", "Access token"),),
    "oauth2_client_credentials": (
        kit.Credential("client_id", "Client id", secret=False),
        kit.Credential("client_secret", "Client secret"),
    ),
}
AUTH_TYPES = (*CREDENTIALS, "oauth2_auth_code", "none")


def app_name(rest_id: Any) -> str:
    return f"rest_{uuid.UUID(str(rest_id)).hex}"


def rest_id(app: str) -> str | None:
    m = re.fullmatch(r"rest_([0-9a-f]{32})", app or "")
    return str(uuid.UUID(m.group(1))) if m else None


# ---- the stand-in built from the document -----------------------------------------------


def example(doc: dict, schema: Any, depth: int = 0) -> Any:
    """An example value for a schema: its own example first, then one shaped by it."""
    schema = oai.resolve(doc, schema or {})
    if not isinstance(schema, dict) or depth > 6:
        return None
    if "example" in schema:
        return schema["example"]
    if schema.get("examples") and isinstance(schema["examples"], list):
        return schema["examples"][0]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    for k in ("allOf", "oneOf", "anyOf"):
        if schema.get(k):
            if k == "allOf":
                out: dict = {}
                for part in schema[k]:
                    v = example(doc, part, depth + 1)
                    if isinstance(v, dict):
                        out.update(v)
                return out
            return example(doc, schema[k][0], depth + 1)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object" or "properties" in schema:
        return {k: example(doc, v, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return [example(doc, schema.get("items") or {}, depth + 1)]
    if t in ("integer", "number"):
        return 1
    if t == "boolean":
        return True
    fmt = schema.get("format", "")
    return {"email": "someone@example.com", "date-time": "2026-01-01T09:00:00Z", "date": "2026-01-01"}.get(
        fmt, "example"
    )


class DocumentStandIn(kit.Simulator):
    """Answers the chosen operations in the document's own shapes."""

    def __init__(self, app: str, doc: dict, ops: list[dict], base_path: str):
        super().__init__()
        self.app = app
        self.doc = doc
        self.base_path = base_path.rstrip("/")
        self.ops = []
        for op in ops:
            parts = re.split(r"(\{[^}]+\})", op["path"])
            pat = "".join("([^/]+)" if p.startswith("{") else re.escape(p) for p in parts)
            self.ops.append((op, re.compile("^" + re.escape(self.base_path) + pat + "$")))

    def _response_example(self, op: dict) -> tuple[int, Any]:
        raw = self.doc["paths"][op["path"]][op["method"].lower()]
        for code, resp in (raw.get("responses") or {}).items():
            if str(code).startswith("2"):
                resp = oai.resolve(self.doc, resp)
                content = (resp or {}).get("content") or {}
                media = next((m for m in content if "json" in m), None)
                if not media:
                    return int(code), None
                c = content[media] or {}
                if c.get("example") is not None:
                    return int(code), c["example"]
                if c.get("examples"):
                    first = oai.resolve(self.doc, next(iter(c["examples"].values())))
                    return int(code), (first or {}).get("value")
                return int(code), example(self.doc, c.get("schema"))
        return 200, None

    def handle(self, conn: psycopg.Connection, connection: dict, req: kit.Request) -> kit.Response:
        if (connection.get("settings") or {}).get("simulate_failure"):
            return super().handle(conn, connection, req)
        path = req.path
        for op, pat in self.ops:
            m = pat.match(path)
            if not m or op["method"] != req.method:
                continue
            status, body = self._response_example(op)
            ids = list(m.groups())
            kind = op["path"].split("{")[0].strip("/").replace("/", ".") or "root"
            if req.method == "POST":
                oid = self.new_id(conn, connection, kind)
                data = {**(body if isinstance(body, dict) else {}), **(req.body or {}), "id": oid}
                self.put(conn, connection, kind, oid, data)
                return kit.Response(status, data)
            if req.method in ("PUT", "PATCH") and ids:
                old = self.get(conn, connection, kind, ids[-1]) or (body if isinstance(body, dict) else {})
                data = {**old, **(req.body or {}), "id": ids[-1]}
                self.put(conn, connection, kind, ids[-1], data)
                return kit.Response(status, data)
            if req.method == "DELETE":
                if ids and self.get(conn, connection, kind, ids[-1]) is None and status != 204:
                    return kit.Response(404, self.error_body(404, "Not found."))
                if ids:
                    self.delete(conn, connection, kind, ids[-1])
                return kit.Response(status if status != 200 else 204, None)
            if ids:
                got = self.get(conn, connection, kind, ids[-1])
                return kit.Response(200, got if got is not None else body)
            stored = self.all(conn, connection, kind)
            return kit.Response(status, stored if stored and isinstance(body, list) else body)
        return kit.Response(404, self.error_body(404, f"The document has no {req.method} {path}."))


# ---- the connector ---------------------------------------------------------------------


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


class RestApp(kit.KitConnector):
    category = "custom"
    health_path = ""
    needs_from_exacarib = "Nothing from ExaCarib beyond switching the REST connector on; the business signs in."
    webhooks = "Use an inbound webhook (CloudEvents or Standard Webhooks) to receive its events."

    def __init__(self, row: dict, client_secret: str = ""):
        self.row = row
        self.owner = str(row["customer_id"])
        self.app = app_name(row["id"])
        self.label = row["name"]
        self.description = f"{row['name']}: your own API, from its OpenAPI document (version {row['version']})."
        self.doc = row["document"]
        self.ops = {o["id"]: o for o in row["operations"]}
        a = row["auth"] or {}
        self.auth_type = a.get("type", "none")
        self.auth = {"oauth2_auth_code": "oauth", "none": "none"}.get(self.auth_type, "credentials")
        self.credentials = CREDENTIALS.get(self.auth_type, ())
        self.client_secret = client_secret
        self.chosen = {c["name"]: c for c in row["actions"]}
        self.actions = {}
        for c in row["actions"]:
            op = self.ops[c["operation"]]
            self.actions[c["name"]] = ActionSpec(
                c["name"],
                c.get("label") or op["summary"][:80],
                op["kind"],
                sensitive=op["kind"] == "delete" or bool(c.get("sensitive")),
                fields=tuple(
                    Field(f["key"], f["label"], f["type"] if f["type"] in kit_types() else "string", f["required"])
                    for f in fields_of(op)
                ),
            )
        base_path = urllib.parse.urlsplit(row["base_url"]).path
        self.simulator = DocumentStandIn(
            self.app, self.doc, [self.ops[c["operation"]] for c in row["actions"]], base_path
        )

    @property
    def golive_key(self) -> str:
        return FEATURE

    def env_names(self) -> list[str]:
        return []

    def real_ready(self, conn, customer_id) -> tuple[bool, list[str]]:
        if str(customer_id) != self.owner:
            return False, ["This API belongs to another business."]
        why = []
        if self.auth == "credentials":
            from ..automation import vault

            if not vault.configured():
                why.append("Secure storage is not set up: the controller needs EXA_SECRETS_KEY.")
        if not golive.enabled(conn, "feature", FEATURE, customer_id):
            why.append("ExaCarib has not switched the REST connector on yet (go-live checks).")
        return (not why), why

    def base_url(self, conn, connection) -> str:
        from .. import webhooks

        url = self.row["base_url"]
        if not self.simulated(connection):
            try:
                webhooks.check_url(url)
            except webhooks.UnsafeURL as e:
                raise ConnectorError(str(e), "input") from None
        return url

    # ---- sign-in -------------------------------------------------------------------

    def auth_headers(self, conn, connection) -> dict:
        from ..automation import http, oauth

        if self.simulated(connection) or self.auth_type == "none":
            return {}
        if self.auth == "oauth":
            return super().auth_headers(conn, connection)
        creds = oauth.credentials(conn, connection)
        a = self.row["auth"]
        if self.auth_type == "api_key":
            return {a["name"]: creds["api_key"]} if a.get("in") == "header" else {}
        if self.auth_type == "basic":
            pair = f"{creds['username']}:{creds['password']}".encode()
            return {"Authorization": "Basic " + base64.b64encode(pair).decode()}
        if self.auth_type == "bearer":
            return {"Authorization": f"Bearer {creds['token']}"}
        # OAuth 2.0 client credentials (RFC 6749 section 4.4), cached until a minute before expiry.
        key = str(connection["id"])
        with _tokens_lock:
            tok = _tokens.get(key)
        if tok and tok[1] > time.time() + 60:
            return {"Authorization": f"Bearer {tok[0]}"}
        form = {"grant_type": "client_credentials"}
        if a.get("scopes"):
            form["scope"] = " ".join(a["scopes"])
        try:
            r = http.request("POST", a["token_url"], form=form, basic=(creds["client_id"], creds["client_secret"]))
        except http.NetworkError as e:
            raise ConnectorError(str(e), "provider") from None
        if r.status != 200 or not isinstance(r.body, dict) or not r.body.get("access_token"):
            err = r.body.get("error", "") if isinstance(r.body, dict) else ""
            raise ConnectorError(
                f"The token endpoint refused the client ({r.status}{': ' + err if err else ''}).",
                "expired_signin" if r.status in (400, 401) else "provider",
            )
        with _tokens_lock:
            _tokens[key] = (r.body["access_token"], time.time() + float(r.body.get("expires_in") or 3600))
        return {"Authorization": f"Bearer {r.body['access_token']}"}

    # ---- actions -------------------------------------------------------------------

    def validate(self, action: str, inputs: dict) -> dict:
        chosen = self.chosen[action]
        mapped = dict(inputs or {})
        for ours, theirs in (chosen.get("mapping") or {}).items():
            if ours in mapped and theirs not in mapped:
                mapped[theirs] = mapped.pop(ours)
        out = super().validate(action, mapped)
        for f in fields_of(self.ops[chosen["operation"]]):
            v = out.get(f["key"])
            if v is None:
                continue
            if f["type"] == "number":
                try:
                    out[f["key"]] = int(v) if re.fullmatch(r"-?\d+", str(v)) else float(v)
                except ValueError:
                    raise ValueError(f"{f['label']} must be a number.") from None
            elif f["type"] == "boolean":
                out[f["key"]] = str(v).lower() in ("1", "true", "yes", "on") if not isinstance(v, bool) else v
            elif f["type"] == "json" and not isinstance(v, dict | list):
                raise ValueError(f"{f['label']} must be a JSON object or list.")
        return out

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if str(connection["customer_id"]) != self.owner:
            raise ConnectorError("This API belongs to another business.", "permission")
        chosen = self.chosen.get(action)
        if chosen is None or chosen["operation"] not in self.ops:
            raise ConnectorError(f"Unknown action {action}.", "input")
        op = self.ops[chosen["operation"]]
        path = op["path"]
        params: dict[str, Any] = {}
        headers: dict[str, str] = {}
        body: dict[str, Any] = {}
        for f in fields_of(op):
            if f["key"] not in inputs:
                continue
            v = inputs[f["key"]]
            if f["in"] == "path":
                path = path.replace("{" + f["name"] + "}", urllib.parse.quote(str(v), safe=""))
            elif f["in"] == "query":
                params[f["name"]] = v
            elif f["in"] == "header":
                headers[f["name"]] = str(v)
            else:
                body[f["name"]] = v
        if "{" in path:
            raise ConnectorError("A value the address needs is missing.", "input")
        if self.auth_type == "api_key" and self.row["auth"].get("in") == "query" and not self.simulated(connection):
            from ..automation import oauth

            params[self.row["auth"]["name"]] = oauth.credentials(conn, connection)["api_key"]
        write = op["kind"] != "read"
        if write:
            known = self.known(conn, connection, key)
            if known:
                return {"id": known["object_id"], "replayed": True}
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"method": op["method"], "path": path, "body": body}}
            headers["Idempotency-Key"] = key
        r = self.call(
            conn,
            connection,
            op["method"],
            path,
            params=params or None,
            json_body=body if (body or op["body_media"]) and op["method"] != "GET" else None,
            headers=headers,
            allow=(404,) if op["kind"] == "delete" else (),
        )
        if op["kind"] == "delete" and r.status == 404:
            # Already gone: a retried delete is done.
            self.remember(conn, connection, key, op["id"], path)
            return {"deleted": True, "already_gone": True}
        out: dict = {"status": r.status, "data": _trim(r.body)}
        if write:
            oid = str(r.body.get("id")) if isinstance(r.body, dict) and r.body.get("id") is not None else path
            self.remember(conn, connection, key, op["id"], oid)
            out["id"] = oid
        return out

    def health(self, conn, connection: dict) -> dict:
        read = next((c for c in self.chosen.values() if self.ops[c["operation"]]["kind"] == "read"), None)
        try:
            if self.auth_type == "oauth2_client_credentials" and not self.simulated(connection):
                _tokens.pop(str(connection["id"]), None)
                self.auth_headers(conn, connection)
            if read and not any(f["required"] for f in fields_of(self.ops[read["operation"]])):
                self.execute(conn, connection, read["name"], {}, "health")
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        mode = "the stand-in" if self.simulated(connection) else self.label
        return {"ok": True, "cause": "", "detail": f"{self.label} answering ({mode})."}

    def describe(self) -> dict:
        out = super().describe()
        out["operations"] = {
            c["name"]: f"{self.ops[c['operation']]['method']} {self.ops[c['operation']]['path']}"
            for c in self.row["actions"]
        }
        out["auth_type"] = self.auth_type
        return out


def kit_types() -> tuple[str, ...]:
    return ("string", "number", "email", "datetime", "text", "phone", "boolean", "json")


def fields_of(op: dict) -> list[dict]:
    """The operation's inputs, each with a unique key (a body field that shares a
    parameter's name is keyed body_<name>)."""
    out, seen = [], set()
    for f in op["params"] + op["body"]:
        key = f["name"] if f["name"] not in seen else f"body_{f['name']}"
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        seen.add(key)
        out.append({**f, "key": key})
    return out


def _trim(v: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "..."
    if isinstance(v, dict):
        return {k: _trim(x, depth + 1) for k, x in list(v.items())[:100]}
    if isinstance(v, list):
        return [_trim(x, depth + 1) for x in v[:50]]
    if isinstance(v, str):
        return v[:2000]
    return v


# ---- choosing actions (checked against the document) ------------------------------------


def check_actions(ops: list[dict], actions: list[dict]) -> list[dict]:
    """The business's chosen actions, refused if any names an operation the document
    does not have, repeats a name, or maps onto a parameter the operation lacks."""
    by_id = {o["id"]: o for o in ops}
    names: set[str] = set()
    out = []
    if len(actions) > 50:
        raise ValueError("Choose at most 50 operations.")
    for a in actions:
        op = by_id.get(str(a.get("operation", "")))
        if op is None:
            raise ValueError(f"The document has no operation {a.get('operation')!r}.")
        name = str(a.get("name") or op["id"])
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", name):
            raise ValueError(f"Action names are lower case letters, digits and _ ({name!r}).")
        if name in names:
            raise ValueError(f"Two actions are called {name}.")
        names.add(name)
        keys = {f["key"] for f in fields_of(op)}
        mapping = {str(k): str(v) for k, v in (a.get("mapping") or {}).items() if v}
        bad = [v for v in mapping.values() if v not in keys]
        if bad:
            raise ValueError(f"{name}: the operation has no field {bad[0]!r}.")
        out.append(
            {
                "operation": op["id"],
                "name": name,
                "label": str(a.get("label") or op["summary"])[:80],
                "sensitive": op["kind"] == "delete" or bool(a.get("sensitive")),
                "mapping": mapping,
            }
        )
    return out


def check_auth(doc: dict, auth: dict) -> dict:
    """The sign-in method, which must be one the document declares (or none)."""
    t = str(auth.get("type", "none"))
    if t not in AUTH_TYPES:
        raise ValueError(f"Sign-in type must be one of {', '.join(AUTH_TYPES)}.")
    if t == "none":
        return {"type": "none"}
    declared = [s for s in oai.security_schemes(doc) if s["type"] == t]
    if not declared:
        raise ValueError(f"The document does not declare {t.replace('_', ' ')} sign-in.")
    s = next((x for x in declared if x["scheme"] == auth.get("scheme")), declared[0])
    out = {k: v for k, v in s.items()}
    if t in ("oauth2_client_credentials", "oauth2_auth_code"):
        from .. import webhooks

        for k in ("token_url", "authorize_url"):
            if k in out:
                try:
                    webhooks.check_url(out[k])
                except webhooks.UnsafeURL as e:
                    raise ValueError(f"{k}: {e}") from None
        if t == "oauth2_auth_code":
            out["client_id"] = str(auth.get("client_id") or "")
            if not out["client_id"]:
                raise ValueError("Give your OAuth client id.")
        wanted = auth.get("scopes")
        if wanted is not None:
            unknown = set(wanted) - set(s.get("scopes") or [])
            if unknown:
                raise ValueError(f"The document has no scope {sorted(unknown)[0]!r}.")
            out["scopes"] = sorted(wanted)
    return out


# ---- finding a business's approved apps ----------------------------------------------------


_cache: dict[str, tuple[int, RestApp]] = {}


def load(conn: psycopg.Connection, row: dict) -> RestApp:
    hit = _cache.get(str(row["id"]))
    if hit and hit[0] == row["version"]:
        return hit[1]
    secret = ""
    ref = (row["auth"] or {}).get("client_secret_ref")
    if ref:
        from ..automation import vault

        try:
            secret = (vault.get(conn, row["customer_id"], ref) or {}).get("client_secret", "")
        except vault.VaultError:
            secret = ""
    app = RestApp(row, secret)
    _cache[str(row["id"])] = (row["version"], app)
    return app


def _resolve(app: str) -> RestApp | None:
    rid = rest_id(app)
    if rid is None:
        return None
    from ... import db

    with db.tx() as conn:
        row = conn.execute("SELECT * FROM commai_rest_apps WHERE id = %s AND status = 'approved'", (rid,)).fetchone()
        return load(conn, row) if row else None


def _resolve_oauth(app: str):
    from ..automation import oauth

    c = _resolve(app)
    if c is None or c.auth != "oauth":
        return None
    a = c.row["auth"]
    return oauth.Provider(
        app,
        c.label,
        a["authorize_url"],
        a["token_url"],
        tuple(a.get("scopes") or ()),
        "EXA_REST",
        client_id=a["client_id"],
        client_secret=c.client_secret,
    )


def owned(conn: psycopg.Connection, customer_id: Any) -> list[RestApp]:
    rows = conn.execute(
        "SELECT * FROM commai_rest_apps WHERE customer_id = %s AND status = 'approved' ORDER BY name", (customer_id,)
    ).fetchall()
    return [load(conn, r) for r in rows]


connector_resolvers.append(_resolve)


def _register_oauth() -> None:
    from ..automation import oauth

    oauth.resolvers.append(_resolve_oauth)


_register_oauth()
