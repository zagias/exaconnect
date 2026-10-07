"""Directory templates and connectors for a business (ADR 0030).

"Connect your directory": pick a provider, get its guided set-up with the
values to paste (made for this business), save the provider's details, run
the connection test, accept preset group mappings, then connect. Sign-in
still goes through the enterprise SSO connection (ADR 0017), created here
with the template's settings; email domains are routed only once ExaCarib
approves them, as before. Every provider is a go-live feature and a business
can connect it only once ExaCarib has switched it on."""

from __future__ import annotations

import os
import re
from typing import Literal

import psycopg
from fastapi import APIRouter, HTTPException, Request, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import audit, db
from ..commai import access, golive
from ..commai.automation import vault
from ..identity import keycloak, scim, sso
from ..identity.directory import checks, scim_fixtures, sources, sync, templates
from ..security import new_token, token_hash
from . import sso as sso_api
from .deps import User, UserDep

router = APIRouter(tags=["directory"])

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9-]{1,63}$")
_DN = re.compile(r"^[^\n\r\x00]{3,500}$")


def _biz_admin(user: User, customer_id: str) -> None:
    access.check(user, customer_id, "commai:admin")


def _template(provider: str) -> templates.Template:
    try:
        return templates.get(provider)
    except templates.TemplateError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


def _gateway(request: Request) -> keycloak.KeycloakAdmin:
    return keycloak.admin_for(request.app.state.settings)


def _gateway_call(fn, *args) -> None:
    try:
        fn(*args)
    except keycloak.GatewayError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e


def _available(conn: psycopg.Connection, t: templates.Template, customer_id: str) -> bool:
    return golive.enabled(conn, "feature", f"directory-{t.key}", customer_id)


def _setup(conn: psycopg.Connection, customer_id: str, provider: str, lock: bool = False) -> dict | None:
    return conn.execute(
        "SELECT * FROM directory_setups WHERE customer_id = %s AND provider = %s" + (" FOR UPDATE" if lock else ""),
        (customer_id, provider),
    ).fetchone()


def _ctx(request: Request, alias: str, inputs: dict) -> templates.Context:
    s = request.app.state.settings
    return templates.Context(
        public_url=s.public_url or str(request.base_url).rstrip("/"),
        issuer=s.oidc_issuer,
        alias=alias,
        inputs=inputs,
        graph_client_id=os.environ.get("EXA_MS_GRAPH_CLIENT_ID", ""),
        google_client_id=sources.google_client_id(),
    )


_PUBLIC_SETTINGS = (
    "group_prefix",
    "host",
    "port",
    "base_dn",
    "bind_dn",
    "user_filter",
    "group_filter",
    "plain_lab",
    "has_ca_cert",
)


def _state(conn: psycopg.Connection, setup: dict | None) -> dict | None:
    if setup is None:
        return None
    c = None
    if setup["connection_id"]:
        c = conn.execute("SELECT * FROM sso_connections WHERE id = %s", (setup["connection_id"],)).fetchone()
    domains = (
        conn.execute(
            "SELECT id, domain, status FROM sso_domains WHERE connection_id = %s ORDER BY domain", (c["id"],)
        ).fetchall()
        if c
        else []
    )
    tok = None
    if setup["scim_token_id"]:
        tok = conn.execute(
            "SELECT id, prefix, created_at, last_used_at, revoked_at FROM scim_tokens WHERE id = %s",
            (setup["scim_token_id"],),
        ).fetchone()
    settings = setup["settings"] or {}
    return {
        "id": str(setup["id"]),
        "status": setup["status"],
        "protocol": setup["protocol"],
        "mode": setup["mode"],
        "inputs": setup["inputs"],
        "settings": {
            **{k: settings[k] for k in _PUBLIC_SETTINGS if k in settings},
            "has_ca_cert": bool(settings.get("ca_cert_pem")),
        },
        "has_password": bool(setup["secret_ref"]),
        "sync_interval_min": setup["sync_interval_s"] // 60,
        "accepted_presets": setup["accepted_presets"],
        "presets_by": setup["presets_by"],
        "last_test": setup["last_test"],
        "last_sync": setup["last_sync"],
        "connected_by": setup["connected_by"],
        "connected_at": setup["connected_at"],
        "domains": domains,
        "connection": None
        if c is None
        else {
            "id": str(c["id"]),
            "status": c["status"],
            "last_test": c["last_test"],
            "has_metadata_xml": bool(c["metadata_xml"]),
            "metadata_url": c["metadata_url"],
            "client_id": c["client_id"],
        },
        "scim_token": None
        if tok is None or tok["revoked_at"]
        else {k: tok[k] for k in ("prefix", "created_at", "last_used_at")},
    }


