"""Integrations API (ADR 0026): subscriptions, connectors, delivery log, REST
hooks, metrics, RESTCONF, carrier notices and cloud on-ramps."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import audit, db
from ..api.deps import AdminDep, UserDep
from ..commai.automation.vault import VaultError
from . import (
    catalogue,
    cloudevents,
    delivery,
    metrics,
    netbox,
    notices,
    onramps,
    providers,
    publish,
    restconf,
    transport,
)
from .adapters.clouds import ADAPTERS, OnrampError

router = APIRouter(tags=["integrations"])

PUBLIC_COLUMNS = (
    "id",
    "customer_id",
    "carrier_id",
    "provider",
    "name",
    "enabled",
    "config",
    "event_types",
    "site_ids",
    "min_severity",
    "origin",
    "platform",
    "last_status",
    "last_delivery_at",
    "created_by",
    "created_at",
    "updated_at",
)
MAX_INTEGRATIONS = 50


def public(row: dict) -> dict:
    p = providers.get(row["provider"])
    out = {k: row[k] for k in PUBLIC_COLUMNS}
    out["provider_name"] = p.name if p else row["provider"]
    out["category"] = p.category if p else ""
    out["secrets_set"] = list(row["secret_fields"] or [])
    out["mode"] = delivery.mode_of(row)
    return out


def _owner_where(user, customer_id: str | None = None, carrier_id: str | None = None) -> tuple[str, dict]:
    if user.role == "admin":
        if carrier_id:
            return "carrier_id = %(k)s", {"k": carrier_id}
        if customer_id:
            return "customer_id = %(c)s", {"c": customer_id}
        return "true", {}
    if user.role == "customer":
        return "customer_id = %(c)s", {"c": user.customer_id}
    if user.role == "carrier" and user.carrier_id:
        return "carrier_id = %(k)s", {"k": user.carrier_id}
    raise HTTPException(403, "Not available for this account.")


def _get(conn, user, integration_id: int, lock: bool = False) -> dict:
    where, args = _owner_where(user)
    row = conn.execute(
        f"SELECT * FROM connect_integrations WHERE id = %(id)s AND {where}" + (" FOR UPDATE" if lock else ""),
        {**args, "id": integration_id},
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Integration not found.")
    return row


# ---- catalogue -------------------------------------------------------------------


@router.get("/integrations/catalogue")
def catalogue_view(user: UserDep) -> dict:
    """Event kinds, connectors (with the settings each needs) and cloud on-ramp adapters."""
    return {
        "events": catalogue.public(),
        "providers": [p.public() for p in providers.all_providers()],
        "onramp_adapters": [a.public() for a in ADAPTERS.values()],
        "live": transport.live_enabled(),
    }


@router.get("/integrations/asyncapi.json")
def asyncapi_view() -> dict:
    """The AsyncAPI 3.0 document for the webhook and CloudEvents catalogue."""
    from .asyncapi import document

    return document()


# ---- integrations ----------------------------------------------------------------


class IntegrationIn(BaseModel):
    provider: str = Field(max_length=40, examples=["pagerduty"])
    name: str = Field(min_length=1, max_length=80, examples=["NOC on-call"])
    customer_id: str | None = Field(default=None, description="Admins only: the organisation it belongs to.")
    carrier_id: str | None = Field(default=None, description="Admins only: a carrier it belongs to.")
    config: dict[str, Any] = Field(default={}, examples=[{"send_changes": True}])
    secrets: dict[str, str] = Field(
        default={}, description="Write-only. Never returned.", examples=[{"routing_key": "R0..."}]
    )
    event_types: list[str] = Field(default=["*"], max_length=40, examples=[["path.*", "node.offline", "sla.breach"]])
    site_ids: list[str] = Field(default=[], max_length=200)
    min_severity: Literal["info", "warning", "critical"] = "info"
    enabled: bool = True

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "provider": "webhook",
                    "name": "Our event bus",
                    "config": {"mode": "structured"},
                    "secrets": {"url": "https://events.example.org/connect"},
                    "event_types": ["*"],
                    "min_severity": "warning",
                }
            ]
        }
    }


class IntegrationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    config: dict[str, Any] | None = None
    secrets: dict[str, str] | None = Field(default=None, description="Only the fields given are replaced.")
    event_types: list[str] | None = Field(default=None, max_length=40)
    site_ids: list[str] | None = Field(default=None, max_length=200)
    min_severity: Literal["info", "warning", "critical"] | None = None
    enabled: bool | None = None


def _check_sites(conn, customer_id: Any, site_ids: list[str]) -> list[str]:
    if not site_ids:
        return []
    if customer_id is None:
        raise HTTPException(400, "Only an organisation's integrations can be limited to sites.")
    found = conn.execute(
        "SELECT id FROM sites WHERE customer_id = %s AND id::text = ANY(%s)", (customer_id, site_ids)
    ).fetchall()
    if len(found) != len(set(site_ids)):
        raise HTTPException(400, "One or more sites are not yours.")
    return [str(r["id"]) for r in found]


def _encrypt(values: dict) -> str:
    try:
        return transport.encrypt(values)
    except VaultError as e:
        raise HTTPException(400, str(e)) from None


def create_integration(conn, user, body: IntegrationIn, origin: str = "api", platform: str = "") -> tuple[dict, str]:
    p = providers.get(body.provider)
    if p is None:
        raise HTTPException(400, f"Unknown provider {body.provider}.")
    customer_id, carrier_id = body.customer_id, body.carrier_id
    if user.role == "customer":
        customer_id, carrier_id = user.customer_id, None
    elif user.role == "carrier":
        customer_id, carrier_id = None, user.carrier_id
    elif user.role != "admin":
        raise HTTPException(403, "Not available for this account.")
    if bool(customer_id) == bool(carrier_id):
        raise HTTPException(400, "An integration belongs to one organisation or one carrier.")
    owner = "customer" if customer_id else "carrier"
    if owner not in p.owners:
        raise HTTPException(400, f"{p.name} is for organisations only.")
    bad = catalogue.valid_patterns(body.event_types)
    if bad:
        raise HTTPException(400, f"Unknown event kinds: {', '.join(bad)}.")
    try:
        config = p.validate(body.config)
        secrets = p.validate_secrets(body.secrets)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    shown_secret = ""
    if body.provider in ("webhook",) and not secrets.get("signing_secret"):
        secrets["signing_secret"] = shown_secret = cloudevents.new_secret()
    if "url" in secrets:
        import urllib.parse

        config["host"] = urllib.parse.urlsplit(secrets["url"]).hostname or ""
    n = conn.execute(
        "SELECT count(*) AS n FROM connect_integrations WHERE customer_id IS NOT DISTINCT FROM %s"
        " AND carrier_id IS NOT DISTINCT FROM %s",
        (customer_id, carrier_id),
    ).fetchone()["n"]
    if n >= MAX_INTEGRATIONS:
        raise HTTPException(400, f"Up to {MAX_INTEGRATIONS} integrations each. Delete one first.")
    sites = _check_sites(conn, customer_id, body.site_ids)
    row = conn.execute(
        """INSERT INTO connect_integrations (customer_id, carrier_id, provider, name, enabled, config,
                                            secret_ciphertext, secret_fields, event_types, site_ids, min_severity,
                                            origin, platform, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            carrier_id,
            p.key,
            body.name,
            body.enabled,
            Jsonb(config),
            _encrypt(secrets),
            sorted(secrets),
            ([] if not p.receives_events else body.event_types),
            sites,
            body.min_severity,
            origin,
            platform,
            user.actor,
        ),
    ).fetchone()
    audit.record(
        conn,
        user.actor,
        "integration.create",
        body.name,
        customer_id,
        {"id": row["id"], "provider": p.key, "origin": origin},
    )
    return row, shown_secret


