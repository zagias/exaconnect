"""Integration catalogue and standard interfaces API (ADR 0028).

Signed in, under /api/v1/commai/customers/{customer_id}: the catalogue grouped
by category, credentials entry, inbound webhooks, the business's own REST
apps (OpenAPI import), outbound webhook formats, the iCalendar feed, vCard
and CSV exchange, and IMAP/SMTP mailboxes.

Public: inbound webhooks, app webhooks, the iCalendar feed and invites, and
the published OpenAPI and AsyncAPI documents.

Reads need commai:read; changes need commai:admin and a business admin. Every
write is audited. Secrets are accepted through secure entry and never returned.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access
from ..automation import catalogue, integrations, vault
from ..connectors import kit
from ..standards import inbound, webhooks_std
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: integrations catalogue"])
public = APIRouter(tags=["commai: integrations catalogue"])


@contextmanager
def _errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except integrations.SetupError as e:
            raise HTTPException(e.code, str(e)) from e
        except vault.VaultError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e


def _admin(conn, user, customer_id: str) -> None:
    access.require_business_admin(user)
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change integration settings.")


def _base(request: Request) -> str:
    return os.environ.get("EXA_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


# ==== the catalogue ===================================================================


@router.get("/integration-catalogue")
def get_catalogue(customer_id: str, user: UserDep) -> dict:
    """Every app by category with its actions, status and what ExaCarib still needs;
    the standard interfaces; apps reached through a standard."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return catalogue.grouped(conn, customer_id)


# ==== inbound webhooks ================================================================


class HookIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    event_type: str = Field(default="", max_length=120, pattern=r"^[A-Za-z0-9._:/-]*$")


class HookPatch(BaseModel):
    active: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)


@router.get("/inbound-hooks")
def list_hooks(customer_id: str, request: Request, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM commai_inbound_hooks WHERE customer_id = %s ORDER BY kind, created_at", (customer_id,)
        ).fetchall()
    return [inbound.public(r, _base(request)) for r in rows]


@router.post("/inbound-hooks", status_code=201)
def create_hook(customer_id: str, body: HookIn, request: Request, user: UserDep) -> dict:
    """A signed inbound webhook that starts workflows. The secret is shown once."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = inbound.create(conn, customer_id, body.name, body.event_type, user.actor)
        audit.record(conn, user.actor, "commai.inbound_hook.create", body.name, customer_id)
    return inbound.public(row, _base(request), secret=True)


@router.patch("/inbound-hooks/{hook_id}")
def update_hook(customer_id: str, hook_id: str, body: HookPatch, request: Request, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = conn.execute(
            """UPDATE commai_inbound_hooks SET active = COALESCE(%s, active), name = COALESCE(%s, name)
               WHERE id = %s AND customer_id = %s RETURNING *""",
            (body.active, body.name, hook_id, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Webhook not found.")
        audit.record(
            conn, user.actor, "commai.inbound_hook.update", row["name"], customer_id, body.model_dump(exclude_none=True)
        )
    return inbound.public(row, _base(request))


@router.delete("/inbound-hooks/{hook_id}", status_code=204)
def delete_hook(customer_id: str, hook_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = conn.execute(
            "DELETE FROM commai_inbound_hooks WHERE id = %s AND customer_id = %s RETURNING name", (hook_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Webhook not found.")
        audit.record(conn, user.actor, "commai.inbound_hook.delete", row["name"], customer_id)


@router.post("/integrations/{app}/hook")
def app_hook(customer_id: str, app: str, request: Request, user: UserDep) -> dict:
    """The address and signing secret to give the app for its change notifications
    (Calendly signing key, Graph clientState, Pipedrive webhook password, Zoho
    channel token, Gmail push token). Shown to an admin only."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        c = integrations._connector(app)
        if not kit.receives_webhooks(c):
            raise HTTPException(409, f"{c.label} does not send change notifications.")
        row = inbound.app_hook(conn, customer_id, app, user.actor)
        audit.record(conn, user.actor, "commai.app_hook.show", app, customer_id)
    return inbound.public(row, _base(request), secret=True)


def _hook_response(token: str, request_headers: dict, body: bytes, query: dict) -> Response:
    with db.tx() as conn:
        try:
            status, ctype, text = inbound.receive(conn, token, request_headers, body, query)
        except inbound.HookError as e:
            # Kept: the rejection is counted on the hook (the transaction commits).
            status, ctype, text = e.code, "application/json", json.dumps({"detail": str(e)})
    return Response(text, status_code=status, media_type=ctype)


@public.post("/hooks/{token}", include_in_schema=True, tags=["commai: inbound webhooks"])
async def receive_hook(token: str, request: Request) -> Response:
    """A signed delivery to a generic inbound webhook (Standard Webhooks or
    ExaCarib v1 signature; CloudEvents or any JSON body)."""
    body = await request.body()
    return _hook_response(token, dict(request.headers), body, dict(request.query_params))


@public.post("/integration-hooks/{token}", include_in_schema=True, tags=["commai: inbound webhooks"])
async def receive_app_hook(token: str, request: Request) -> Response:
    """An app's change notification, checked with the app's own signature."""
    body = await request.body()
    q = dict(request.query_params)
    q["__url__"] = str(request.url)
    return _hook_response(token, dict(request.headers), body, q)


# ==== outbound webhook formats ========================================================


class FormatIn(BaseModel):
    format: Literal["exacarib", "cloudevents"] = "exacarib"
    standard_headers: bool = False


@router.put("/webhooks/{endpoint_id}/format")
def set_webhook_format(customer_id: str, endpoint_id: str, body: FormatIn, user: UserDep) -> dict:
    """ExaCarib v1 or CloudEvents 1.0 bodies, and Standard Webhooks headers, per endpoint.
    Returns the same secret in the Standard Webhooks whsec_ form."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = conn.execute(
            """UPDATE webhook_endpoints SET format = %s, standard_headers = %s WHERE id = %s AND customer_id = %s
               RETURNING id, url, format, standard_headers, secret""",
            (body.format, body.standard_headers, endpoint_id, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Webhook not found.")
        audit.record(conn, user.actor, "commai.webhook.format", row["url"], customer_id, body.model_dump())
    out = {k: row[k] for k in ("id", "url", "format", "standard_headers")}
    if body.standard_headers:
        out["standard_secret"] = webhooks_std.standard_secret(row["secret"])
    return out


class CredentialsIn(BaseModel):
    credentials: dict[str, str] = Field(max_length=10)


@router.post("/integrations/{app}/credentials")
def enter_credentials(customer_id: str, app: str, body: CredentialsIn, user: UserDep) -> dict:
    """Secure entry of an app's credentials (API key, user name and app password).
    Checked with one call, stored encrypted and never shown again."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.enter_credentials(conn, customer_id, app, body.credentials, user.actor)
        audit.record(conn, user.actor, "commai.integration.credentials", app, customer_id)  # never the values
    return row