def _guide(conn: psycopg.Connection, request: Request, t: templates.Template, customer_id: str, setup: dict | None):
    alias = setup["alias"] if setup else "{alias}"
    protocol = setup["protocol"] if setup else t.default_protocol
    mode = setup["mode"] if setup else t.modes[0]
    out = templates.render(t, _ctx(request, alias, (setup or {}).get("inputs") or {}), protocol, mode)
    s = request.app.state.settings
    return {
        **out,
        "available": _available(conn, t, customer_id),
        "gateway": {"configured": bool(s.oidc_issuer), "simulated": _gateway(request).simulated},
        "setup": _state(conn, setup),
        "plain_ldap_allowed": sources.plain_allowed(),
    }


# ---- reading ------------------------------------------------------------------------------------


@router.get("/directory/templates")
def list_templates(user: UserDep) -> list[dict]:
    """Every provider template, for anyone signed in."""
    return [
        {
            "provider": t.key,
            "name": t.name,
            "summary": t.summary,
            "protocols": t.protocols,
            "modes": list(t.modes),
            "scim": t.scim.supported,
            "pull": t.pull,
        }
        for t in templates.TEMPLATES.values()
    ]


@router.get("/customers/{customer_id}/directory")
def list_setups(customer_id: str, user: UserDep) -> list[dict]:
    _biz_admin(user, customer_id)
    with db.tx() as conn:
        rows = {
            r["provider"]: r
            for r in conn.execute("SELECT * FROM directory_setups WHERE customer_id = %s", (customer_id,)).fetchall()
        }
        return [
            {
                "provider": t.key,
                "name": t.name,
                "summary": t.summary,
                "available": _available(conn, t, customer_id),
                "status": rows[t.key]["status"] if t.key in rows else "off",
                "started": t.key in rows,
                "mode": rows[t.key]["mode"] if t.key in rows else None,
            }
            for t in templates.TEMPLATES.values()
        ]


@router.get("/customers/{customer_id}/directory/{provider}")
def get_guide(customer_id: str, provider: str, user: UserDep, request: Request) -> dict:
    _biz_admin(user, customer_id)
    t = _template(provider)
    with db.tx() as conn:
        return _guide(conn, request, t, customer_id, _setup(conn, customer_id, provider))


# ---- saving the provider's details -------------------------------------------------------------


class SetupIn(BaseModel):
    protocol: Literal["", "saml", "oidc"] | None = None
    mode: Literal["none", "scim", "pull", "ldap"] | None = None
    inputs: dict[str, str] | None = Field(default=None, max_length=10)
    domains: list[str] | None = Field(default=None, max_length=20)
    metadata_url: str | None = Field(default=None, max_length=500)
    metadata_xml: str | None = Field(default=None, max_length=512_000)
    client_id: str | None = Field(default=None, max_length=200)
    # Passed to the sign-in gateway only, never stored by the controller.
    client_secret: str | None = Field(default=None, max_length=500, repr=False)
    group_prefix: str | None = Field(default=None, max_length=60)
    sync_interval_min: int | None = Field(default=None, ge=15, le=1440)
    # LDAP / Active Directory.
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    base_dn: str | None = Field(default=None, max_length=500)
    bind_dn: str | None = Field(default=None, max_length=500)
    bind_password: str | None = Field(default=None, max_length=500, repr=False)  # into the vault
    user_filter: str | None = Field(default=None, max_length=1000)
    group_filter: str | None = Field(default=None, max_length=1000)
    ca_cert_pem: str | None = Field(default=None, max_length=20_000)
    plain_lab: bool | None = None