def _desired(conn, row: dict) -> None:
    """IPFIX export lives in desired state: new versions for the organisation's nodes."""
    if row["provider"] == "ipfix" and row["customer_id"]:
        from .. import desired

        desired.refresh(conn, row["customer_id"])


@router.get("/integrations")
def list_integrations(user: UserDep, customer_id: str | None = None, carrier_id: str | None = None) -> list[dict]:
    where, args = _owner_where(user, customer_id, carrier_id)
    with db.tx() as conn:
        rows = conn.execute(f"SELECT * FROM connect_integrations WHERE {where} ORDER BY id", args).fetchall()
    return [public(r) for r in rows]


@router.post("/integrations", status_code=201)
def create(body: IntegrationIn, user: UserDep) -> dict:
    """Add a connector or webhook. Secrets are write-only; a webhook's signing secret is shown once here."""
    with db.tx() as conn:
        row, shown = create_integration(conn, user, body)
        _desired(conn, row)
    out = public(row)
    if shown:
        out["signing_secret"] = shown
    return out


@router.get("/integrations/{integration_id}")
def get_integration(integration_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        return public(_get(conn, user, integration_id))


@router.patch("/integrations/{integration_id}")
def update_integration(integration_id: int, body: IntegrationPatch, user: UserDep) -> dict:
    with db.tx() as conn:
        row = _get(conn, user, integration_id, lock=True)
        p = providers.get(row["provider"])
        changes: dict[str, Any] = {}
        if body.name is not None:
            changes["name"] = body.name
        if body.enabled is not None:
            changes["enabled"] = body.enabled
        if body.min_severity is not None:
            changes["min_severity"] = body.min_severity
        if body.event_types is not None:
            bad = catalogue.valid_patterns(body.event_types)
            if bad:
                raise HTTPException(400, f"Unknown event kinds: {', '.join(bad)}.")
            changes["event_types"] = body.event_types if p is None or p.receives_events else []
        if body.site_ids is not None:
            changes["site_ids"] = _check_sites(conn, row["customer_id"], body.site_ids)
        try:
            if body.config is not None and p is not None:
                changes["config"] = Jsonb(
                    {
                        **{k: v for k, v in row["config"].items() if k == "host"},
                        **p.validate({**row["config"], **body.config}),
                    }
                )
            if body.secrets is not None and p is not None:
                current = transport.decrypt(row["secret_ciphertext"])
                given = p.validate_secrets(body.secrets)
                merged = {**current, **given}
                for k, v in body.secrets.items():
                    if v == "":
                        merged.pop(k, None)
                changes["secret_ciphertext"] = _encrypt(merged)
                changes["secret_fields"] = sorted(merged)
        except VaultError as e:
            raise HTTPException(400, str(e)) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if not changes:
            return public(row)
        sets = ", ".join(f"{k} = %({k})s" + ("::uuid[]" if k == "site_ids" else "") for k in changes)
        row = conn.execute(
            f"UPDATE connect_integrations SET {sets}, updated_at = now() WHERE id = %(id)s RETURNING *",
            {**changes, "id": integration_id},
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "integration.update",
            row["name"],
            row["customer_id"],
            {"id": row["id"], "fields": sorted(changes)},
        )
        _desired(conn, row)
    return public(row)


@router.delete("/integrations/{integration_id}", status_code=204)
def delete_integration(integration_id: int, user: UserDep) -> None:
    with db.tx() as conn:
        row = _get(conn, user, integration_id, lock=True)
        conn.execute("DELETE FROM connect_integrations WHERE id = %s", (integration_id,))
        audit.record(conn, user.actor, "integration.delete", row["name"], row["customer_id"], {"id": row["id"]})
        _desired(conn, row)


@router.post("/integrations/{integration_id}/test")
def test_send(integration_id: int, user: UserDep) -> dict:
    """Send a test.ping now and return the delivery (simulated until the integration is live)."""
    with db.tx() as conn:
        row = _get(conn, user, integration_id)
        p = providers.get(row["provider"])
        if p is not None and not p.receives_events:
            how = "use Sync now" if p.key == "netbox" else "your sites' agents carry it out"
            raise HTTPException(400, f"{p.name} takes no events; {how}.")
        stamp = dt.datetime.now(dt.UTC)
        eid = publish.emit(
            conn,
            "test.ping",
            customer_id=row["customer_id"],
            carrier_ids=[row["carrier_id"]] if row["carrier_id"] else [],
            data={"message": "This is a test from ExaCarib Connect.", "summary": "Test from ExaCarib Connect."},
            event_id=f"test-{integration_id}-{int(stamp.timestamp() * 1000)}",
            time=stamp,
            test_integration=integration_id,
        )
        d = conn.execute(
            "SELECT id FROM connect_deliveries WHERE integration_id = %s AND event_id = %s", (integration_id, eid)
        ).fetchone()
        delivery.attempt(conn, d["id"], final=True)
        out = conn.execute("SELECT * FROM connect_deliveries WHERE id = %s", (d["id"],)).fetchone()
    return out


@router.get("/integrations/{integration_id}/deliveries")
def deliveries(integration_id: int, user: UserDep, limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """The delivery log, newest first. Requests are shown with every secret replaced by [secret]."""
    with db.tx() as conn:
        _get(conn, user, integration_id)
        return conn.execute(
            "SELECT * FROM connect_deliveries WHERE integration_id = %s ORDER BY id DESC LIMIT %s",
            (integration_id, limit),
        ).fetchall()


@router.get("/integrations/{integration_id}/deliveries/{delivery_id}")
def get_delivery(integration_id: int, delivery_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        _get(conn, user, integration_id)
        row = conn.execute(
            "SELECT d.*, e.data AS event_data, e.type AS event_ce_type FROM connect_deliveries d"
            " JOIN connect_events e ON e.id = d.event_id WHERE d.id = %s AND d.integration_id = %s",
            (delivery_id, integration_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Delivery not found.")
    return row


@router.post("/integrations/{integration_id}/deliveries/{delivery_id}/retry")
def retry_delivery(integration_id: int, delivery_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        _get(conn, user, integration_id)
        row = conn.execute(
            "UPDATE connect_deliveries SET status = 'pending', last_error = '' WHERE id = %s AND integration_id = %s"
            " AND status = 'failed' RETURNING id",
            (delivery_id, integration_id),
        ).fetchone()
        if row is None:
            raise HTTPException(400, "Only a failed delivery can be retried.")
        from ..commai import jobs

        jobs.enqueue(conn, "integrations.deliver", {"delivery_id": delivery_id}, dedupe_key=None)
    return {"queued": True}


@router.get("/integrations/{integration_id}/outbox")
def outbox(integration_id: int, user: UserDep, limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
    """What a simulated integration would have sent (secrets replaced)."""
    with db.tx() as conn:
        _get(conn, user, integration_id)
        return conn.execute(
            "SELECT id, provider, method, target, body, at FROM connect_sim_outbox WHERE integration_id = %s"
            " ORDER BY id DESC LIMIT %s",
            (integration_id, limit),
        ).fetchall()


@router.post("/integrations/{integration_id}/sync")
def sync_now(integration_id: int, user: UserDep) -> dict:
    """Run an inventory sync (NetBox) now."""
    with db.tx() as conn:
        row = _get(conn, user, integration_id, lock=True)
        if row["provider"] != "netbox":
            raise HTTPException(400, "Only inventory integrations sync.")
        return run_sync(conn, row, user.actor)


def run_sync(conn, row: dict, actor: str) -> dict:
    p = providers.get("netbox")
    try:
        secrets = transport.decrypt(row["secret_ciphertext"])
    except VaultError as e:
        raise HTTPException(400, str(e)) from None
    live = delivery.mode_of(row, secrets) == "live"
    http = transport.Transport(conn, row["id"], "netbox", live, secrets, p.simulate)
    try:
        out = netbox.sync(conn, row, http, secrets, actor)
        status = f"synced ({'live' if live else 'simulated'})"
    except (providers.ProviderError, transport.Unreachable) as e:
        out, status = {"error": http.redact(str(e))}, f"failed: {http.redact(str(e))[:180]}"
    conn.execute(
        "UPDATE connect_integrations SET last_status = %s, last_delivery_at = now(), state = %s WHERE id = %s",
        (status, Jsonb({"last_sync": out}), row["id"]),
    )
    return {"mode": "live" if live else "simulated", **out}


# ---- REST hooks (Zapier, Make, n8n) ---------------------------------------------


class HookIn(BaseModel):
    target_url: str | None = Field(default=None, examples=["https://hooks.zapier.com/hooks/standard/123/abc/"])
    hookUrl: str | None = Field(default=None, description="Accepted as an alias of target_url (Zapier).")
    event: str | None = Field(default=None, examples=["path.down"])
    events: list[str] | None = None
    platform: Literal["zapier", "make", "n8n", "other"] = "other"
    min_severity: Literal["info", "warning", "critical"] = "info"


@router.post("/hooks", status_code=201)
def subscribe_hook(body: HookIn, user: UserDep) -> dict:
    """REST-hook subscribe: Zapier, Make and n8n call this when a flow is switched on."""
    url = body.target_url or body.hookUrl
    if not url:
        raise HTTPException(400, "Give target_url.")
    events = body.events or ([body.event] if body.event else ["*"])
    with db.tx() as conn:
        row, shown = create_integration(
            conn,
            user,
            IntegrationIn(
                provider="webhook",
                name=f"{body.platform.capitalize()} hook"[:80],
                config={"mode": "structured"},
                secrets={"url": url},
                event_types=events,
                min_severity=body.min_severity,
            ),
            origin="resthook",
            platform=body.platform,
        )
    return {"id": row["id"], "events": events, "signing_secret": shown, "mode": delivery.mode_of(row)}


@router.delete("/hooks/{hook_id}", status_code=204)
def unsubscribe_hook(hook_id: int, user: UserDep) -> None:
    """REST-hook unsubscribe, when the flow is switched off."""
    with db.tx() as conn:
        row = _get(conn, user, hook_id, lock=True)
        if row["origin"] != "resthook":
            raise HTTPException(404, "Hook not found.")
        conn.execute("DELETE FROM connect_integrations WHERE id = %s", (hook_id,))
        audit.record(conn, user.actor, "integration.delete", row["name"], row["customer_id"], {"id": hook_id})


@router.get("/hooks/me")
def hook_me(user: UserDep) -> dict:
    """For the platforms' connection test: who this key acts for."""
    with db.tx() as conn:
        org = (
            conn.execute("SELECT name FROM customers WHERE id = %s", (user.customer_id,)).fetchone()
            if user.customer_id
            else None
        )
    return {"email": user.email, "role": user.role, "organisation": org["name"] if org else None}


@router.get("/hooks/sample")
def hook_sample(user: UserDep, event: str = "path.down") -> list[dict]:
    """Sample events in the delivered shape, for setting up a flow."""
    kind = catalogue.KINDS.get(event)
    if kind is None:
        raise HTTPException(404, "Unknown event kind.")
    ev = {
        "id": f"sample-{event}",
        "type": catalogue.type_of(event),
        "source": catalogue.SOURCE,
        "subject": "",
        "time": dt.datetime.now(dt.UTC),
        "customer_id": user.customer_id,
        "severity": kind.severity,
        "action": kind.action,
        "dedup_key": "",
        "data": {**kind.example, "summary": kind.title, "example": True},
    }
    return [cloudevents.event(ev)]


# ---- admin status -----------------------------------------------------------------

ENV_NEEDED = {
    "EXA_INTEGRATIONS_LIVE": "Lets integrations with credentials send for real.",
    "EXA_SECRETS_KEY": "Encrypts integration secrets (shared with CommAI).",
    "EXA_PUBLIC_URL": "Links back to the portal in alerts.",
    "EXA_IANA_PEN": "ExaCarib's IANA enterprise number for syslog and SNMP (32473, for documentation, until set).",
}


@router.get("/admin/integrations/status")
def admin_status(user: AdminDep) -> dict:
    """Configured or not, never values: platform switches, connectors in use, on-ramp adapters and carrier feeds."""
    import os

    with db.tx() as conn:
        per = conn.execute(
            """SELECT provider, count(*) AS n, count(*) FILTER (WHERE enabled) AS enabled,
                      max(last_delivery_at) AS last_delivery
               FROM connect_integrations GROUP BY provider"""
        ).fetchall()
        rows = conn.execute("SELECT * FROM connect_integrations WHERE enabled").fetchall()
        feeds = conn.execute(
            """SELECT c.id, c.name, count(n.id) AS notices,
                      count(n.id) FILTER (WHERE n.status NOT IN ('resolved', 'cancelled', 'closed')) AS open,
                      max(n.created_at) AS last_notice,
                      (SELECT count(*) FROM connect_integrations i WHERE i.carrier_id = c.id AND i.enabled) AS outbound,
                      (SELECT count(*) FROM users u WHERE u.carrier_id = c.id) AS accounts
               FROM carriers c LEFT JOIN connect_notices n ON n.carrier_id = c.id
               GROUP BY c.id, c.name ORDER BY c.name"""
        ).fetchall()
    live = {p: 0 for p in (r["provider"] for r in rows)}
    for r in rows:
        if delivery.mode_of(r) == "live":
            live[r["provider"]] += 1
    used = {r["provider"]: r for r in per}
    return {
        "platform": [{"name": k, "purpose": v, "configured": bool(os.environ.get(k))} for k, v in ENV_NEEDED.items()],
        "live": transport.live_enabled(),
        "secure_storage": transport.vault.configured(),
        "providers": [
            {
                "key": p.key,
                "name": p.name,
                "category": p.category,
                "in_use": int(used[p.key]["n"]) if p.key in used else 0,
                "enabled": int(used[p.key]["enabled"]) if p.key in used else 0,
                "live": live.get(p.key, 0),
                "last_delivery": used[p.key]["last_delivery"] if p.key in used else None,
                "live_needs": p.live_needs,
            }
            for p in providers.all_providers()
        ],
        "onramp_adapters": [
            {
                "key": a.key,
                "name": a.name,
                "configured": a.configured(),
                "mode": "live" if a.live() else "simulated",
                "env": list(a.env),
                "live_needs": a.live_needs,
            }
            for a in ADAPTERS.values()
        ],
        "carrier_feeds": feeds,
    }


# ---- Prometheus / OpenMetrics ------------------------------------------------------


@router.get("/metrics", response_class=PlainTextResponse)
def prometheus(
    user: UserDep, accept: Annotated[str | None, Header()] = None, customer_id: str | None = None
) -> Response:
    """Prometheus text format (or OpenMetrics with Accept: application/openmetrics-text). Authenticate with an API
    key (scope `metrics` or `connect`) as a bearer token. A key sees only its own organisation, a carrier key only
    its own links."""
    if user.role == "customer":
        scope = {"customer_id": user.customer_id}
    elif user.role == "carrier" and user.carrier_id:
        scope = {"carrier_id": user.carrier_id}
    elif user.role == "admin":
        scope = {"customer_id": customer_id}
    else:
        raise HTTPException(403, "Not available for this account.")
    with db.tx() as conn:
        fams = metrics.collect(conn, **scope)
    om = "application/openmetrics-text" in (accept or "")
    body = metrics.render(fams, openmetrics=om)
    ctype = (
        "application/openmetrics-text; version=1.0.0; charset=utf-8"
        if om
        else "text/plain; version=0.0.4; charset=utf-8"
    )
    return Response(body, media_type=ctype)


# ---- RESTCONF ---------------------------------------------------------------------


def _yang(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=restconf.MEDIA)


def _rc_error(tag: str, msg: str, status: int) -> JSONResponse:
    return _yang(
        {"ietf-restconf:errors": {"error": [{"error-type": "application", "error-tag": tag, "error-message": msg}]}},
        status,
    )


def _rc_scope(user) -> Any:
    if user.role == "admin":
        return None
    if user.role == "customer":
        return user.customer_id
    raise HTTPException(403, "Not available for this account.")


@router.get("/restconf", tags=["restconf"])
def restconf_root(user: UserDep) -> JSONResponse:
    return _yang({"ietf-restconf:restconf": {"data": {}, "operations": {}, "yang-library-version": "2019-01-04"}})


@router.get("/restconf/yang-library-version", tags=["restconf"])
def restconf_library_version(user: UserDep) -> JSONResponse:
    return _yang({"ietf-restconf:yang-library-version": "2019-01-04"})


@router.get("/restconf/modules/exacarib-connect.yang", tags=["restconf"], response_class=PlainTextResponse)
def restconf_module(user: UserDep) -> Response:
    return Response(restconf.yang_text(), media_type="application/yang")


@router.get("/restconf/data/ietf-yang-library:yang-library", tags=["restconf"])
def restconf_yang_library(user: UserDep) -> JSONResponse:
    return _yang(restconf.library())


@router.get("/restconf/data/{path:path}", tags=["restconf"])
def restconf_data(path: str, user: UserDep, accept: Annotated[str | None, Header()] = None) -> JSONResponse:
    """Read-only data under exacarib-connect:connect (RFC 8040 §3.5), JSON encoding (RFC 7951)."""
    if accept and "xml" in accept and "json" not in accept:
        return _rc_error("operation-not-supported", "Only application/yang-data+json is served.", 406)
    scope = _rc_scope(user)
    with db.tx() as conn:
        doc = restconf.tree(conn, scope)
    try:
        return _yang(restconf.select(doc, path))
    except restconf.NotFound:
        return _rc_error("invalid-value", f"No data at {path}.", 404)


@router.api_route("/restconf/data/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def restconf_write(path: str, user: UserDep) -> JSONResponse:
    return _rc_error("access-denied", "This RESTCONF interface is read-only; use the REST API to make changes.", 405)


# ---- carrier notices --------------------------------------------------------------


class NoticeIn(BaseModel):
    kind: Literal["maintenance", "fault"] = Field(examples=["maintenance"])
    title: str = Field(min_length=1, max_length=200, examples=["Core router upgrade, Kingston POP"])
    description: str = Field(default="", max_length=4000)
    link_ids: list[str] = Field(min_length=1, max_length=500)
    severity: Literal["info", "warning", "critical"] | None = None
    starts_at: dt.datetime | None = Field(default=None, examples=["2026-10-10T02:00:00Z"])
    ends_at: dt.datetime | None = Field(default=None, examples=["2026-10-10T04:00:00Z"])
    move_traffic: bool = Field(default=True, description="Move classes off the links when the window starts.")
    external_id: str = Field(default="", max_length=120, description="Your own reference, unique per carrier.")
    carrier_id: str | None = Field(default=None, description="Admins only.")


class NoticePatch(BaseModel):
    status: Literal["scheduled", "open", "in_progress", "resolved", "cancelled", "closed"] | None = None
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    severity: Literal["info", "warning", "critical"] | None = None
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    move_traffic: bool | None = None


def carrier_of(user, given: str | None = None) -> Any:
    if user.role == "carrier" and user.carrier_id:
        return user.carrier_id
    if user.role == "admin":
        if not given:
            raise HTTPException(400, "Say which carrier (carrier_id).")
        return given
    raise HTTPException(403, "Only carriers post notices.")


def _notice_error(e: notices.NoticeError) -> HTTPException:
    return HTTPException(e.code, str(e))


@router.get("/carrier/notices")
def carrier_notices(user: UserDep, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    if user.role not in ("carrier", "admin"):
        raise HTTPException(403, "Carriers only.")
    with db.tx() as conn:
        return notices.list_for(conn, user, limit=limit)


@router.post("/carrier/notices", status_code=201)
def post_notice(body: NoticeIn, user: UserDep) -> dict:
    """Post planned maintenance or a fault on your own links. Affected sites see it, and classes move off the links
    when a maintenance window starts."""
    with db.tx() as conn:
        try:
            row = notices.create(
                conn,
                carrier_id=carrier_of(user, body.carrier_id),
                actor=user.actor,
                source="api" if user.via == "key" else "portal",
                **body.model_dump(exclude={"carrier_id"}),
            )
        except notices.NoticeError as e:
            raise _notice_error(e) from None
        return notices.list_for(conn, user, row["id"])[0]


@router.get("/carrier/notices/{notice_id}")
def carrier_notice(notice_id: int, user: UserDep) -> dict:
    if user.role not in ("carrier", "admin"):
        raise HTTPException(403, "Carriers only.")
    with db.tx() as conn:
        rows = notices.list_for(conn, user, notice_id)
    if not rows:
        raise HTTPException(404, "Notice not found.")
    return rows[0]


@router.patch("/carrier/notices/{notice_id}")
def patch_notice(notice_id: int, body: NoticePatch, user: UserDep) -> dict:
    if user.role not in ("carrier", "admin"):
        raise HTTPException(403, "Carriers only.")
    with db.tx() as conn:
        n = conn.execute(
            "SELECT * FROM connect_notices WHERE id = %s AND kind <> 'trouble'"
            " AND (%s::uuid IS NULL OR carrier_id = %s)"
            " FOR UPDATE",
            (
                notice_id,
                user.carrier_id if user.role == "carrier" else None,
                user.carrier_id if user.role == "carrier" else None,
            ),
        ).fetchone()
        if n is None:
            raise HTTPException(404, "Notice not found.")
        try:
            notices.update(conn, n, body.model_dump(exclude_none=True), user.actor)
        except notices.NoticeError as e:
            raise _notice_error(e) from None
        return notices.list_for(conn, user, notice_id)[0]


@router.get("/notices")
def customer_notices(user: UserDep, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    """Carrier notices that touch your links (only your links are listed in each)."""
    with db.tx() as conn:
        return notices.list_for(conn, user, limit=limit)


@router.get("/notices/{notice_id}")
def customer_notice(notice_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        rows = notices.list_for(conn, user, notice_id)
    if not rows:
        raise HTTPException(404, "Notice not found.")
    return rows[0]


# ---- cloud on-ramps ----------------------------------------------------------------


def _customer(user, customer_id: str) -> None:
    from ..api.deps import check_customer

    check_customer(user, customer_id)


@router.get("/onramps/adapters")
def onramp_adapters(user: UserDep) -> list[dict]:
    return [a.public() for a in ADAPTERS.values()]


@router.get("/customers/{customer_id}/onramps")
def list_onramps(customer_id: str, user: UserDep) -> list[dict]:
    _customer(user, customer_id)
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM connect_onramps WHERE customer_id = %s ORDER BY id DESC", (customer_id,)
        ).fetchall()
    return [onramps.public(r) for r in rows]


class OnrampIn(BaseModel):
    provider: Literal["aws_dx", "azure_er", "gcp_pi", "megaport"]
    name: str = Field(default="", max_length=80)
    site: str | None = Field(default=None, description="Site name or id the on-ramp serves.")
    bandwidth_mbps: int = Field(examples=[50])
    aws_account_id: str | None = Field(default=None, examples=["123456789012"])
    region: str | None = None
    circuit_id: str | None = None
    service_key: str | None = None
    pairing_key: str | None = None
    b_end_product_uid: str | None = None


@router.post("/customers/{customer_id}/onramps", status_code=201)
def create_onramp(customer_id: str, body: OnrampIn, user: UserDep) -> dict:
    """Order a dedicated cloud on-ramp now (simulated until the provider is connected)."""
    _customer(user, customer_id)
    with db.tx() as conn:
        try:
            o = onramps.create(conn, customer_id, body.model_dump(exclude_none=True), user.actor)
        except OnrampError as e:
            raise HTTPException(400, str(e)) from None
    return onramps.public(o)


def _onramp(conn, user, customer_id: str, onramp_id: int) -> dict:
    _customer(user, customer_id)
    o = conn.execute(
        "SELECT * FROM connect_onramps WHERE id = %s AND customer_id = %s FOR UPDATE", (onramp_id, customer_id)
    ).fetchone()
    if o is None:
        raise HTTPException(404, "On-ramp not found.")
    return o


@router.get("/customers/{customer_id}/onramps/{onramp_id}")
def get_onramp(customer_id: str, onramp_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        return onramps.public(_onramp(conn, user, customer_id, onramp_id))


@router.post("/customers/{customer_id}/onramps/{onramp_id}/refresh")
def refresh_onramp(customer_id: str, onramp_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        try:
            return onramps.public(onramps.refresh(conn, _onramp(conn, user, customer_id, onramp_id)))
        except OnrampError as e:
            raise HTTPException(502, str(e)) from None


@router.delete("/customers/{customer_id}/onramps/{onramp_id}")
def delete_onramp(customer_id: str, onramp_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        try:
            return onramps.public(onramps.delete(conn, _onramp(conn, user, customer_id, onramp_id), user.actor))
        except OnrampError as e:
            raise HTTPException(502, str(e)) from None


def host_meta(request: Request) -> Response:
    """RFC 8040 §3.1 root discovery."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0">\n'
        '  <Link rel="restconf" href="/api/v1/restconf"/>\n'
        "</XRD>\n"
    )
    return Response(xml, media_type="application/xrd+xml")
