"""CommAI channels API (ADR 0018): website chat, WhatsApp, SMS and email.

Two routers:

- `router` (signed in, under /customers/{customer_id}): widget keys, channel
  accounts, WhatsApp templates, template sends, simulated inbound, the
  simulated outbox and channel diagnostics.
- `public` (no sign-in): the widget script and its visitor API (CORS checked
  per key against its allowed origins), provider webhooks (signature checked)
  and email unsubscribe links.

Nothing on a customer-facing path here reads private notes: the widget reads
the messages table only.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
import re
import secrets
import time
import urllib.parse
from importlib import resources
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from ... import audit, db
from ...api.deps import UserDep
from .. import access, branding, diagnostics, events, inbox, visitor_calls
from ..channels import email as email_ch
from ..channels import messaging, providers, widget
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: channels"])
public = APIRouter(tags=["commai: channels (public)"])

events.register("widget.callback_requested", "widget.offline_message")

SESSION_HEADER = "x-widget-session"


def _no_internal(conn, user, customer_id) -> None:
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your seat can't change channel settings.")


def _public_base(request: Request) -> str:
    return os.environ.get("EXA_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


# =====================================================================================
# Signed in: setup
# =====================================================================================


def _key_out(k: dict, installs: list[dict] | None = None, secret: bool = False) -> dict:
    out = {
        "id": k["id"],
        "public_key": k["public_key"],
        "name": k["name"],
        "allowed_origins": k["allowed_origins"],
        "settings": widget.settings_of(k),
        "active": k["active"],
        "created_at": k["created_at"],
        "rotated_at": k["rotated_at"],
        "secret_hint": "…" + k["secret"][-4:],
    }
    if secret:
        out["secret"] = k["secret"]
    if installs is not None:
        out["installs"] = installs
    return out


class WidgetKeyIn(BaseModel):
    name: str = Field(default="Website", min_length=1, max_length=80)
    allowed_origins: list[str] = Field(default_factory=list, max_length=20)
    settings: dict = Field(default_factory=dict)


class WidgetKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    allowed_origins: list[str] | None = Field(default=None, max_length=20)
    settings: dict | None = None
    active: bool | None = None


def _origins(raw: list[str]) -> list[str]:
    try:
        return sorted({widget.normalise_origin(o) for o in raw if o.strip()})
    except widget.WidgetError as e:
        raise HTTPException(422, str(e)) from e


def _settings(raw: dict) -> dict:
    try:
        return widget.check_settings(raw)
    except widget.WidgetError as e:
        raise HTTPException(422, str(e)) from e


def _get_key(conn, customer_id: str, key_id: str) -> dict:
    row = conn.execute("SELECT * FROM widget_keys WHERE id = %s AND customer_id = %s", (key_id, customer_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Widget key not found.")
    return row


@router.get("/widget-keys")
def list_widget_keys(customer_id: str, user: UserDep) -> list[dict]:
    """Website chat keys, with where each widget has reported from (the installation checker)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        keys = conn.execute(
            "SELECT * FROM widget_keys WHERE customer_id = %s ORDER BY created_at", (customer_id,)
        ).fetchall()
        out = []
        for k in keys:
            installs = conn.execute(
                "SELECT origin, page, allowed, hits, first_seen_at, last_seen_at FROM widget_installs"
                " WHERE key_id = %s ORDER BY last_seen_at DESC LIMIT 20",
                (k["id"],),
            ).fetchall()
            out.append(_key_out(k, installs))
    return out


