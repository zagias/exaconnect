"""Integration catalogue and standard interfaces API (ADR 0034).

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
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access
from ..automation import catalogue, integrations, vault
from ..channels import mailbox
from ..connectors import kit, rest_generic
from ..standards import api_docs, exchange, ical_feed, inbound, openapi_import, webhooks_std
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


# ==== a business's own mailbox (IMAP and SMTP) ==========================================


class MailboxIn(BaseModel):
    imap_host: str = Field(min_length=3, max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_security: Literal["ssl", "starttls"] = "ssl"
    smtp_host: str = Field(min_length=3, max_length=253)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls"] = "starttls"
    username: str = Field(min_length=1, max_length=320)
    password: str | None = Field(default=None, max_length=1000, description="Stored encrypted; never shown again")
    folder: str = Field(default="INBOX", max_length=200, pattern=r'^[^"\r\n]+$')
    mode: Literal["poll", "idle"] = "poll"
    poll_seconds: int = Field(default=60, ge=30, le=3600)


@contextmanager
def _mailbox_errors() -> Iterator[None]:
    with _errors():
        try:
            yield
        except mailbox.MailboxError as e:
            raise HTTPException(422 if e.cause == "input" else 409, f"{e} ({e.cause})") from e


def _mail_account(conn, customer_id: str, account_id: str) -> dict:
    a = conn.execute(
        "SELECT * FROM channel_accounts WHERE id::text = %s AND customer_id = %s", (account_id, customer_id)
    ).fetchone()
    if a is None:
        raise HTTPException(404, "Account not found.")
    return a


def _mailbox_row(conn, customer_id: str, account_id: str) -> dict:
    a = _mail_account(conn, customer_id, account_id)
    row = mailbox.get(conn, customer_id, a["id"])
    if row is None:
        raise HTTPException(404, "Set up the mailbox first.")
    if not mailbox.live(conn, customer_id):
        raise HTTPException(409, "ExaCarib has not switched business mailboxes on yet.")
    return row


@router.get("/channel-accounts/{account_id}/mailbox")
def get_mailbox(customer_id: str, account_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        a = _mail_account(conn, customer_id, account_id)
        return {
            "mailbox": mailbox.out(mailbox.get(conn, customer_id, a["id"])),
            "live": mailbox.live(conn, customer_id),
        }


@router.put("/channel-accounts/{account_id}/mailbox")
def put_mailbox(customer_id: str, account_id: str, body: MailboxIn, user: UserDep) -> dict:
    """Set the IMAP and SMTP servers of an email account with the mailbox provider.
    Until ExaCarib switches the mailbox feature on, nothing is read and replies
    go to the simulated outbox."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _mailbox_errors():
        _admin(conn, user, customer_id)
        a = _mail_account(conn, customer_id, account_id)
        row = mailbox.configure(conn, a, body.model_dump(exclude={"password"}), body.password, user.actor)
        audit.record(
            conn,
            user.actor,
            "commai.mailbox.configure",
            a["address"],
            customer_id,
            body.model_dump(exclude={"password"}) | {"password_changed": bool(body.password)},
        )
        return {"mailbox": mailbox.out(row), "live": mailbox.live(conn, customer_id)}


@router.post("/channel-accounts/{account_id}/mailbox/check")
def check_mailbox(customer_id: str, account_id: str, user: UserDep) -> dict:
    """Sign in to IMAP and SMTP; nothing is read or sent."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _mailbox_errors():
        return mailbox.check(conn, _mailbox_row(conn, customer_id, account_id))


@router.post("/channel-accounts/{account_id}/mailbox/poll")
def poll_mailbox(customer_id: str, account_id: str, user: UserDep) -> dict:
    """Read new mail now."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _mailbox_errors():
        return mailbox.poll(conn, _mailbox_row(conn, customer_id, account_id))


# ==== your own REST API, from its OpenAPI document ======================================


class RestAppIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    document: str | dict = Field(description="The OpenAPI 3.0 or 3.1 document, as JSON (or YAML) text or an object")
    base_url: str = Field(default="", max_length=500, description="Defaults to the document's first server")