def _check_filter(f: str, what: str) -> str:
    f = f.strip()
    if f and not (f.startswith("(") and f.endswith(")") and f.count("(") == f.count(")")):
        raise HTTPException(422, f"The {what} filter must be an LDAP filter in brackets, like (objectClass=user).")
    return f


def _ldap_settings(body: SetupIn, current: dict) -> dict:
    s = dict(current)
    if body.host is not None:
        host = body.host.strip().lower().removeprefix("ldaps://").removeprefix("ldap://").rstrip("/")
        if host and not _HOSTNAME.match(host):
            raise HTTPException(422, "The LDAP host should be a host name, like dc1.example.com.")
        s["host"] = host
    if body.port is not None:
        s["port"] = body.port
    for k in ("base_dn", "bind_dn"):
        v = getattr(body, k)
        if v is not None:
            if v.strip() and not _DN.match(v.strip()):
                raise HTTPException(422, f"The {k.replace('_', ' ')} doesn't look like a DN.")
            s[k] = v.strip()
    if body.user_filter is not None:
        s["user_filter"] = _check_filter(body.user_filter, "user")
    if body.group_filter is not None:
        s["group_filter"] = _check_filter(body.group_filter, "group")
    if body.ca_cert_pem is not None:
        pem = body.ca_cert_pem.strip()
        if pem:
            from cryptography import x509

            try:
                x509.load_pem_x509_certificates(pem.encode())
            except ValueError:
                raise HTTPException(422, "The CA certificate is not a PEM certificate.") from None
        s["ca_cert_pem"] = pem
    if body.plain_lab is not None:
        if body.plain_lab and not sources.plain_allowed():
            raise HTTPException(422, "Plain LDAP is refused. Use LDAPS (port 636).")
        s["plain_lab"] = bool(body.plain_lab)
        if body.plain_lab and body.port is None:
            s["port"] = 389
    return s


def _provider_url(t: templates.Template, protocol: str, inputs: dict) -> str:
    spec = t.saml if protocol == "saml" else t.oidc
    pattern = (spec.metadata_url if isinstance(spec, templates.Saml) else spec.discovery_url) if spec else ""
    if not pattern or templates.missing(pattern, inputs):
        return ""
    return templates.fill(pattern, inputs)