@router.post("/widget-keys", status_code=201)
def create_widget_key(customer_id: str, body: WidgetKeyIn, user: UserDep) -> dict:
    """A new website chat key. The secret is shown once, here."""
    access.check(user, customer_id, "commai:admin")
    pub, secret = widget.new_keys()
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        row = conn.execute(
            """INSERT INTO widget_keys (customer_id, public_key, secret, name, allowed_origins, settings, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                customer_id,
                pub,
                secret,
                body.name,
                _origins(body.allowed_origins),
                Jsonb(_settings(body.settings)),
                user.actor,
            ),
        ).fetchone()
        audit.record(conn, user.actor, "commai.widget_key.create", pub, customer_id)
    return _key_out(row, [], secret=True)


@router.patch("/widget-keys/{key_id}")
def update_widget_key(customer_id: str, key_id: str, body: WidgetKeyPatch, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        k = _get_key(conn, customer_id, key_id)
        merged = {**(k["settings"] or {}), **_settings(body.settings)} if body.settings is not None else None
        row = conn.execute(
            """UPDATE widget_keys SET name = COALESCE(%s, name), allowed_origins = COALESCE(%s, allowed_origins),
                      settings = COALESCE(%s, settings), active = COALESCE(%s, active)
               WHERE id = %s RETURNING *""",
            (
                body.name,
                _origins(body.allowed_origins) if body.allowed_origins is not None else None,
                Jsonb(merged) if merged is not None else None,
                body.active,
                k["id"],
            ),
        ).fetchone()
        conn.execute(
            "UPDATE widget_installs SET allowed = false WHERE key_id = %s AND NOT (origin = ANY(%s))",
            (k["id"], row["allowed_origins"]),
        )
        audit.record(
            conn,
            user.actor,
            "commai.widget_key.update",
            k["public_key"],
            customer_id,
            {"fields": sorted(body.model_dump(exclude_none=True))},
        )
    return _key_out(row)


@router.post("/widget-keys/{key_id}/rotate")
def rotate_widget_secret(customer_id: str, key_id: str, user: UserDep) -> dict:
    """A new secret. Visitor sessions and signed-in tokens made with the old one stop working."""
    access.check(user, customer_id, "commai:admin")
    _, secret = widget.new_keys()
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        k = _get_key(conn, customer_id, key_id)
        row = conn.execute(
            "UPDATE widget_keys SET secret = %s, rotated_at = now() WHERE id = %s RETURNING *", (secret, k["id"])
        ).fetchone()
        audit.record(conn, user.actor, "commai.widget_key.rotate", k["public_key"], customer_id)
    return _key_out(row, secret=True)


@router.post("/widget-keys/{key_id}/test-conversation", status_code=201)
def test_conversation(customer_id: str, key_id: str, user: UserDep) -> dict:
    """Send a test conversation into the inbox, exactly as a visitor would."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _no_internal(conn, user, customer_id)
        k = _get_key(conn, customer_id, key_id)
        got = inbox.receive(
            conn,
            customer_id,
            "web",
            f"visitor:test-{secrets.token_hex(6)}",
            "This is a test conversation from the website chat setup screen.",
            name="Test visitor",
            subject="Website chat test",
        )
        conv = inbox.set_fields(conn, customer_id, got["conversation"]["id"], actor=user.actor, tags=["test"])
        audit.record(conn, user.actor, "commai.widget_key.test", k["public_key"], customer_id)
    return {"conversation_id": conv["id"], "assignee_id": conv["assignee_id"], "team_id": conv["team_id"]}


# ---- accounts -------------------------------------------------------------------------


def _hook_url(request: Request, a: dict) -> str:
    return f"{_public_base(request)}/api/v1/commai/channels/hooks/{a['hook_token']}"


def _account_out(request: Request, a: dict, secret: bool = False) -> dict:
    if a["channel"] == "email":
        sender = email_ch.SENDERS.get(a["provider"])
        simulated = bool(sender and sender.simulated)
        missing = email_ch.SmtpSender.missing() if a["provider"] == "smtp" else []
        label = "Simulated" if simulated else "SMTP"
    else:
        prov = providers.get(a["provider"])
        simulated, missing, label = prov.simulated, prov.missing(a), prov.label
    out = {
        k: a[k]
        for k in (
            "id",
            "channel",
            "provider",
            "name",
            "address",
            "status",
            "settings",
            "last_inbound_at",
            "last_sent_at",
            "last_error",
            "last_error_at",
            "created_at",
        )
    }
    out.update(
        provider_label=label,
        simulated=simulated,
        missing_env=missing,
        webhook_url=_hook_url(request, a),
        secret_hint="…" + a["hook_secret"][-4:],
    )
    if secret:
        out["secret"] = a["hook_secret"]
    return out


class AccountIn(BaseModel):
    channel: Literal["whatsapp", "sms", "email"]
    provider: str = Field(default="simulated", max_length=20)
    name: str = Field(default="", max_length=80)
    address: str = Field(min_length=3, max_length=255, description="E.164 number, or an email address")
    settings: dict = Field(default_factory=dict)


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    status: Literal["setup", "live", "paused", "broken"] | None = None
    settings: dict | None = None


ACCOUNT_SETTINGS = {
    "credentials_env",
    "daily_limits",
    "from_name",
    "status_callback_url",
    "fail_sends",
    "email_limits",
}


def _account_settings(channel: str, provider: str, s: dict) -> dict:
    extra = set(s) - ACCOUNT_SETTINGS
    if extra:
        raise HTTPException(422, f"Unknown settings: {', '.join(sorted(extra))}.")
    if "daily_limits" in s:
        lim = s["daily_limits"]
        if not isinstance(lim, dict) or not all(
            isinstance(v, int) and 0 <= v <= 1_000_000 and len(k) <= 6 for k, v in lim.items()
        ):
            raise HTTPException(422, 'daily_limits maps a country code (or "*") to a whole number.')
    if "email_limits" in s:
        try:
            email_ch.check_limits_setting(s["email_limits"])
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    if "fail_sends" in s and provider != "simulated":
        raise HTTPException(422, "fail_sends is only for simulated accounts.")
    if "credentials_env" in s:
        try:
            providers._prefix_env({"settings": s}, "EXA_TWILIO" if provider == "twilio" else "EXA_360DIALOG")
        except providers.ProviderError as e:
            raise HTTPException(422, str(e)) from e
    return s


def _check_provider(channel: str, provider: str) -> None:
    if channel == "email":
        if provider not in email_ch.SENDERS:
            raise HTTPException(422, "Email accounts use the simulated sender or smtp.")
        return
    try:
        p = providers.get(provider)
    except providers.ProviderError as e:
        raise HTTPException(422, str(e)) from e
    if channel not in p.channels:
        raise HTTPException(422, f"{p.label} does not carry {channel}.")


def _get_account(conn, customer_id: str, account_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM channel_accounts WHERE id = %s AND customer_id = %s", (account_id, customer_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Channel account not found.")
    return row


@router.get("/channel-accounts")
def list_accounts(customer_id: str, request: Request, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_accounts WHERE customer_id = %s ORDER BY channel, created_at", (customer_id,)
        ).fetchall()
    return [_account_out(request, a) for a in rows]


@router.post("/channel-accounts", status_code=201)
def create_account(customer_id: str, body: AccountIn, request: Request, user: UserDep) -> dict:
    """A WhatsApp number, SMS number or email address. Simulated accounts are live
    at once; real providers start in setup until their credentials are on the server.
    The secret (inbound email's shared secret; simulated webhooks' signing key) is shown once."""
    access.check(user, customer_id, "commai:admin")
    _check_provider(body.channel, body.provider)
    settings = _account_settings(body.channel, body.provider, body.settings)
    if body.channel == "email":
        address = body.address.strip().lower()
        if "@" not in address:
            raise HTTPException(422, "Give an email address.")
        live = body.provider == "simulated"
    else:
        address = providers.e164(body.address)
        if len(address) < 8:
            raise HTTPException(422, "Give the number in international form, like +18685550100.")
        live = body.provider == "simulated"
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        if conn.execute(
            "SELECT 1 FROM channel_accounts WHERE customer_id = %s AND channel = %s AND address = %s",
            (customer_id, body.channel, address),
        ).fetchone():
            raise HTTPException(409, "That account is already set up.")
        row = conn.execute(
            """INSERT INTO channel_accounts (customer_id, channel, provider, name, address, status, settings,
                                             hook_token, hook_secret, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                customer_id,
                body.channel,
                body.provider,
                body.name,
                address,
                "live" if live else "setup",
                Jsonb(settings),
                messaging.new_hook_token(),
                "chs_" + secrets.token_urlsafe(24),
                user.actor,
            ),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "commai.channel_account.create",
            f"{body.channel}:{address}",
            customer_id,
            {"provider": body.provider},
        )
    return _account_out(request, row, secret=True)


@router.patch("/channel-accounts/{account_id}")
def update_account(customer_id: str, account_id: str, body: AccountPatch, request: Request, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        a = _get_account(conn, customer_id, account_id)
        settings = None
        if body.settings is not None:
            settings = {**(a["settings"] or {}), **_account_settings(a["channel"], a["provider"], body.settings)}
        if body.status == "live":
            probe = {**a, "settings": settings if settings is not None else a["settings"]}
            if a["channel"] == "email":
                missing = email_ch.SmtpSender.missing() if a["provider"] == "smtp" else []
            else:
                missing = providers.get(a["provider"]).missing(probe)
            if missing:
                raise HTTPException(409, f"It can't go live yet: set {', '.join(missing)} on the server first.")
        row = conn.execute(
            """UPDATE channel_accounts SET name = COALESCE(%s, name), status = COALESCE(%s, status),
                      settings = COALESCE(%s, settings), updated_at = now(),
                      last_error = CASE WHEN %s = 'live' THEN '' ELSE last_error END
               WHERE id = %s RETURNING *""",
            (body.name, body.status, Jsonb(settings) if settings is not None else None, body.status, a["id"]),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "commai.channel_account.update",
            f"{a['channel']}:{a['address']}",
            customer_id,
            body.model_dump(exclude_none=True),
        )
    return _account_out(request, row)


@router.delete("/channel-accounts/{account_id}", status_code=204)
def delete_account(customer_id: str, account_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        a = _get_account(conn, customer_id, account_id)
        conn.execute("DELETE FROM channel_accounts WHERE id = %s", (a["id"],))
        audit.record(conn, user.actor, "commai.channel_account.delete", f"{a['channel']}:{a['address']}", customer_id)


class SimInbound(BaseModel):
    from_: str = Field(alias="from", min_length=3, max_length=255)
    name: str = Field(default="", max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    subject: str = Field(default="", max_length=200)
    id: str = Field(default="", max_length=200, description="The provider's message id; repeat it to test duplicates")
    in_reply_to: str = Field(default="", max_length=500)


@router.post("/channel-accounts/{account_id}/simulate-inbound")
def simulate_inbound(customer_id: str, account_id: str, body: SimInbound, request: Request, user: UserDep) -> dict:
    """A message from a customer, through the same signed webhook path a provider uses.
    Simulated accounts only."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        a = _get_account(conn, customer_id, account_id)
        if a["channel"] == "email":
            raw = json.dumps(
                {
                    "from": body.from_,
                    "from_name": body.name,
                    "subject": body.subject,
                    "text": body.body,
                    "message_id": f"<{body.id or secrets.token_hex(8)}@sim.example>",
                    "in_reply_to": body.in_reply_to,
                }
            ).encode()
            got = _email_inbound(conn, a, "application/json", raw, {}, a["hook_secret"])
        else:
            if a["provider"] != "simulated":
                raise HTTPException(409, "Only simulated accounts can be sent test messages from here.")
            raw = json.dumps(
                {
                    "messages": [
                        {
                            "id": body.id or secrets.token_hex(8),
                            "from": body.from_,
                            "name": body.name,
                            "body": body.body,
                        }
                    ]
                }
            ).encode()
            req = providers.InboundRequest(
                url=_hook_url(request, a),
                headers={"x-exa-signature": providers.Simulated.sign(a["hook_secret"], raw)},
                body=raw,
            )
            got = messaging.handle_provider_webhook(conn, a, req)
        audit.record(conn, user.actor, "commai.channel_account.simulate_inbound", str(a["id"]), customer_id)
    return got


class SimReceipt(BaseModel):
    message_id: str
    status: Literal["sent", "delivered", "read", "failed"]
    error: str = Field(default="", max_length=200)


@router.post("/channel-accounts/{account_id}/simulate-receipt")
def simulate_receipt(customer_id: str, account_id: str, body: SimReceipt, request: Request, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        a = _get_account(conn, customer_id, account_id)
        if a["provider"] != "simulated":
            raise HTTPException(409, "Only simulated accounts take test receipts.")
        m = conn.execute(
            "SELECT provider_ref FROM messages WHERE id = %s AND customer_id = %s", (body.message_id, customer_id)
        ).fetchone()
        if m is None or not m["provider_ref"]:
            raise HTTPException(404, "That message hasn't been sent yet.")
        raw = json.dumps(
            {"statuses": [{"ref": m["provider_ref"], "status": body.status, "error": body.error}]}
        ).encode()
        req = providers.InboundRequest(
            url=_hook_url(request, a),
            headers={"x-exa-signature": providers.Simulated.sign(a["hook_secret"], raw)},
            body=raw,
        )
        return messaging.handle_provider_webhook(conn, a, req)


@router.get("/channels/outbox")
def simulated_outbox(customer_id: str, user: UserDep, limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    """What the simulated providers 'sent' (nothing reaches real phones or inboxes)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return conn.execute(
            """SELECT id, channel, to_address, body, template, provider_ref, attachments, created_at
               FROM sim_channel_outbox
               WHERE customer_id = %s ORDER BY id DESC LIMIT %s""",
            (customer_id, limit),
        ).fetchall()


@router.get("/channels/diagnostics")
def channel_diagnostics(customer_id: str, user: UserDep) -> list[dict]:
    """Why a channel may have stopped sending, from its real configuration and recent sends."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return diagnostics.run(conn, customer_id, ["whatsapp", "sms", "email", "website_chat"])


# ---- WhatsApp templates -------------------------------------------------------------------


class TemplateIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_]{1,60}$", description="lower case, digits and underscores")
    language: str = Field(default="en", pattern=r"^[a-z]{2}(_[A-Z]{2})?$")
    category: Literal["utility", "marketing", "authentication"] = "utility"
    body: str = Field(min_length=1, max_length=1024)
    provider_template_id: str = Field(default="", max_length=80)


class TemplateReview(BaseModel):
    status: Literal["approved", "rejected", "pending"]
    reason: str = Field(default="", max_length=300)
    provider_template_id: str | None = Field(default=None, max_length=80)


@router.get("/whatsapp-templates")
def list_templates(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM whatsapp_templates WHERE customer_id = %s ORDER BY name, language", (customer_id,)
        ).fetchall()
    for r in rows:
        r["placeholders"] = providers.placeholders(r["body"])
    return rows


@router.post("/whatsapp-templates", status_code=201)
def create_template(customer_id: str, body: TemplateIn, user: UserDep) -> dict:
    """A template waits as 'pending' until the provider (or, when simulated, the setup screen) approves it."""
    access.check(user, customer_id, "commai:admin")
    n = providers.placeholders(body.body)
    if sorted(set(int(x) for x in re.findall(r"\{\{(\d+)\}\}", body.body))) != list(range(1, n + 1)):
        raise HTTPException(422, "Number the placeholders {{1}}, {{2}}... with no gaps.")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        if conn.execute(
            "SELECT 1 FROM whatsapp_templates WHERE customer_id = %s AND name = %s AND language = %s",
            (customer_id, body.name, body.language),
        ).fetchone():
            raise HTTPException(409, "A template with that name and language exists.")
        row = conn.execute(
            """INSERT INTO whatsapp_templates (customer_id, name, language, category, body, provider_template_id,
                                               created_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (customer_id, body.name, body.language, body.category, body.body, body.provider_template_id, user.actor),
        ).fetchone()
        audit.record(conn, user.actor, "commai.template.create", f"{body.name}:{body.language}", customer_id)
    row["placeholders"] = n
    return row


@router.post("/whatsapp-templates/{template_id}/review")
def review_template(customer_id: str, template_id: str, body: TemplateReview, user: UserDep) -> dict:
    """Record the template's approval status. With only simulated WhatsApp accounts,
    this stands in for Meta's review. With a real provider, the status must mirror
    the provider's console, and only ExaCarib admins may set it."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        tpl = conn.execute(
            "SELECT * FROM whatsapp_templates WHERE id = %s AND customer_id = %s", (template_id, customer_id)
        ).fetchone()
        if tpl is None:
            raise HTTPException(404, "Template not found.")
        real = conn.execute(
            "SELECT 1 FROM channel_accounts WHERE customer_id = %s AND channel = 'whatsapp'"
            " AND provider <> 'simulated'",
            (customer_id,),
        ).fetchone()
        if real and user.role != "admin":
            raise HTTPException(403, "Template approval comes from Meta through your provider; ExaCarib records it.")
        by = "simulated provider" if not real else f"{user.actor} (from the provider console)"
        row = conn.execute(
            """UPDATE whatsapp_templates SET status = %s, status_reason = %s, status_by = %s,
                      provider_template_id = COALESCE(%s, provider_template_id), updated_at = now()
               WHERE id = %s RETURNING *""",
            (body.status, body.reason, by, body.provider_template_id, tpl["id"]),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "commai.template.review",
            f"{tpl['name']}:{tpl['language']}",
            customer_id,
            {"status": body.status, "by": by},
        )
    row["placeholders"] = providers.placeholders(row["body"])
    return row


@router.delete("/whatsapp-templates/{template_id}", status_code=204)
def delete_template(customer_id: str, template_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        row = conn.execute(
            "DELETE FROM whatsapp_templates WHERE id = %s AND customer_id = %s RETURNING name",
            (template_id, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Template not found.")
        audit.record(conn, user.actor, "commai.template.delete", row["name"], customer_id)


class TemplateSend(BaseModel):
    template_id: str
    params: list[str] = Field(default_factory=list, max_length=20)
    take_over: bool = False


@router.post("/conversations/{conversation_id}/template", status_code=201)
def send_template(customer_id: str, conversation_id: str, body: TemplateSend, user: UserDep) -> dict:
    """Send an approved WhatsApp template (the only thing allowed outside the
    24-hour service window). Send an Idempotency-Key header so a retry never sends twice."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        access.require_reply_seat(conn, user, customer_id)
        tpl = conn.execute(
            "SELECT * FROM whatsapp_templates WHERE id = %s AND customer_id = %s", (body.template_id, customer_id)
        ).fetchone()
        if tpl is None:
            raise HTTPException(404, "Template not found.")
        n = providers.placeholders(tpl["body"])
        params = [p.strip() for p in body.params]
        if len(params) != n or any(not p or "\n" in p or len(p) > 500 for p in params):
            raise HTTPException(422, f"The template {tpl['name']} needs {n} values, each on one line.")
        msg = inbox.send(
            conn,
            customer_id,
            conversation_id,
            providers.render(tpl["body"], params),
            author_kind="user",
            author=user.email,
            user_id=user.id,
            template=f"{tpl['name']}:{tpl['language']}",
            take_over=body.take_over,
        )
        conn.execute(
            "INSERT INTO template_sends (message_id, template_id, params) VALUES (%s, %s, %s)",
            (msg["id"], tpl["id"], params),
        )
        audit.record(
            conn,
            user.actor,
            "commai.message.template",
            str(msg["id"]),
            customer_id,
            {"conversation_id": conversation_id, "template": tpl["name"]},
        )
    return msg


@router.get("/files/{file_id}")
def staff_file(customer_id: str, file_id: str, user: UserDep) -> Response:
    """A file a contact attached, for the inbox."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        f = conn.execute(
            "SELECT * FROM channel_files WHERE id = %s AND customer_id = %s AND conversation_id IS NOT NULL",
            (file_id, customer_id),
        ).fetchone()
    if f is None:
        raise HTTPException(404, "File not found.")
    return _file_response(f)


def _file_response(f: dict) -> Response:
    inline = f["content_type"].startswith("image/")
    safe = "".join(c for c in f["name"] if c.isalnum() or c in "._- ")[:100] or "file"
    return Response(
        bytes(f["data"]),
        media_type=f["content_type"],
        headers={
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=300",
        },
    )


# =====================================================================================
# Public: the widget
# =====================================================================================


_SCRIPT_CACHE: dict[str, bytes] = {}


@public.get("/widget/v1.js", include_in_schema=False)
def widget_script() -> Response:
    """The website chat script. Install with one tag:
    <script src="https://HOST/api/v1/commai/widget/v1.js" data-key="wk_..." async></script>"""
    if "js" not in _SCRIPT_CACHE or os.environ.get("EXA_ENV") == "dev":
        _SCRIPT_CACHE["js"] = resources.files("exaconnect_controller.commai").joinpath("static/widget.js").read_bytes()
    return Response(
        _SCRIPT_CACHE["js"],
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


def _cors_headers(origin: str) -> dict:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Widget-Session",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def _load_key(conn, public_key: str) -> dict:
    k = conn.execute("SELECT * FROM widget_keys WHERE public_key = %s AND active", (public_key,)).fetchone()
    if k is None:
        raise widget.WidgetError("This chat key is not active.", 404)
    return k


class _Ctx:
    def __init__(self, conn, key: dict, origin: str, session: dict | None):
        self.conn, self.key, self.origin, self.session = conn, key, origin, session

    @property
    def address(self) -> str:
        if not self.session:
            raise widget.WidgetError("Start a chat session first.", 401)
        return self.session["a"]


def _widget_call(request: Request, public_key: str, fn, *, need_session: bool = True, status_code: int = 200):
    """Run one widget request: check the key and its allowed origins, read the
    visitor session, and answer with CORS headers (errors included)."""
    origin = request.headers.get("origin", "")
    headers: dict = {"Vary": "Origin"}
    try:
        with db.tx() as conn:
            k = _load_key(conn, public_key)
            if not widget.origin_allowed(k, origin):
                raise widget.WidgetError("This website isn't on the allowed list for this chat key.", 403)
            headers = _cors_headers(origin.strip().rstrip("/"))
            session = None
            token = request.headers.get(SESSION_HEADER, "")
            if token:
                session = widget.read_session(k, token)
            elif need_session:
                raise widget.WidgetError("Start a chat session first.", 401)
            try:
                data = fn(_Ctx(conn, k, origin, session))
            except inbox.InboxError as e:
                raise widget.WidgetError(str(e), e.code) from e
            if session and isinstance(data, dict) and session["exp"] - time.time() < widget.SESSION_TTL_S / 2:
                data["session"] = widget.issue_session(k, session["a"], session["s"], session.get("n", ""))
        if isinstance(data, Response):
            data.headers.update(headers)
            return data
        return JSONResponse(jsonable_encoder(data), status_code=status_code, headers=headers)
    except widget.WidgetError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.code, headers=headers)


def _body(model: type[BaseModel], raw: Any) -> Any:
    try:
        return model.model_validate(raw or {})
    except ValidationError as e:
        first = e.errors()[0]
        field = ".".join(str(x) for x in first["loc"]) or "body"
        raise widget.WidgetError(f"{field}: {first['msg']}", 422) from e


async def _json(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > 3_000_000:
        return None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return None


@public.options("/widget/{public_key}/{rest:path}", include_in_schema=False)
def widget_preflight(public_key: str, rest: str, request: Request) -> Response:
    origin = request.headers.get("origin", "")
    with db.tx() as conn:
        k = conn.execute("SELECT * FROM widget_keys WHERE public_key = %s AND active", (public_key,)).fetchone()
    if k is None or not widget.origin_allowed(k, origin):
        return Response(status_code=403, headers={"Vary": "Origin"})
    return Response(status_code=204, headers=_cors_headers(origin.strip().rstrip("/")))


@public.get("/widget/{public_key}/config")
def widget_config(public_key: str, request: Request):
    """What the widget shows: title, greeting, colours, hours, mode."""

    def run(c: _Ctx) -> dict:
        s = inbox.settings(c.conn, c.key["customer_id"])
        ws = widget.settings_of(c.key)
        ai = s["mode"] == "ai_first" and inbox.ai_available()
        brand = branding.public_view(branding.for_customer(c.conn, c.key["customer_id"]))  # ADR 0031
        return {
            "title": ws["title"],
            "greeting": ws["greeting"],
            "colour": brand["colour"] if brand and ws["colour"] == widget.DEFAULT_SETTINGS["colour"] else ws["colour"],
            "brand": brand,
            "position": ws["position"],
            "mode": s["mode"],
            "ai": ai,
            "online": ai or widget.open_now(c.key, s["timezone"]),
            "hours": widget.hours_text(c.key),
            "timezone": s["timezone"],
            "offline_message": ws["offline_message"],
            "callbacks": bool(ws["callbacks"]),
            "attachments": {
                "enabled": bool(ws["attachments"]),
                "max_bytes": widget.MAX_FILE_BYTES,
                "types": sorted(widget.FILE_TYPES),
            },
            "ask_contact": ws["ask_contact"],
            "ai_calls": visitor_calls.enabled(c.conn, c.key),
        }

    return _widget_call(request, public_key, run, need_session=False)


class SessionIn(BaseModel):
    user_token: str = Field(default="", max_length=4000)


@public.post("/widget/{public_key}/session")
async def widget_session(public_key: str, request: Request):
    """Start (or renew) a visitor session. With `user_token` (an HS256 JWT the
    business's own site signed with its widget secret) the visitor is a verified,
    signed-in customer and sees their past conversations; without one, an anonymous
    visitor with no history."""
    raw = await _json(request)

    def run(c: _Ctx) -> dict:
        body = _body(SessionIn, raw)
        if body.user_token:
            claims = widget.verify_user_token(c.key, body.user_token)
            address = f"user:{claims['sub']}"
            ident = inbox.find_or_create_identity(
                c.conn, c.key["customer_id"], "web", address, name=claims["name"], verified=True
            )
            if claims["email"]:
                c.conn.execute(
                    "UPDATE contacts SET email = %s WHERE id = %s AND email = ''",
                    (claims["email"], ident["contact_id"]),
                )
            sess = widget.issue_session(c.key, address, True, claims["name"])
            return {
                **sess,
                "name": claims["name"],
                "conversations": widget.visitor_conversations(c.conn, c.key, address),
            }
        if c.session:  # renew, same visitor
            sess = widget.issue_session(c.key, c.session["a"], c.session["s"], c.session.get("n", ""))
            return {**sess, "conversations": widget.visitor_conversations(c.conn, c.key, c.session["a"])}
        sess = widget.issue_session(c.key, f"visitor:{secrets.token_urlsafe(16)}", False)
        return {**sess, "conversations": []}

    return await run_in_threadpool(_widget_call, request, public_key, run, need_session=False)


@public.get("/widget/{public_key}/conversations")
def widget_conversations(public_key: str, request: Request):
    return _widget_call(
        request, public_key, lambda c: {"items": widget.visitor_conversations(c.conn, c.key, c.address)}
    )


@public.get("/widget/{public_key}/messages")
def widget_messages(public_key: str, request: Request, conversation_id: str, after: str | None = None):
    """Customer-facing messages of the visitor's own conversation (poll with `after`)."""

    def run(c: _Ctx) -> dict:
        conv = widget.own_conversation(c.conn, c.key, c.address, conversation_id)
        if after:
            try:
                dt.datetime.fromisoformat(after)
            except ValueError as e:
                raise widget.WidgetError("after must be a timestamp.", 422) from e
        msgs = inbox.messages(c.conn, c.key["customer_id"], conv["id"], after)
        return {
            "conversation": {"id": conv["id"], "state": conv["state"]},
            "items": [widget.public_message(m) for m in msgs],
        }

    return _widget_call(request, public_key, run)


class WidgetMessageIn(BaseModel):
    body: str = Field(default="", max_length=4000)
    conversation_id: str | None = None
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    attachments: list[str] = Field(default_factory=list, max_length=5)


@public.post("/widget/{public_key}/messages")
async def widget_send(public_key: str, request: Request):
    """A visitor's message. `client_id` makes it safe to retry: the same id is stored once."""
    raw = await _json(request)

    def run(c: _Ctx) -> dict:
        body = _body(WidgetMessageIn, raw)
        text = body.body.strip()
        if not text and not body.attachments:
            raise widget.WidgetError("Write a message first.")
        files = []
        if body.attachments:
            if not widget.settings_of(c.key)["attachments"]:
                raise widget.WidgetError("Attachments are switched off for this chat.")
            files = c.conn.execute(
                """SELECT id, name, content_type, size FROM channel_files WHERE id = ANY(%s::uuid[])
                   AND customer_id = %s AND owner = %s AND conversation_id IS NULL""",
                (body.attachments, c.key["customer_id"], c.address),
            ).fetchall()
            if len(files) != len(set(body.attachments)):
                raise widget.WidgetError("One of the attachments wasn't found. Upload it again.", 404)
        got = inbox.receive(
            c.conn,
            c.key["customer_id"],
            "web",
            c.address,
            text,
            external_id=f"web:{c.key['public_key']}:{body.client_id}",
            name=c.session.get("n", ""),
            verified=bool(c.session["s"]),
            conversation_id=body.conversation_id,
            attachments=[
                {"id": str(f["id"]), "name": f["name"], "type": f["content_type"], "size": f["size"]} for f in files
            ],
        )
        if files and not got["duplicate"]:
            c.conn.execute(
                "UPDATE channel_files SET conversation_id = %s WHERE id = ANY(%s)",
                (got["conversation"]["id"], [f["id"] for f in files]),
            )
        return {
            "conversation_id": got["conversation"]["id"],
            "message": widget.public_message(got["message"]),
            "duplicate": got["duplicate"],
        }

    return await run_in_threadpool(_widget_call, request, public_key, run, status_code=201)


class FileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(max_length=100)
    data: str = Field(description="base64")


@public.post("/widget/{public_key}/files")
async def widget_upload(public_key: str, request: Request):
    """A small attachment (images, PDF, plain text; up to 2 MB). Attach it by id on the next message."""
    raw = await _json(request)
    if raw is None:
        return JSONResponse({"detail": "Files can be up to 2 MB."}, status_code=413, headers={"Vary": "Origin"})

    def run(c: _Ctx) -> dict:
        body = _body(FileIn, raw)
        try:
            data = base64.b64decode(body.data, validate=True)
        except (binascii.Error, ValueError) as e:
            raise widget.WidgetError("The file couldn't be read.") from e
        widget.check_file(body.name, body.type, data)
        if not widget.settings_of(c.key)["attachments"]:
            raise widget.WidgetError("Attachments are switched off for this chat.")
        pending = c.conn.execute(
            "SELECT count(*) AS n FROM channel_files WHERE owner = %s AND customer_id = %s AND conversation_id IS NULL",
            (c.address, c.key["customer_id"]),
        ).fetchone()["n"]
        if pending >= 10:
            raise widget.WidgetError("Send the files you've added before adding more.", 429)
        row = c.conn.execute(
            """INSERT INTO channel_files (customer_id, owner, name, content_type, size, data)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, name, content_type, size""",
            (c.key["customer_id"], c.address, body.name, body.type, len(data), data),
        ).fetchone()
        return {"id": row["id"], "name": row["name"], "type": row["content_type"], "size": row["size"]}

    return await run_in_threadpool(_widget_call, request, public_key, run, status_code=201)


@public.get("/widget/{public_key}/files/{file_id}")
def widget_file(public_key: str, file_id: str, request: Request, s: str = ""):
    """A file in the visitor's own conversation. `s` is the session token (images can't send headers)."""
    origin = request.headers.get("origin", "")
    try:
        with db.tx() as conn:
            k = _load_key(conn, public_key)
            sess = widget.read_session(k, s or request.headers.get(SESSION_HEADER, ""))
            f = conn.execute(
                """SELECT f.* FROM channel_files f
                   LEFT JOIN conversations c ON c.id = f.conversation_id
                   LEFT JOIN contact_identities ci ON ci.id = c.identity_id
                   WHERE f.id = %s AND f.customer_id = %s
                     AND (f.owner = %s OR (ci.channel = 'web' AND ci.address = %s))""",
                (file_id, k["customer_id"], sess["a"], sess["a"]),
            ).fetchone()
        if f is None:
            raise widget.WidgetError("File not found.", 404)
    except widget.WidgetError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.code)
    resp = _file_response(f)
    if origin and widget.origin_allowed(k, origin):
        resp.headers.update(_cors_headers(origin.strip().rstrip("/")))
    return resp


class ContactIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=40)


@public.post("/widget/{public_key}/contact")
async def widget_contact(public_key: str, request: Request):
    """Name and email the visitor typed. Recorded on their contact as given
    (unverified): it links nothing and shows no history."""
    raw = await _json(request)

    def run(c: _Ctx) -> dict:
        body = _body(ContactIn, raw)
        if body.email and ("@" not in body.email or " " in body.email.strip()):
            raise widget.WidgetError("That email address doesn't look right.", 422)
        ident = inbox.find_or_create_identity(
            c.conn, c.key["customer_id"], "web", c.address, verified=bool(c.session["s"])
        )
        c.conn.execute(
            """UPDATE contacts SET name = CASE WHEN %(n)s <> '' THEN %(n)s ELSE name END,
                      email = CASE WHEN %(e)s <> '' AND NOT %(signed)s THEN %(e)s ELSE email END,
                      phone = CASE WHEN %(p)s <> '' THEN %(p)s ELSE phone END
               WHERE id = %(id)s""",
            {
                "n": body.name.strip(),
                "e": body.email.strip(),
                "p": body.phone.strip(),
                "signed": bool(c.session["s"]),
                "id": ident["contact_id"],
            },
        )
        return {"ok": True}

    return await run_in_threadpool(_widget_call, request, public_key, run)


class OfflineIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(min_length=3, max_length=255)
    message: str = Field(min_length=1, max_length=4000)
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@public.post("/widget/{public_key}/offline")
async def widget_offline(public_key: str, request: Request):
    """The offline form: the message becomes an email conversation, so the reply goes by email."""
    raw = await _json(request)

    def run(c: _Ctx) -> dict:
        body = _body(OfflineIn, raw)
        addr = body.email.strip().lower()
        if "@" not in addr or " " in addr:
            raise widget.WidgetError("That email address doesn't look right.", 422)
        got = inbox.receive(
            c.conn,
            c.key["customer_id"],
            "email",
            addr,
            body.message.strip(),
            external_id=f"web-offline:{c.key['public_key']}:{body.client_id}",
            name=body.name.strip(),
            subject="Message from your website (out of hours)",
        )
        if not got["duplicate"]:
            events.emit(
                c.conn,
                c.key["customer_id"],
                "widget.offline_message",
                {"conversation_id": str(got["conversation"]["id"])},
                got["conversation"]["id"],
            )
        return {"ok": True}

    return await run_in_threadpool(_widget_call, request, public_key, run, status_code=201)


class CallbackIn(BaseModel):
    name: str = Field(default="", max_length=200)
    phone: str = Field(min_length=7, max_length=40)
    when: str = Field(default="", max_length=100)
    note: str = Field(default="", max_length=1000)
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@public.post("/widget/{public_key}/callback")
async def widget_callback(public_key: str, request: Request):
    """A callback request: lands in the inbox tagged 'callback' with the number to ring."""
    raw = await _json(request)

    def run(c: _Ctx) -> dict:
        body = _body(CallbackIn, raw)
        if not widget.settings_of(c.key)["callbacks"]:
            raise widget.WidgetError("Callbacks are switched off for this chat.")
        phone = providers.e164(body.phone)
        if len(phone) < 8:
            raise widget.WidgetError("Give the number with its country code, like +1 868 555 0100.", 422)
        text = f"Please call me back on {phone}" + (f" ({body.when.strip()})" if body.when.strip() else "") + "."
        if body.note.strip():
            text += f"\n{body.note.strip()}"
        got = inbox.receive(
            c.conn,
            c.key["customer_id"],
            "web",
            c.address,
            text,
            external_id=f"web-callback:{c.key['public_key']}:{body.client_id}",
            name=body.name.strip() or c.session.get("n", ""),
            verified=bool(c.session["s"]),
            subject="Callback request",
        )
        conv = got["conversation"]
        if not got["duplicate"]:
            inbox.set_fields(
                c.conn, c.key["customer_id"], conv["id"], actor="widget", tags=sorted(set(conv["tags"]) | {"callback"})
            )
            c.conn.execute("UPDATE contacts SET phone = %s WHERE id = %s AND phone = ''", (phone, conv["contact_id"]))
            events.emit(
                c.conn,
                c.key["customer_id"],
                "widget.callback_requested",
                {"conversation_id": str(conv["id"]), "when": body.when.strip()},
                conv["id"],
            )
        return {"ok": True, "conversation_id": conv["id"]}

    return await run_in_threadpool(_widget_call, request, public_key, run, status_code=201)


@public.post("/widget/{public_key}/heartbeat")
async def widget_heartbeat(public_key: str, request: Request):
    """The installation checker. The widget reports the page it runs on (sent as
    text/plain, so even a website that isn't allowed yet shows up on the setup screen).
    Nothing fetches the customer's site."""
    raw = await _json(request) or {}
    origin = request.headers.get("origin", "").strip().rstrip("/").lower()
    page = str(raw.get("page", ""))[:300] if isinstance(raw, dict) else ""

    def run() -> Response:
        with db.tx() as conn:
            k = conn.execute("SELECT * FROM widget_keys WHERE public_key = %s", (public_key,)).fetchone()
            if k is None or not origin or len(origin) > 200:
                return Response(status_code=204)
            allowed = widget.origin_allowed(k, origin)
            known = conn.execute(
                "SELECT 1 FROM widget_installs WHERE key_id = %s AND origin = %s", (k["id"], origin)
            ).fetchone()
            count = conn.execute("SELECT count(*) AS n FROM widget_installs WHERE key_id = %s", (k["id"],)).fetchone()
            if known or count["n"] < widget.MAX_INSTALL_ORIGINS:
                conn.execute(
                    """INSERT INTO widget_installs (key_id, origin, page, allowed) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (key_id, origin) DO UPDATE SET page = EXCLUDED.page, allowed = EXCLUDED.allowed,
                         hits = widget_installs.hits + 1, last_seen_at = now()""",
                    (k["id"], origin, page, allowed),
                )
        return Response(status_code=204, headers=_cors_headers(origin) if allowed else {"Vary": "Origin"})

    return await run_in_threadpool(run)


# =====================================================================================
# Public: provider webhooks and unsubscribe
# =====================================================================================


def _email_inbound(conn, account: dict, content_type: str, raw: bytes, form: dict, secret: str) -> dict:
    if not email_ch.verify_secret(account, secret):
        messaging.log_webhook(conn, account, "rejected", "shared secret did not match")
        raise PermissionError("The shared secret did not match.")
    mail = email_ch.parse_inbound(content_type, raw, form)
    got = email_ch.receive_email(conn, account, mail)
    messaging.log_webhook(conn, account, "accepted", "duplicate" if got["duplicate"] else "received")
    return {"received": 0 if got["duplicate"] else 1, "duplicates": 1 if got["duplicate"] else 0, "receipts": 0}


@public.post("/channels/hooks/{hook_token}", include_in_schema=False)
async def provider_webhook(hook_token: str, request: Request):
    """Inbound messages and receipts from a provider (or inbound email). The
    signature (or email shared secret) is checked before anything is stored; a
    repeated webhook never stores a message twice."""
    raw = await request.body()
    if len(raw) > 5_000_000:
        return JSONResponse({"detail": "Too large."}, status_code=413)
    ctype = request.headers.get("content-type", "")
    form: dict[str, str] = {}
    if "application/x-www-form-urlencoded" in ctype:
        form = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    elif "multipart/form-data" in ctype:
        parsed = await request.form()
        form = {k: v for k, v in parsed.items() if isinstance(v, str)}
    base = os.environ.get("EXA_PUBLIC_URL", "").rstrip("/")
    url = (
        (base + request.url.path + (f"?{request.url.query}" if request.url.query else "")) if base else str(request.url)
    )
    headers = {k.lower(): v for k, v in request.headers.items()}

    def run():
        with db.tx() as conn:
            a = conn.execute("SELECT * FROM channel_accounts WHERE hook_token = %s", (hook_token,)).fetchone()
            if a is None:
                return None, "missing"
            try:
                if a["channel"] == "email":
                    secret = headers.get("x-exa-email-secret") or request.query_params.get("secret", "")
                    out = _email_inbound(conn, a, ctype, raw, form, secret)
                else:
                    req = providers.InboundRequest(url=url, headers=headers, body=raw, form=form)
                    out = messaging.handle_provider_webhook(conn, a, req)
            except PermissionError:
                return a, "rejected"
            except (ValueError, KeyError) as e:
                messaging.log_webhook(conn, a, "unreadable", type(e).__name__)
                return a, "unreadable"
            return a, out

    # A refused webhook is still logged: run() commits its own log row before answering.
    a, out = await run_in_threadpool(run)
    if a is None:
        return JSONResponse({"detail": "Unknown webhook."}, status_code=404)
    if out == "rejected":
        return JSONResponse({"detail": "Signature did not verify."}, status_code=403)
    if out == "unreadable":
        return JSONResponse({"detail": "The webhook body couldn't be read."}, status_code=400)
    if a["channel"] != "email" and not providers.get(a["provider"]).simulated:
        ctype_out, body = providers.get(a["provider"]).response()
        return Response(body, media_type=ctype_out)
    return out


@public.get("/channels/hooks/{hook_token}", include_in_schema=False)
def provider_webhook_verify(hook_token: str, request: Request):
    """Meta-style subscription check: echo hub.challenge when hub.verify_token is
    the account's secret."""
    q = request.query_params
    with db.tx() as conn:
        a = conn.execute("SELECT * FROM channel_accounts WHERE hook_token = %s", (hook_token,)).fetchone()
    if a is None or q.get("hub.mode") != "subscribe" or not email_ch.verify_secret(a, q.get("hub.verify_token", "")):
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse(q.get("hub.challenge", "")[:200])


@public.api_route("/channels/email/{hook_token}/unsubscribe/{token}", methods=["GET", "POST"], include_in_schema=False)
def email_unsubscribe(hook_token: str, token: str):
    """The List-Unsubscribe link (one click)."""
    with db.tx() as conn:
        ident = email_ch.check_unsubscribe(conn, hook_token, token)
    if ident is None:
        return PlainTextResponse("This unsubscribe link isn't valid.", status_code=404)
    return PlainTextResponse("You're unsubscribed. You won't get more emails from this address.")