class RestAppPatch(BaseModel):
    actions: list[dict] | None = None
    auth: dict | None = None
    base_url: str | None = Field(default=None, max_length=500)
    client_secret: str | None = Field(default=None, max_length=2000, description="OAuth client secret; kept encrypted")


def _rest_out(row: dict, full: bool = False) -> dict:
    auth = {k: v for k, v in (row["auth"] or {}).items() if k != "client_secret_ref"}
    out = {
        "id": str(row["id"]),
        "app": rest_generic.app_name(row["id"]),
        "name": row["name"],
        "base_url": row["base_url"],
        "status": row["status"],
        "version": row["version"],
        "actions": row["actions"],
        "auth": auth | {"client_secret_set": bool((row["auth"] or {}).get("client_secret_ref"))},
        "operation_count": len(row["operations"]),
        "approved_by": row["approved_by"],
        "approved_at": row["approved_at"],
        "drafted_by": row["drafted_by"],
    }
    if full:
        out["operations"] = row["operations"]
        out["security_schemes"] = openapi_import.security_schemes(row["document"])
        out["servers"] = openapi_import.servers(row["document"])
        out["title"] = row["document"].get("info", {}).get("title", "")
    return out


def _rest_row(conn, customer_id: str, rest_id: str, lock: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM commai_rest_apps WHERE id::text = %s AND customer_id = %s" + (" FOR UPDATE" if lock else ""),
        (rest_id, customer_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "REST app not found.")
    return row


def _check_base(url: str) -> str:
    from .. import webhooks

    url = url.strip().rstrip("/")
    try:
        webhooks.check_url(url)
    except webhooks.UnsafeURL as e:
        raise HTTPException(422, f"Base address: {e}") from None
    return url


@router.get("/rest-apps")
def list_rest_apps(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM commai_rest_apps WHERE customer_id = %s ORDER BY name", (customer_id,)
        ).fetchall()
    return [_rest_out(r) for r in rows]


@router.post("/rest-apps", status_code=201)
def import_rest_app(customer_id: str, body: RestAppIn, user: UserDep) -> dict:
    """Import an OpenAPI document. Nothing in it runs; its operations are listed
    for you to choose as actions. The app stays a draft until a person approves it."""
    access.check(user, customer_id, "commai:admin")
    try:
        doc = openapi_import.parse(body.document)
        ops = openapi_import.operations(doc)
    except openapi_import.OpenAPIError as e:
        raise HTTPException(422, str(e)) from None
    if not ops:
        raise HTTPException(422, "The document has no operations Jibsy can call.")
    servers = openapi_import.servers(doc)
    base_url = body.base_url or next((s for s in servers if s.startswith("http")), "")
    if not base_url:
        raise HTTPException(422, "Give the API's base address (the document has no absolute server URL).")
    base_url = _check_base(base_url)
    slug = openapi_import._slug(body.name)
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        if conn.execute(
            "SELECT 1 FROM commai_rest_apps WHERE customer_id = %s AND slug = %s", (customer_id, slug)
        ).fetchone():
            raise HTTPException(409, "You already have an API with that name.")
        row = conn.execute(
            """INSERT INTO commai_rest_apps (customer_id, slug, name, base_url, document, operations, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (customer_id, slug, body.name, base_url, Jsonb(doc), Jsonb(ops), user.actor),
        ).fetchone()
        audit.record(conn, user.actor, "commai.rest_app.import", body.name, customer_id, {"operations": len(ops)})
    return _rest_out(row, full=True)


@router.get("/rest-apps/{rest_id}")
def get_rest_app(customer_id: str, rest_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        return _rest_out(_rest_row(conn, customer_id, rest_id), full=True)


@router.post("/rest-apps/{rest_id}/draft")
def draft_rest_actions(customer_id: str, rest_id: str, user: UserDep) -> dict:
    """A suggested first choice of actions with field mapping, made only from the
    document's operations. Nothing is saved: review it, then save it with PUT."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        row = _rest_row(conn, customer_id, rest_id)
    return {"actions": openapi_import.draft_actions(row["operations"]), "drafted_by": "suggestion"}


@router.put("/rest-apps/{rest_id}")
def update_rest_app(customer_id: str, rest_id: str, body: RestAppPatch, user: UserDep) -> dict:
    """Choose operations as actions, map fields, set the sign-in method. Any change
    sends the app back to draft for a person to approve again."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _rest_row(conn, customer_id, rest_id, lock=True)
        ops = row["operations"]
        actions = row["actions"] if body.actions is None else rest_generic.check_actions(ops, body.actions)
        auth = row["auth"] or {}
        if body.auth is not None:
            ref = auth.get("client_secret_ref", "")
            auth = rest_generic.check_auth(row["document"], body.auth)
            if ref and auth["type"] == "oauth2_auth_code":
                auth["client_secret_ref"] = ref
        if body.client_secret:
            if auth.get("type") != "oauth2_auth_code":
                raise HTTPException(422, "A client secret is entered here only for OAuth sign-in with your client.")
            auth["client_secret_ref"] = vault.put(
                conn, customer_id, f"rest:{rest_id}", {"client_secret": body.client_secret},
                auth.get("client_secret_ref", ""),
            )  # fmt: skip
        base_url = row["base_url"] if body.base_url is None else _check_base(body.base_url)
        row = conn.execute(
            """UPDATE commai_rest_apps SET actions = %s, auth = %s, base_url = %s, status = 'draft',
                      version = version + 1, approved_by = '', approved_at = NULL, drafted_by = 'person',
                      updated_at = now() WHERE id = %s RETURNING *""",
            (Jsonb(actions), Jsonb(auth), base_url, row["id"]),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "commai.rest_app.update",
            row["name"],
            customer_id,
            {"actions": [a["name"] for a in actions], "auth": auth.get("type", "none"), "version": row["version"]},
        )
    return _rest_out(row, full=True)


@router.post("/rest-apps/{rest_id}/approve")
def approve_rest_app(customer_id: str, rest_id: str, user: UserDep) -> dict:
    """A person approves the chosen actions; the app then appears in this business's
    catalogue. API keys can't approve."""
    access.check(user, customer_id, "commai:admin")
    if user.via == "key":
        raise HTTPException(403, "A person approves an API's actions, not an API key.")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _rest_row(conn, customer_id, rest_id, lock=True)
        if not row["actions"]:
            raise HTTPException(409, "Choose at least one operation first.")
        rest_generic.check_actions(row["operations"], row["actions"])  # every one still in the document
        if (row["auth"] or {}).get("type") == "oauth2_auth_code" and not row["auth"].get("client_secret_ref"):
            raise HTTPException(409, "Enter your OAuth client secret first.")
        row = conn.execute(
            """UPDATE commai_rest_apps SET status = 'approved', approved_by = %s, approved_at = now(),
                      updated_at = now() WHERE id = %s RETURNING *""",
            (user.actor, row["id"]),
        ).fetchone()
        audit.record(conn, user.actor, "commai.rest_app.approve", row["name"], customer_id, {"version": row["version"]})
    return _rest_out(row)


@router.delete("/rest-apps/{rest_id}", status_code=204)
def delete_rest_app(customer_id: str, rest_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        row = _rest_row(conn, customer_id, rest_id, lock=True)
        app = rest_generic.app_name(row["id"])
        conn.execute("DELETE FROM integration_connections WHERE customer_id = %s AND app = %s", (customer_id, app))
        conn.execute("DELETE FROM commai_rest_apps WHERE id = %s", (row["id"],))
        audit.record(conn, user.actor, "commai.rest_app.delete", row["name"], customer_id)


# ==== published descriptions ============================================================


@public.get("/openapi.json", tags=["commai: descriptions"])
def commai_openapi(request: Request) -> dict:
    """OpenAPI 3.1 description of every Jibsy endpoint (from the running code)."""
    return api_docs.openapi(request.app.openapi(), _base(request) + "/")


@public.get("/asyncapi.json", tags=["commai: descriptions"])
def commai_asyncapi(request: Request) -> dict:
    """AsyncAPI 3.0 description of the webhook event stream and inbound webhooks."""
    return api_docs.asyncapi(_base(request))


# ==== data exchange: CSV and vCard ======================================================


class ImportIn(BaseModel):
    data: str = Field(min_length=1, max_length=5_000_000, description="The file's text (CSV or vCard)")


def _file(text: str, media: str, name: str) -> Response:
    return Response(
        content=text.encode(),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.get("/exports/contacts.csv")
def export_contacts_csv(customer_id: str, user: UserDep) -> Response:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        text = exchange.contacts_csv(conn, customer_id)
        audit.record(conn, user.actor, "commai.export.contacts", "csv", customer_id)
    return _file(text, "text/csv; charset=utf-8", "contacts.csv")


@router.get("/exports/contacts.vcf")
def export_contacts_vcf(customer_id: str, user: UserDep) -> Response:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        text = exchange.contacts_vcf(conn, customer_id)
        audit.record(conn, user.actor, "commai.export.contacts", "vcard", customer_id)
    return _file(text, "text/vcard; charset=utf-8", "contacts.vcf")


@router.get("/exports/conversations.csv")
def export_conversations_csv(customer_id: str, user: UserDep, since: str | None = None) -> Response:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        text = exchange.conversations_csv(conn, customer_id, since)
        audit.record(conn, user.actor, "commai.export.conversations", "csv", customer_id, {"since": since})
    return _file(text, "text/csv; charset=utf-8", "conversations.csv")


@router.post("/imports/contacts.csv")
def import_contacts_csv(customer_id: str, body: ImportIn, user: UserDep) -> dict:
    """Contacts from CSV (name, email, phone, language, external_ref), matched by
    email then phone: importing the same file twice changes nothing."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = exchange.import_contacts_csv(conn, customer_id, body.data, user.actor)
        audit.record(conn, user.actor, "commai.import.contacts", "csv", customer_id, {"rows": out["rows"]})
    return out


@router.post("/imports/contacts.vcf")
def import_contacts_vcf(customer_id: str, body: ImportIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = exchange.import_contacts_vcf(conn, customer_id, body.data, user.actor)
        audit.record(conn, user.actor, "commai.import.contacts", "vcard", customer_id, {"rows": out["rows"]})
    return out


@router.get("/imports")
def list_imports(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM commai_data_imports WHERE customer_id = %s ORDER BY created_at DESC LIMIT 50",
            (customer_id,),
        ).fetchall()


# ==== the iCalendar feed of bookings, and invites =======================================


@router.get("/ical-feed")
def get_ical_feed(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        row = ical_feed.get(conn, customer_id)
    return {"published": ical_feed.published(row), "since": row["created_at"] if ical_feed.published(row) else None}


@router.post("/ical-feed")
def publish_ical_feed(customer_id: str, request: Request, user: UserDep) -> dict:
    """A secret calendar address for the business's bookings, shown once. Making a
    new one stops the old address working."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        token = ical_feed.publish(conn, customer_id, user.actor)
        audit.record(conn, user.actor, "commai.ical_feed.publish", "bookings", customer_id)
    url = f"{_base(request)}/api/v1/commai/ical/{token}.ics"
    return {"url": url, "webcal": "webcal://" + url.split("://", 1)[1]}


@router.delete("/ical-feed", status_code=204)
def unpublish_ical_feed(customer_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        ical_feed.unpublish(conn, customer_id)
        audit.record(conn, user.actor, "commai.ical_feed.unpublish", "bookings", customer_id)


_ICS = {"Cache-Control": "private, max-age=300"}


@public.get("/ical/{token}.ics", tags=["commai: calendar"])
def bookings_feed(token: str) -> Response:
    """A business's bookings as iCalendar (subscribe from any calendar app)."""
    with db.tx() as conn:
        text = ical_feed.feed(conn, token)
    if text is None:
        raise HTTPException(404, "No such calendar.")
    return Response(content=text.encode(), media_type="text/calendar; charset=utf-8", headers=_ICS)


@public.get("/ical/invites/{run_id}.ics", tags=["commai: calendar"])
def booking_invite(run_id: str, sig: str = "") -> Response:
    """One booking as an .ics invite (or cancellation), from a signed link."""
    with db.tx() as conn:
        text = ical_feed.invite(conn, run_id, sig)
    if text is None:
        raise HTTPException(404, "No such invite.")
    return Response(
        content=text.encode(),
        media_type="text/calendar; charset=utf-8; method=" + ("CANCEL" if "METHOD:CANCEL" in text else "REQUEST"),
        headers={**_ICS, "Content-Disposition": 'attachment; filename="invite.ics"'},
    )