def _sync_connection(conn, request: Request, t: templates.Template, setup: dict, body: SetupIn, actor: str) -> dict:
    """Create or update the SSO connection this set-up uses (ADR 0017 rules: draft until tested)."""
    protocol = setup["protocol"]
    if not protocol:
        return setup
    c = None
    if setup["connection_id"]:
        c = conn.execute("SELECT * FROM sso_connections WHERE id = %s", (setup["connection_id"],)).fetchone()
    xml = body.metadata_xml if body.metadata_xml is not None else (c["metadata_xml"] if c else "")
    url = (body.metadata_url.strip() if body.metadata_url is not None else "") or _provider_url(
        t, protocol, setup["inputs"] or {}
    )
    if not url and c and body.metadata_url is None:
        url = c["metadata_url"]
    client_id = (body.client_id if body.client_id is not None else (c["client_id"] if c else "")).strip()
    if protocol == "saml":
        if xml.strip():
            try:
                sso.check_saml_metadata(xml)
            except sso.SsoError as e:
                raise HTTPException(422, str(e)) from e
            url = "" if body.metadata_url is None else url
        if not xml.strip() and not url:
            return setup  # not enough yet; the guide says what is missing
    else:
        if not url or not client_id:
            return setup
        xml = ""
    if url:
        why = checks.host_allowed(url, t.metadata_hosts)
        if why:
            raise HTTPException(422, why)
    changed = (
        c is None
        or (xml, url, client_id, protocol) != (c["metadata_xml"], c["metadata_url"], c["client_id"], c["protocol"])
        or bool(body.client_secret)
    )
    if c is None:
        c = conn.execute(
            """INSERT INTO sso_connections (customer_id, alias, protocol, display_name, metadata_xml, metadata_url,
                                            client_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (setup["customer_id"], setup["alias"], protocol, t.name, xml, url, client_id),
        ).fetchone()
        setup = conn.execute(
            "UPDATE directory_setups SET connection_id = %s WHERE id = %s RETURNING *", (c["id"], setup["id"])
        ).fetchone()
    elif changed:
        c = conn.execute(
            """UPDATE sso_connections SET protocol = %s, metadata_xml = %s, metadata_url = %s, client_id = %s,
                 status = 'draft', updated_at = now() WHERE id = %s RETURNING *""",
            (protocol, xml, url, client_id, c["id"]),
        ).fetchone()
    if changed:
        idp = keycloak.IdpConfig(
            alias=c["alias"],
            display_name=c["display_name"],
            protocol=protocol,
            metadata_xml=xml,
            metadata_url=url,
            client_id=client_id,
            client_secret=body.client_secret or "",
            enabled=True,
        )
        _gateway_call(_gateway(request).upsert_idp, idp)
        audit.record(conn, actor, "sso.update", c["alias"], setup["customer_id"], {"via": f"directory:{t.key}"})
    return setup


@router.put("/customers/{customer_id}/directory/{provider}")
def save_setup(customer_id: str, provider: str, body: SetupIn, user: UserDep, request: Request) -> dict:
    """Save the provider's details. Makes or updates a draft SSO connection; switches nothing on."""
    _biz_admin(user, customer_id)
    t = _template(provider)
    if body.protocol is not None and body.protocol and body.protocol not in t.protocols:
        raise HTTPException(422, f"{t.name} doesn't offer that sign-in protocol here.")
    if body.mode is not None and body.mode not in t.modes:
        raise HTTPException(422, f"{t.name} doesn't offer that way of syncing people.")
    with db.tx() as conn:
        setup = _setup(conn, customer_id, provider, lock=True)
        if setup is None:
            setup = conn.execute(
                """INSERT INTO directory_setups (customer_id, provider, alias, protocol, mode, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (customer_id, provider, sso.new_alias(t.key), t.default_protocol, t.modes[0], user.actor),
            ).fetchone()
            audit.record(conn, user.actor, "directory.start", provider, customer_id)
        inputs = dict(setup["inputs"] or {})
        if body.inputs is not None:
            try:
                inputs.update(templates.check_inputs(t, body.inputs))
            except templates.TemplateError as e:
                raise HTTPException(422, str(e)) from e
            for k, v in body.inputs.items():
                if not str(v or "").strip():
                    inputs.pop(k, None)
        settings = dict(setup["settings"] or {})
        if body.group_prefix is not None:
            settings["group_prefix"] = body.group_prefix.strip()
        if t.pull == "ldap":
            settings = _ldap_settings(body, settings)
        secret_ref = setup["secret_ref"]
        if body.bind_password:
            if t.pull != "ldap":
                raise HTTPException(422, f"{t.name} doesn't use a bind password.")
            try:
                secret_ref = vault.put(
                    conn, customer_id, f"directory:{provider}", {"password": body.bind_password}, secret_ref
                )
            except vault.VaultError as e:
                raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
        interval = body.sync_interval_min * 60 if body.sync_interval_min else setup["sync_interval_s"]
        setup = conn.execute(
            """UPDATE directory_setups SET protocol = %s, mode = %s, inputs = %s, settings = %s, secret_ref = %s,
                 sync_interval_s = %s, updated_at = now() WHERE id = %s RETURNING *""",
            (
                setup["protocol"] if body.protocol is None else body.protocol,
                setup["mode"] if body.mode is None else body.mode,
                Jsonb(inputs),
                Jsonb(settings),
                secret_ref,
                interval,
                setup["id"],
            ),
        ).fetchone()
        setup = _sync_connection(conn, request, t, setup, body, user.actor)
        if body.domains is not None:
            if not setup["connection_id"]:
                if body.domains:
                    raise HTTPException(422, "Give the sign-in details first; domains route people to that sign-in.")
            else:
                c = conn.execute("SELECT * FROM sso_connections WHERE id = %s", (setup["connection_id"],)).fetchone()
                sso_api._set_domains(conn, c, body.domains, user.actor)
        changed = sorted(k for k in body.model_dump(exclude_unset=True) if k not in ("client_secret", "bind_password"))
        audit.record(
            conn,
            user.actor,
            "directory.save",
            provider,
            customer_id,
            {
                "changed": changed
                + (["client_secret"] if body.client_secret else [])
                + (["bind_password"] if body.bind_password else [])
            },
        )
        return _guide(conn, request, t, customer_id, setup)


# ---- testing --------------------------------------------------------------------------------------


def _need(setup: dict) -> dict:
    if setup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Save the provider's details first.")
    return setup


@router.post("/customers/{customer_id}/directory/{provider}/test")
def run_test(customer_id: str, provider: str, user: UserDep, request: Request) -> dict:
    """The connection test: metadata or discovery fetch and parse, the test sign-in's
    result, a SCIM dry run with the provider's request shapes, and a read from the
    directory for pull connectors. Nothing is switched on."""
    _biz_admin(user, customer_id)
    t = _template(provider)
    with db.tx() as conn:
        setup = _need(_setup(conn, customer_id, provider, lock=True))
        results: list[dict] = []
        c = None
        if setup["connection_id"]:
            c = conn.execute("SELECT * FROM sso_connections WHERE id = %s", (setup["connection_id"],)).fetchone()
        if setup["protocol"]:
            spec = t.saml if setup["protocol"] == "saml" else t.oidc
            if c is None:
                pattern = (
                    (spec.metadata_url if isinstance(spec, templates.Saml) else spec.discovery_url) if spec else ""
                )
                gone = templates.missing(pattern, setup["inputs"] or {}) if pattern else []
                need = ", ".join(i.label for i in t.inputs if i.key in gone) or "the provider's metadata"
                results.append(checks.result("sign-in details", False, f"Enter {need} first."))
            elif setup["protocol"] == "saml" and c["metadata_xml"]:
                results += checks.parse_saml(c["metadata_xml"], t.saml.name_id_format if t.saml else "")
            else:
                kind = "saml" if setup["protocol"] == "saml" else "oidc"
                expected = t.saml.name_id_format if (kind == "saml" and t.saml) else ""
                results += checks.fetch_checks(kind, c["metadata_url"], t.metadata_hosts, expected)
            if c is not None:
                lt = c["last_test"] or {}
                if lt.get("ok") and c["status"] in ("tested", "enabled", "disabled"):
                    results.append(
                        checks.result("test sign-in", True, f"Worked for {lt.get('email') or 'a test user'}.")
                    )
                else:
                    msg = lt.get("message") if lt else ""
                    results.append(
                        checks.result(
                            "test sign-in",
                            False,
                            (
                                f"The last test sign-in failed: {msg}"
                                if lt and not lt.get("ok")
                                else "Not run since the details last changed. Choose 'Test sign-in' to run one."
                            ),
                        )
                    )
        if setup["mode"] == "scim":
            domain = next(iter(_domains(conn, setup)), "")
            results.append(scim_fixtures.dry_run(conn, provider, customer_id, domain))
            tok = None
            if setup["scim_token_id"]:
                tok = conn.execute("SELECT * FROM scim_tokens WHERE id = %s", (setup["scim_token_id"],)).fetchone()
            if tok and tok["last_used_at"] and not tok["revoked_at"]:
                results.append(
                    checks.result(
                        "SCIM calls",
                        True,
                        f"{t.name} last called ExaCarib on {tok['last_used_at']:%d %b %Y %H:%M} UTC.",
                    )
                )
            else:
                results.append(
                    checks.result(
                        "SCIM calls",
                        True,
                        f"{t.name} has not called ExaCarib yet. Connect to "
                        "get the token, then use the provider's own test.",
                        warn=True,
                    )
                )
        if setup["mode"] in ("pull", "ldap"):
            try:
                results += sync.source_for(conn, setup).test()
            except sources.SourceError as e:
                results.append(checks.result("directory read", False, str(e)))
        if not results:
            results.append(checks.result("set-up", False, "Choose a sign-in protocol or a way to sync people."))
        out = {"ok": all(r["ok"] for r in results), "at": _now(), "results": results}
        conn.execute(
            "UPDATE directory_setups SET last_test = %s, tested_at = now() WHERE id = %s", (Jsonb(out), setup["id"])
        )
        audit.record(conn, user.actor, "directory.test", provider, customer_id, {"ok": out["ok"]})
        if c is not None:
            out["signin_test_path"] = f"/customers/{customer_id}/sso-connections/{c['id']}/test"
        return out


def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


def _domains(conn, setup: dict) -> list[str]:
    if not setup["connection_id"]:
        return []
    return [
        r["domain"]
        for r in conn.execute(
            "SELECT domain FROM sso_domains WHERE connection_id = %s ORDER BY domain", (setup["connection_id"],)
        ).fetchall()
    ]


# ---- connecting ------------------------------------------------------------------------------------


@router.post("/customers/{customer_id}/directory/{provider}/connect")
def connect(customer_id: str, provider: str, user: UserDep, request: Request) -> dict:
    """Switch this business's directory on: the tested sign-in, a SCIM token
    (shown once) or the scheduled pull sync. Needs ExaCarib to have switched
    the provider on (go-live)."""
    _biz_admin(user, customer_id)
    t = _template(provider)
    token = None
    with db.tx() as conn:
        setup = _need(_setup(conn, customer_id, provider, lock=True))
        try:
            golive.require(conn, "feature", f"directory-{t.key}", customer_id)
        except golive.GoLiveError as e:
            raise HTTPException(e.code, str(e)) from e
        if not setup["protocol"] and setup["mode"] == "none":
            raise HTTPException(422, "Choose a sign-in protocol or a way to sync people first.")
        if setup["protocol"]:
            c = conn.execute("SELECT * FROM sso_connections WHERE id = %s", (setup["connection_id"],)).fetchone()
            if c is None or c["status"] == "draft" or not (c["last_test"] or {}).get("ok"):
                raise HTTPException(status.HTTP_409_CONFLICT, "Run a successful test sign-in before connecting.")
            _gateway_call(_gateway(request).set_enabled, c["alias"], True)
            conn.execute("UPDATE sso_connections SET status = 'enabled', updated_at = now() WHERE id = %s", (c["id"],))
            audit.record(conn, user.actor, "sso.enable", c["alias"], customer_id, {"via": f"directory:{t.key}"})
        if setup["mode"] in ("pull", "ldap"):
            lt = setup["last_test"] or {}
            read_ok = any(
                r["ok"] and r["check"] in ("Microsoft Graph", "Google Directory API", "LDAP search")
                for r in lt.get("results", [])
            )
            if not read_ok:
                raise HTTPException(status.HTTP_409_CONFLICT, "Run a connection test that reads your directory first.")
        if setup["mode"] == "scim":
            old = setup["scim_token_id"]
            if old:
                conn.execute("UPDATE scim_tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (old,))
            n = conn.execute(
                "SELECT count(*) AS n FROM scim_tokens WHERE customer_id = %s AND revoked_at IS NULL", (customer_id,)
            ).fetchone()["n"]
            if n >= 5:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "This organisation has 5 SCIM tokens. Revoke one first."
                )
            token = scim.TOKEN_PREFIX + new_token()
            row = conn.execute(
                """INSERT INTO scim_tokens (customer_id, name, prefix, token_hash, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id, prefix""",
                (customer_id, t.name[:60], token[:12], token_hash(token), user.actor),
            ).fetchone()
            conn.execute("UPDATE directory_setups SET scim_token_id = %s WHERE id = %s", (row["id"], setup["id"]))
            audit.record(
                conn, user.actor, "scim_token.create", row["prefix"], customer_id, {"via": f"directory:{t.key}"}
            )
        setup = conn.execute(
            """UPDATE directory_setups SET status = 'connected', connected_by = %s, connected_at = now(),
                 updated_at = now() WHERE id = %s RETURNING *""",
            (user.actor, setup["id"]),
        ).fetchone()
        if setup["mode"] in ("pull", "ldap"):
            sync.schedule(conn, setup)
        audit.record(conn, user.actor, "directory.connect", provider, customer_id, {"mode": setup["mode"]})
        out = _guide(conn, request, t, customer_id, setup)
    if token:
        out["scim_token_value"] = token  # shown once
    return out


@router.post("/customers/{customer_id}/directory/{provider}/disconnect")
def disconnect(customer_id: str, provider: str, user: UserDep, request: Request) -> dict:
    """Switch it off: sign-in through it stops, its SCIM token is revoked, the pull schedule ends.
    People already made keep their accounts (switch them off in the directory first if needed)."""
    _biz_admin(user, customer_id)
    t = _template(provider)
    with db.tx() as conn:
        setup = _need(_setup(conn, customer_id, provider, lock=True))
        _switch_off(conn, request, setup, user.actor)
        setup = conn.execute(
            "UPDATE directory_setups SET status = 'off', updated_at = now() WHERE id = %s RETURNING *", (setup["id"],)
        ).fetchone()
        audit.record(conn, user.actor, "directory.disconnect", provider, customer_id)
        return _guide(conn, request, t, customer_id, setup)


def _switch_off(conn, request: Request, setup: dict, actor: str) -> None:
    if setup["connection_id"]:
        c = conn.execute("SELECT * FROM sso_connections WHERE id = %s", (setup["connection_id"],)).fetchone()
        if c and c["status"] == "enabled":
            _gateway_call(_gateway(request).set_enabled, c["alias"], False)
            conn.execute("UPDATE sso_connections SET status = 'disabled', updated_at = now() WHERE id = %s", (c["id"],))
            audit.record(conn, actor, "sso.disable", c["alias"], setup["customer_id"])
    if setup["scim_token_id"]:
        r = conn.execute(
            "UPDATE scim_tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING prefix",
            (setup["scim_token_id"],),
        ).fetchone()
        if r:
            audit.record(conn, actor, "scim_token.revoke", r["prefix"], setup["customer_id"])


@router.delete("/customers/{customer_id}/directory/{provider}", status_code=204)
def delete_setup(customer_id: str, provider: str, user: UserDep, request: Request) -> None:
    _biz_admin(user, customer_id)
    _template(provider)
    with db.tx() as conn:
        setup = _need(_setup(conn, customer_id, provider, lock=True))
        _switch_off(conn, request, setup, user.actor)
        if setup["connection_id"]:
            c = conn.execute("SELECT alias FROM sso_connections WHERE id = %s", (setup["connection_id"],)).fetchone()
            if c:
                _gateway_call(_gateway(request).delete_idp, c["alias"])
                conn.execute("DELETE FROM sso_connections WHERE id = %s", (setup["connection_id"],))
        vault.delete(conn, customer_id, setup["secret_ref"])
        conn.execute("DELETE FROM directory_setups WHERE id = %s", (setup["id"],))
        audit.record(conn, user.actor, "directory.delete", provider, customer_id)


@router.post("/customers/{customer_id}/directory/{provider}/sync", status_code=202)
def sync_now(customer_id: str, provider: str, user: UserDep) -> dict:
    _biz_admin(user, customer_id)
    _template(provider)
    with db.tx() as conn:
        setup = _need(_setup(conn, customer_id, provider))
        if setup["status"] != "connected" or setup["mode"] not in ("pull", "ldap"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Only a connected pull or LDAP directory can sync now.")
        from ..commai import jobs

        jobs.enqueue(
            conn,
            "directory.sync",
            {"setup_id": str(setup["id"]), "once": True},
            customer_id=customer_id,
            dedupe_key=f"directory.sync-now:{setup['id']}:{new_token()[:12]}",
        )
        audit.record(conn, user.actor, "directory.sync_now", provider, customer_id)
    return {"queued": True}


# ---- group presets and the mapping preview ---------------------------------------------------------


class PresetsIn(BaseModel):
    accepted: list[str] = Field(max_length=20)


@router.put("/customers/{customer_id}/directory/{provider}/presets")
def accept_presets(customer_id: str, provider: str, body: PresetsIn, user: UserDep, request: Request) -> dict:
    """Accept preset group mappings. An admin preset only asks for admin rights:
    a different business admin (or ExaCarib) still has to approve it."""
    _biz_admin(user, customer_id)
    t = _template(provider)
    ids = {p.id for p in t.presets}
    unknown = [p for p in body.accepted if p not in ids]
    if unknown:
        raise HTTPException(422, f"{t.name} has no preset called {', '.join(unknown)}.")
    with db.tx() as conn:
        setup = _need(_setup(conn, customer_id, provider, lock=True))
        setup = conn.execute(
            """UPDATE directory_setups SET accepted_presets = %s, presets_by = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (sorted(set(body.accepted)), user.actor, setup["id"]),
        ).fetchone()
        audit.record(
            conn, user.actor, "directory.presets", provider, customer_id, {"accepted": setup["accepted_presets"]}
        )
        return _guide(conn, request, t, customer_id, setup)


@router.post("/customers/{customer_id}/directory/{provider}/presets/apply")
def apply_presets(customer_id: str, provider: str, user: UserDep) -> dict:
    """Apply accepted presets to groups that have already arrived (by SCIM or a sync)."""
    _biz_admin(user, customer_id)
    _template(provider)
    with db.tx() as conn:
        setup = _need(_setup(conn, customer_id, provider, lock=True))
        done = sync.apply_presets(conn, setup)
        audit.record(conn, user.actor, "directory.presets_apply", provider, customer_id, {"groups": done})
    return {"applied": done}


@router.get("/customers/{customer_id}/directory/{provider}/preview")
def mapping_preview(customer_id: str, provider: str, user: UserDep, live: bool = False) -> dict:
    """How each directory group maps to a team, a seat and admin rights."""
    _biz_admin(user, customer_id)
    t = _template(provider)
    with db.tx() as conn:
        setup = _setup(conn, customer_id, provider)
        names = [p.group for p in t.presets]
        source_note = "Preset groups and groups already in ExaCarib."
        if live:
            setup = _need(setup)
            try:
                snap = sync.source_for(conn, setup).snapshot()
            except sources.SourceError as e:
                raise HTTPException(422, str(e)) from e
            names = [g.name for g in snap.groups]
            source_note = f"{len(snap.groups)} groups and {len(snap.users)} people read from your directory just now."
        return {"note": source_note, "groups": sync.preview(conn, setup, provider, customer_id, names)}
