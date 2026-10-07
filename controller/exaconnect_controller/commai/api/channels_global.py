"""CommAI phase 3 channels API (ADR 0029): Messenger, Instagram and Telegram
accounts, the country matrix and SMS rules, and SMS routing across carriers.

- `router` (signed in, under /customers/{customer_id}): the business's
  Messenger, Instagram and Telegram accounts (secure token entry, connect,
  simulated traffic), the country matrix as the business sees it, and SMS
  sender registrations.
- `public` (no customer prefix): ExaCarib admin endpoints (SMS routes,
  carriers, country SMS rules, registration decisions; admins only) and the
  platform webhooks (signature checked, no sign-in).

No endpoint here returns a token or reads private notes.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ... import audit, db
from ...api.deps import User, UserDep, require_admin
from .. import access, events, golive, inbox
from ..channels import countries, messaging, providers, sms_routing, social, whatsapp_cloud

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: channels (global)"])
public = APIRouter(tags=["commai: channels (global)"])

SOCIAL = ("messenger", "instagram", "telegram")

# What each channel needs before it can go live, in plain words for the setup screens.
NEEDS: dict[str, list[str]] = {
    "messenger": [
        "ExaCarib's Meta app must pass Meta's app review for pages_messaging (ExaCarib does this once).",
        "Your business completes Meta business verification.",
        "A Page admin connects your Facebook Page and gives a Page access token through secure entry.",
        "Replies are free within 24 hours of the person's last message; after that only a person can reply, "
        "with a message tag (human agent: up to 7 days).",
    ],
    "instagram": [
        "ExaCarib's Meta app must pass Meta's app review for instagram_manage_messages (ExaCarib does this once).",
        "Your Instagram account must be a professional account linked to your Facebook Page.",
        "In the Instagram app, turn on 'Allow access to messages' under message controls.",
        "A Page admin gives the Page access token through secure entry.",
        "Replies are free within 24 hours; after that only a person can reply, with the human agent tag, up to 7 days.",
    ],
    "telegram": [
        "Create a bot with @BotFather in Telegram and copy its token.",
        "Enter the token through secure entry here. It is stored encrypted and never shown again.",
        "Connect: CommAI sets the bot's webhook with a secret only Telegram and CommAI know.",
        "People must start the bot first. /stop, or blocking the bot, opts them out.",
    ],
}


def _no_internal(conn, user, customer_id) -> None:
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change channel settings.")


def _base(request: Request) -> str:
    return os.environ.get("EXA_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


def _hook_url(request: Request, a: dict) -> str:
    if a["channel"] == "telegram":
        return f"{_base(request)}/api/v1/commai/channels/telegram/hooks/{a['hook_token']}"
    if a["provider"] == "meta":
        return f"{_base(request)}/api/v1/commai/channels/meta/webhook"
    return f"{_base(request)}/api/v1/commai/channels/meta/hooks/{a['hook_token']}"


def _out(request: Request, a: dict, secret: bool = False) -> dict:
    prov = social.PROVIDERS[a["provider"]]
    s = a["settings"] or {}
    out = {k: a[k] for k in ("id", "channel", "provider", "name", "address", "status", "last_inbound_at",
                             "last_sent_at", "last_error", "last_error_at", "created_at")}  # fmt: skip
    out.update(
        provider_label=prov.label,
        simulated=prov.simulated,
        missing=prov.missing(a),
        webhook_url=_hook_url(request, a),
        token_saved=bool(s.get("token_ref")),
        token_saved_at=s.get("token_saved_at"),
        connected_at=s.get("connected_at"),
        connection=s.get("connection") or {},
    )
    if secret:
        out["secret"] = a["hook_secret"]
    return out


def _get(conn, customer_id: str, account_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM channel_accounts WHERE id = %s AND customer_id = %s AND channel = ANY(%s)",
        (account_id, customer_id, list(SOCIAL)),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Account not found.")
    return row


# =====================================================================================
# Messenger, Instagram and Telegram accounts
# =====================================================================================


@router.get("/social-accounts")
def list_social(customer_id: str, request: Request, user: UserDep) -> dict:
    """The business's Messenger, Instagram and Telegram accounts, whether each
    channel is switched on for it, and what each needs."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_accounts WHERE customer_id = %s AND channel = ANY(%s) ORDER BY channel, created_at",
            (customer_id, list(SOCIAL)),
        ).fetchall()
        chans = {
            c: {
                "label": social.LABELS[c],
                "available": golive.enabled(conn, "channel", c, customer_id),
                "needs": NEEDS[c],
            }
            for c in SOCIAL
        }
    return {"channels": chans, "accounts": [_out(request, a) for a in rows]}


class SocialIn(BaseModel):
    channel: Literal["messenger", "instagram", "telegram"]
    simulated: bool = True
    name: str = Field(default="", max_length=80)
    address: str = Field(min_length=3, max_length=64, description="Page id, Instagram account id, or @bot_username")


def _address(channel: str, raw: str) -> str:
    raw = raw.strip()
    if channel == "telegram":
        name = raw.lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name) or not name.lower().endswith("bot"):
            raise HTTPException(422, "Give the bot's username, like @ExampleBankBot (bot usernames end in 'bot').")
        return "@" + name
    if not re.fullmatch(r"\d{5,25}", raw):
        what = "Page id" if channel == "messenger" else "Instagram account id"
        raise HTTPException(422, f"Give the {what}: digits only, as Meta shows it.")
    return raw


@router.post("/social-accounts", status_code=201)
def create_social(customer_id: str, body: SocialIn, request: Request, user: UserDep) -> dict:
    """Add a Messenger Page, Instagram account or Telegram bot. Refused until the
    channel is switched on for this business. Simulated accounts are live at
    once; real ones start in setup."""
    access.check(user, customer_id, "commai:admin")
    address = _address(body.channel, body.address)
    prov = social.provider_for(body.channel, body.simulated)
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        try:
            golive.require(conn, "channel", body.channel, customer_id)
        except golive.GoLiveError as e:
            raise HTTPException(409, str(e)) from e
        if conn.execute(
            "SELECT 1 FROM channel_accounts WHERE channel = %s AND address = %s"
            " AND (customer_id = %s OR provider = 'meta')",
            (body.channel, address, customer_id),
        ).fetchone():
            raise HTTPException(409, "That account is already connected.")
        row = conn.execute(
            """INSERT INTO channel_accounts (customer_id, channel, provider, name, address, status, settings,
                                             hook_token, hook_secret, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, '{}', %s, %s, %s) RETURNING *""",
            (customer_id, body.channel, prov.name, body.name, address, "live" if prov.simulated else "setup",
             messaging.new_hook_token(), "chs_" + secrets.token_urlsafe(24), user.actor),
        ).fetchone()  # fmt: skip
        audit.record(
            conn, user.actor, "commai.social_account.create", f"{body.channel}:{address}", customer_id,
            {"provider": prov.name},
        )  # fmt: skip
    # The secret signs simulated webhooks; real platforms use their own secret.
    return _out(request, row, secret=prov.simulated)


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=600)


@router.put("/social-accounts/{account_id}/token", status_code=204)
def save_token(customer_id: str, account_id: str, body: TokenIn, user: UserDep) -> None:
    """Secure entry: the Telegram bot token or the Meta Page access token. Stored
    encrypted in the vault, never returned, never logged."""
    from ..automation import vault

    access.check(user, customer_id, "commai:admin")
    token = body.token.strip()
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        a = _get(conn, customer_id, account_id)
        if a["channel"] == "telegram" and not social.TELEGRAM_TOKEN.fullmatch(token):
            raise HTTPException(
                422, "That doesn't look like a bot token from @BotFather (digits, a colon, then letters)."
            )
        if a["channel"] != "telegram" and not re.fullmatch(r"[A-Za-z0-9_\-|.]{20,600}", token):
            raise HTTPException(422, "That doesn't look like a Page access token.")
        try:
            social.save_token(conn, a, token, user.actor)
        except vault.VaultError as e:
            raise HTTPException(409, str(e)) from e
        audit.record(
            conn, user.actor, "commai.social_account.token_saved", f"{a['channel']}:{a['address']}", customer_id
        )


@router.post("/social-accounts/{account_id}/connect")
def connect_social(customer_id: str, account_id: str, request: Request, user: UserDep) -> dict:
    """Point the platform at CommAI's webhook (Telegram setWebhook; Meta page
    subscription). Real platforms need the token first and the channel switched on."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        a = _get(conn, customer_id, account_id)
        try:
            golive.require(conn, "channel", a["channel"], customer_id)
        except golive.GoLiveError as e:
            raise HTTPException(409, str(e)) from e
        prov = social.PROVIDERS[a["provider"]]
        if prov.missing(a):
            raise HTTPException(409, "Not ready to connect: " + ", ".join(prov.missing(a)) + ".")
        try:
            facts = prov.connect(conn, a, _hook_url(request, a))
        except providers.ProviderError as e:
            raise HTTPException(502, str(e)) from e
        settings = {**(a["settings"] or {}), "connected_at": db_now(conn), "connection": facts}
        row = conn.execute(
            "UPDATE channel_accounts SET settings = %s, updated_at = now() WHERE id = %s RETURNING *",
            (Jsonb(settings), a["id"]),
        ).fetchone()
        events.emit(
            conn, customer_id, "channel.connected", {"account_id": str(a["id"]), "channel": a["channel"]}, a["id"]
        )
        audit.record(conn, user.actor, "commai.social_account.connect", f"{a['channel']}:{a['address']}", customer_id)
    return _out(request, row)


def db_now(conn) -> str:
    return conn.execute("SELECT now()::text AS t").fetchone()["t"]


class SocialPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    status: Literal["setup", "live", "paused"] | None = None


@router.patch("/social-accounts/{account_id}")
def update_social(customer_id: str, account_id: str, body: SocialPatch, request: Request, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        a = _get(conn, customer_id, account_id)
        if body.status == "live":
            try:
                golive.require(conn, "channel", a["channel"], customer_id)
            except golive.GoLiveError as e:
                raise HTTPException(409, str(e)) from e
            prov = social.PROVIDERS[a["provider"]]
            if prov.missing(a):
                raise HTTPException(409, "It can't go live yet: " + ", ".join(prov.missing(a)) + ".")
            if not prov.simulated and not (a["settings"] or {}).get("connected_at"):
                raise HTTPException(409, "Connect it first, so the platform sends messages here.")
        row = conn.execute(
            """UPDATE channel_accounts SET name = COALESCE(%s, name), status = COALESCE(%s, status), updated_at = now(),
                      last_error = CASE WHEN %s = 'live' THEN '' ELSE last_error END
               WHERE id = %s RETURNING *""",
            (body.name, body.status, body.status, a["id"]),
        ).fetchone()
        audit.record(
            conn, user.actor, "commai.social_account.update", f"{a['channel']}:{a['address']}", customer_id,
            body.model_dump(exclude_none=True),
        )  # fmt: skip
    return _out(request, row)


@router.delete("/social-accounts/{account_id}", status_code=204)
def delete_social(customer_id: str, account_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        a = _get(conn, customer_id, account_id)
        social.drop_token(conn, a)
        conn.execute("DELETE FROM channel_accounts WHERE id = %s", (a["id"],))
        audit.record(conn, user.actor, "commai.social_account.delete", f"{a['channel']}:{a['address']}", customer_id)


class SimIn(BaseModel):
    from_: str = Field(alias="from", min_length=1, max_length=64, description="PSID, IGSID or Telegram chat id")
    name: str = Field(default="", max_length=120)
    body: str = Field(min_length=1, max_length=4000)
    id: str = Field(default="", max_length=120, description="The platform's message id; repeat it to test duplicates")


@router.post("/social-accounts/{account_id}/simulate-inbound")
def simulate_social_inbound(customer_id: str, account_id: str, body: SimIn, user: UserDep) -> dict:
    """A message from a person, in the platform's own webhook format and signed
    the platform's way, through the same handler. Simulated accounts only."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        a = _get(conn, customer_id, account_id)
        if not social.PROVIDERS[a["provider"]].simulated:
            raise HTTPException(409, "Only simulated accounts can be sent test messages from here.")
        mid = body.id or (str(secrets.randbelow(10**9)) if a["channel"] == "telegram" else "m_" + secrets.token_hex(10))
        raw = json.dumps(social.sim_payload(a, body.from_, body.body, mid, body.name)).encode()
        req = providers.InboundRequest(url="", headers=social.sim_headers(a, raw), body=raw)
        try:
            got = social.handle_webhook(conn, a, req)
        except social.ChannelOff as e:
            raise HTTPException(409, str(e)) from e
        audit.record(conn, user.actor, "commai.social_account.simulate_inbound", str(a["id"]), customer_id)
    return got


class SimReceiptIn(BaseModel):
    message_id: str
    status: Literal["delivered", "read"]


@router.post("/social-accounts/{account_id}/simulate-receipt")
def simulate_social_receipt(customer_id: str, account_id: str, body: SimReceiptIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        a = _get(conn, customer_id, account_id)
        if not social.PROVIDERS[a["provider"]].simulated:
            raise HTTPException(409, "Only simulated accounts take test receipts.")
        if a["channel"] == "telegram":
            raise HTTPException(409, "Telegram's Bot API has no delivery or read receipts.")
        if a["channel"] == "instagram" and body.status == "delivered":
            raise HTTPException(409, "Instagram sends read receipts only.")
        m = conn.execute(
            """SELECT m.provider_ref, ci.address FROM messages m JOIN conversations c ON c.id = m.conversation_id
               JOIN contact_identities ci ON ci.id = c.identity_id WHERE m.id = %s AND m.customer_id = %s""",
            (body.message_id, customer_id),
        ).fetchone()
        if m is None or not m["provider_ref"]:
            raise HTTPException(404, "That message hasn't been sent yet.")
        raw = json.dumps(social.sim_receipt_payload(a, m["address"], body.status, m["provider_ref"])).encode()
        req = providers.InboundRequest(url="", headers=social.sim_headers(a, raw), body=raw)
        try:
            return social.handle_webhook(conn, a, req)
        except social.ChannelOff as e:
            raise HTTPException(409, str(e)) from e


# =====================================================================================
# Countries and SMS senders (the business's view)
# =====================================================================================


@router.get("/countries")
def list_countries(customer_id: str, user: UserDep) -> list[dict]:
    """What is available where. Everything in the matrix is unverified research
    until ExaCarib confirms it; `available` says whether the country is switched on for you."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return countries.matrix_for(conn, customer_id)


class SenderIn(BaseModel):
    sender: str = Field(min_length=3, max_length=20)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    note: str = Field(default="", max_length=500)


@router.get("/sms-senders")
def list_senders(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM sms_sender_registrations WHERE customer_id = %s ORDER BY created_at DESC", (customer_id,)
        ).fetchall()


@router.post("/sms-senders", status_code=201)
def request_sender(customer_id: str, body: SenderIn, user: UserDep) -> dict:
    """Ask for a sender registration a country requires (US 10DLC, toll-free
    verification). ExaCarib files it with the carrier and records the outcome."""
    access.check(user, customer_id, "commai:admin")
    sender = providers.e164(body.sender) if not re.search(r"[A-Za-z]", body.sender) else body.sender.strip()
    kind = countries.sender_kind(sender)
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        rules = countries.sms_rules(conn, body.country)
        if rules is None:
            raise HTTPException(422, f"{body.country} is not in the country list.")
        need = (rules.get("registration") or {}).get(kind)
        if not need:
            raise HTTPException(
                422, f"{countries.name_of(body.country)} needs no registration for {countries.SENDER_LABEL[kind]}."
            )
        row = conn.execute(
            """INSERT INTO sms_sender_registrations (customer_id, sender, country, kind, note, created_by)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, sender, country) DO UPDATE SET note = EXCLUDED.note, updated_at = now()
               RETURNING *""",
            (customer_id, sender, body.country, kind, body.note, user.actor),
        ).fetchone()
        audit.record(
            conn, user.actor, "commai.sms_sender.request", f"{body.country}:{sender}", customer_id, {"kind": kind}
        )
    return {**row, "requirement": need}


@router.get("/sms-route-attempts")
def list_attempts(customer_id: str, user: UserDep) -> list[dict]:
    """The business's recent SMS hand-offs to carriers (failovers show here)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return sms_routing.route_attempts(conn, customer_id)


# =====================================================================================
# ExaCarib admin: routes, carriers, country rules, registrations
# =====================================================================================


class RouteIn(BaseModel):
    primary_carrier: str
    fallback_carrier: str | None = None
    primary_cost: float = Field(default=0, ge=0, le=10)
    fallback_cost: float = Field(default=0, ge=0, le=10)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


@public.get("/sms-routes")
def list_routes(user: User = Depends(require_admin)) -> list[dict]:
    with db.tx() as conn:
        return conn.execute("SELECT * FROM sms_routes ORDER BY country").fetchall()


@public.put("/sms-routes/{country}")
def put_route(country: str, body: RouteIn, user: User = Depends(require_admin)) -> dict:
    """Set the primary and fallback carrier for a destination country."""
    if country not in countries.MATRIX:
        raise HTTPException(422, f"{country} is not in the country list.")
    for c in (body.primary_carrier, body.fallback_carrier):
        if c is not None and c not in sms_routing.CARRIERS:
            raise HTTPException(422, f"Unknown carrier {c}. Known: {', '.join(sms_routing.CARRIERS)}.")
    if body.fallback_carrier == body.primary_carrier:
        raise HTTPException(422, "The fallback must be a different carrier.")
    with db.tx() as conn:
        row = conn.execute(
            """INSERT INTO sms_routes (country, primary_carrier, fallback_carrier, primary_cost, fallback_cost,
                                      currency, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (country) DO UPDATE SET primary_carrier = EXCLUDED.primary_carrier,
                 fallback_carrier = EXCLUDED.fallback_carrier, primary_cost = EXCLUDED.primary_cost,
                 fallback_cost = EXCLUDED.fallback_cost, currency = EXCLUDED.currency,
                 updated_by = EXCLUDED.updated_by, updated_at = now()
               RETURNING *""",
            (country, body.primary_carrier, body.fallback_carrier, body.primary_cost, body.fallback_cost,
             body.currency, user.actor),
        ).fetchone()  # fmt: skip
        audit.record(conn, user.actor, "commai.sms_route.set", country, None, body.model_dump())
    return row


@public.delete("/sms-routes/{country}", status_code=204)
def delete_route(country: str, user: User = Depends(require_admin)) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM sms_routes WHERE country = %s", (country,))
        audit.record(conn, user.actor, "commai.sms_route.delete", country, None)


@public.get("/sms-carriers")
def list_carriers(user: User = Depends(require_admin)) -> list[dict]:
    with db.tx() as conn:
        faults = {r["carrier"]: r["mode"] for r in conn.execute("SELECT carrier, mode FROM sms_sim_faults").fetchall()}
        out = []
        for c in sms_routing.CARRIERS.values():
            cap = golive.get(conn, "carrier", c.key) or {}
            out.append({"key": c.key, "label": c.label, "simulated": c.simulated, "missing_env": c.missing(),
                        "status": cap.get("status", "off"), "fault": faults.get(c.key)})  # fmt: skip
    return out


class FaultIn(BaseModel):
    mode: Literal["refuse", "timeout", "timeout_after_send"] | None


@public.put("/sms-carriers/{key}/fault")
def set_fault(key: str, body: FaultIn, user: User = Depends(require_admin)) -> dict:
    """Make a simulated carrier refuse or time out, to show failover. Simulated carriers only."""
    c = sms_routing.CARRIERS.get(key)
    if c is None or not c.simulated:
        raise HTTPException(422, "Faults can only be set on simulated carriers.")
    with db.tx() as conn:
        if body.mode is None:
            conn.execute("DELETE FROM sms_sim_faults WHERE carrier = %s", (key,))
        else:
            conn.execute(
                """INSERT INTO sms_sim_faults (carrier, mode, updated_by) VALUES (%s, %s, %s)
                   ON CONFLICT (carrier) DO UPDATE SET mode = EXCLUDED.mode, updated_by = EXCLUDED.updated_by,
                     updated_at = now()""",
                (key, body.mode, user.actor),
            )
        audit.record(conn, user.actor, "commai.sms_carrier.fault", key, None, {"mode": body.mode})
    return {"key": key, "fault": body.mode}


@public.put("/countries/{code}/sms-rules")
def set_country_rules(code: str, body: dict, user: User = Depends(require_admin)) -> dict:
    """Adjust a country's SMS rules (rate_per_minute, quiet_hours, sender_id_types, registration)."""
    if code not in countries.MATRIX:
        raise HTTPException(404, "Not in the country list.")
    try:
        rules = countries.check_override(body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO country_sms_overrides (country, rules, updated_by) VALUES (%s, %s, %s)
               ON CONFLICT (country) DO UPDATE SET rules = country_sms_overrides.rules || EXCLUDED.rules,
                 updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (code, Jsonb(rules), user.actor),
        )
        audit.record(conn, user.actor, "commai.country.sms_rules", code, None, rules)
        return countries.sms_rules(conn, code) or {}


class DecisionIn(BaseModel):
    status: Literal["pending", "approved", "rejected"]
    reference: str = Field(default="", max_length=200)


@public.get("/sms-senders")
def all_senders(user: User = Depends(require_admin), status: str | None = None) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            """SELECT * FROM sms_sender_registrations WHERE (%(s)s::text IS NULL OR status = %(s)s)
               ORDER BY created_at DESC LIMIT 500""",
            {"s": status},
        ).fetchall()


@public.put("/sms-senders/{reg_id}")
def decide_sender(reg_id: str, body: DecisionIn, user: User = Depends(require_admin)) -> dict:
    """Record the outcome of a registration ExaCarib filed (10DLC, toll-free verification)."""
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE sms_sender_registrations SET status = %s, reference = %s, decided_by = %s, updated_at = now()
               WHERE id::text = %s RETURNING *""",
            (body.status, body.reference, user.actor, reg_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No such registration.")
        audit.record(conn, user.actor, "commai.sms_sender.decide", f"{row['country']}:{row['sender']}",
                     row["customer_id"], body.model_dump())  # fmt: skip
    return row


# =====================================================================================
# Platform webhooks (no sign-in; signature checked)
# =====================================================================================


async def _hook(request: Request, hook_token: str, channels_ok: tuple[str, ...]):
    raw = await request.body()
    if len(raw) > 2_000_000:
        return JSONResponse({"detail": "Too large."}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}

    def run() -> tuple[Any, Any]:
        with db.tx() as conn:
            a = conn.execute(
                "SELECT * FROM channel_accounts WHERE hook_token = %s AND channel = ANY(%s)",
                (hook_token, list(channels_ok)),
            ).fetchone()
            if a is None:
                return None, "missing"
            try:
                return a, social.handle_webhook(conn, a, providers.InboundRequest(url="", headers=headers, body=raw))
            except PermissionError:
                return a, "rejected"
            except social.ChannelOff:
                return a, "off"
            except (ValueError, KeyError, TypeError) as e:
                messaging.log_webhook(conn, a, "unreadable", type(e).__name__)
                return a, "unreadable"

    # A refused webhook is still logged: run() commits its log row before answering.
    a, out = await run_in_threadpool(run)
    if a is None:
        return JSONResponse({"detail": "Unknown webhook."}, status_code=404)
    if out == "rejected":
        return JSONResponse({"detail": "Signature did not verify."}, status_code=403)
    if out == "off":
        return JSONResponse({"detail": "This channel is not switched on."}, status_code=403)
    if out == "unreadable":
        return JSONResponse({"detail": "The webhook body couldn't be read."}, status_code=400)
    return out


@public.post("/channels/meta/hooks/{hook_token}", include_in_schema=False)
async def meta_hook(hook_token: str, request: Request):
    """Messenger or Instagram webhooks for one account (X-Hub-Signature-256)."""
    return await _hook(request, hook_token, ("messenger", "instagram"))


@public.get("/channels/meta/hooks/{hook_token}", include_in_schema=False)
def meta_hook_verify(hook_token: str, request: Request):
    """Meta's subscription check for one account: echo hub.challenge when
    hub.verify_token is the account's webhook secret."""
    q = request.query_params
    with db.tx() as conn:
        a = conn.execute(
            "SELECT hook_secret FROM channel_accounts WHERE hook_token = %s AND channel IN ('messenger', 'instagram')",
            (hook_token,),
        ).fetchone()
    tok = q.get("hub.verify_token", "")
    if a is None or q.get("hub.mode") != "subscribe" or not tok or not secrets.compare_digest(tok, a["hook_secret"]):
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse(q.get("hub.challenge", "")[:200])


@public.post("/channels/meta/webhook", include_in_schema=False)
async def meta_app_hook(request: Request):
    """Meta's app-wide webhook for real Pages and Instagram accounts, verified with
    EXA_META_APP_SECRET and routed by Page or account id."""
    raw = await request.body()
    if len(raw) > 2_000_000:
        return JSONResponse({"detail": "Too large."}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}

    def run():
        with db.tx() as conn:
            return social.meta_app_webhook(conn, providers.InboundRequest(url="", headers=headers, body=raw))

    try:
        return await run_in_threadpool(run)
    except PermissionError:
        return JSONResponse({"detail": "Signature did not verify."}, status_code=403)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"detail": "The webhook body couldn't be read."}, status_code=400)


@public.get("/channels/meta/webhook", include_in_schema=False)
def meta_app_hook_verify(request: Request):
    q = request.query_params
    want = os.environ.get("EXA_META_VERIFY_TOKEN", "")
    tok = q.get("hub.verify_token", "")
    if not want or q.get("hub.mode") != "subscribe" or not tok or not secrets.compare_digest(tok, want):
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse(q.get("hub.challenge", "")[:200])


@public.post("/channels/telegram/hooks/{hook_token}", include_in_schema=False)
async def telegram_hook(hook_token: str, request: Request):
    """Telegram updates for one bot (X-Telegram-Bot-Api-Secret-Token)."""
    return await _hook(request, hook_token, ("telegram",))


# =====================================================================================
# WhatsApp through Meta's Cloud API, interactive messages and click-to-chat links
# =====================================================================================

WA_CLOUD_NEEDS = [
    "ExaCarib's Meta app must be a Meta Tech Provider with WhatsApp permissions approved in app review "
    "(ExaCarib does this once).",
    "Your business completes Meta business verification and has a WhatsApp Business Account, or creates one "
    "during sign-up.",
    "A number that can receive a verification code by SMS or call and is not in use on the WhatsApp app.",
    "You connect through Meta's Embedded Signup window; CommAI keeps the business token encrypted and never shows it.",
    "Templates are submitted to Meta from here; their approval comes back from Meta.",
]


def _wa_cloud(conn, customer_id: str, account_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM channel_accounts WHERE id = %s AND customer_id = %s AND channel = 'whatsapp'"
        " AND provider = ANY(%s)",
        (account_id, customer_id, list(whatsapp_cloud.CLOUD)),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Cloud API account not found.")
    return row


def _wa_out(request: Request, a: dict, secret: bool = False) -> dict:
    prov = whatsapp_cloud.PROVIDERS[a["provider"]]
    s = a["settings"] or {}
    out = {k: a[k] for k in ("id", "channel", "provider", "name", "address", "status", "last_inbound_at",
                             "last_sent_at", "last_error", "created_at")}  # fmt: skip
    hook = (
        f"{_base(request)}/api/v1/commai/channels/meta/webhook"
        if a["provider"] == "meta-cloud"
        else f"{_base(request)}/api/v1/commai/channels/whatsapp-cloud/hooks/{a['hook_token']}"
    )
    out.update(provider_label=prov.label, simulated=prov.simulated, missing=prov.missing(a), webhook_url=hook,
               waba_id=s.get("waba_id", ""), phone_number_id=s.get("phone_number_id", ""),
               verified_name=s.get("verified_name", ""), token_saved=bool(s.get("token_ref")))  # fmt: skip
    if secret:
        out["secret"] = a["hook_secret"]
    return out


def _require_wa_cloud(conn, customer_id: str) -> None:
    try:
        golive.require(conn, "channel", whatsapp_cloud.KEY, customer_id)
    except golive.GoLiveError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/whatsapp-cloud")
def wa_cloud_setup(customer_id: str, request: Request, user: UserDep) -> dict:
    """Whether the Cloud API is switched on for this business, what it needs, and
    the public Embedded Signup settings (app id and configuration id, never a secret)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_accounts WHERE customer_id = %s AND channel = 'whatsapp' AND provider = ANY(%s)"
            " ORDER BY created_at",
            (customer_id, list(whatsapp_cloud.CLOUD)),
        ).fetchall()
        available = golive.enabled(conn, "channel", whatsapp_cloud.KEY, customer_id)
    missing = whatsapp_cloud.CloudApi.signup_env_missing()
    return {
        "available": available,
        "needs": WA_CLOUD_NEEDS,
        "embedded_signup": {
            "ready": not missing,
            "missing_env": missing,
            "app_id": os.environ.get("EXA_META_APP_ID", ""),
            "config_id": os.environ.get("EXA_META_ES_CONFIG_ID", ""),
        },
        "accounts": [_wa_out(request, a) for a in rows],
    }


class SignupIn(BaseModel):
    simulated: bool = True
    number: str = Field(default="", max_length=30, description="Simulated sign-up only: the number to stand in")
    code: str = Field(default="", max_length=1000, description="Embedded Signup: the code from FB.login")
    waba_id: str = Field(default="", pattern=r"^\d{0,25}$")
    phone_number_id: str = Field(default="", pattern=r"^\d{0,25}$")
    pin: str = Field(
        default="", pattern=r"^(\d{6})?$", description="Two-step verification PIN; used once, never stored"
    )
    name: str = Field(default="", max_length=80)


@router.post("/whatsapp-cloud/signup", status_code=201)
def wa_cloud_signup(customer_id: str, body: SignupIn, request: Request, user: UserDep) -> dict:
    """Connect a WhatsApp number through Embedded Signup (real), or the simulated
    stand-in. Refused until the Cloud API is switched on for this business."""
    from ..automation import vault

    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        _require_wa_cloud(conn, customer_id)
    if body.simulated:
        number = providers.e164(body.number)
        if len(number) < 8:
            raise HTTPException(422, "Give the number in international form, like +18685550100.")
        prov_name = "meta-cloud-simulated"
        settings = {
            "waba_id": str(10**14 + secrets.randbelow(10**14)),
            "phone_number_id": str(10**14 + secrets.randbelow(10**14)),
        }
        got: dict = {}
    else:
        if not (body.code and body.waba_id and body.phone_number_id):
            raise HTTPException(422, "Embedded Signup returns a code, a WABA id and a phone number id; send all three.")
        if not vault.configured():
            raise HTTPException(409, "Secure storage is not set up: the controller needs EXA_SECRETS_KEY.")
        try:
            got = whatsapp_cloud.PROVIDERS["meta-cloud"].signup(body.code, body.waba_id, body.phone_number_id, body.pin)
        except providers.ProviderError as e:
            raise HTTPException(502 if "answered" in str(e) or "reached" in str(e) else 409, str(e)) from e
        number = got["number"]
        prov_name = "meta-cloud"
        settings = {
            "waba_id": body.waba_id,
            "phone_number_id": body.phone_number_id,
            "verified_name": got["verified_name"],
        }
    with db.tx() as conn:
        if conn.execute(
            "SELECT 1 FROM channel_accounts WHERE channel = 'whatsapp' AND address = %s AND customer_id = %s",
            (number, customer_id),
        ).fetchone() or (
            prov_name == "meta-cloud"
            and conn.execute(
                "SELECT 1 FROM channel_accounts WHERE provider = 'meta-cloud' AND settings->>'phone_number_id' = %s",
                (body.phone_number_id,),
            ).fetchone()
        ):
            raise HTTPException(409, "That WhatsApp number is already connected.")
        row = conn.execute(
            """INSERT INTO channel_accounts (customer_id, channel, provider, name, address, status, settings,
                                             hook_token, hook_secret, created_by)
               VALUES (%s, 'whatsapp', %s, %s, %s, 'live', %s, %s, %s, %s) RETURNING *""",
            (customer_id, prov_name, body.name, number, Jsonb(settings), messaging.new_hook_token(),
             "chs_" + secrets.token_urlsafe(24), user.actor),
        ).fetchone()  # fmt: skip
        if got.get("token"):
            row["settings"] = social.save_token(conn, row, got["token"], user.actor)
        audit.record(conn, user.actor, "commai.whatsapp_cloud.signup", f"whatsapp:{number}", customer_id,
                     {"provider": prov_name, "waba_id": settings["waba_id"]})  # fmt: skip
    return _wa_out(request, row, secret=body.simulated)


class WaSimIn(BaseModel):
    from_: str = Field(alias="from", min_length=8, max_length=20)
    name: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=4000)
    id: str = Field(default="", max_length=120)
    button: dict | None = Field(default=None, description='Reply to buttons: {"id", "title"}')


def _wa_sim_post(conn, a: dict, payload: dict) -> dict:
    if not whatsapp_cloud.PROVIDERS[a["provider"]].simulated:
        raise HTTPException(409, "Only simulated accounts take test traffic from here.")
    raw = json.dumps(payload).encode()
    req = providers.InboundRequest(
        url="", headers={"x-hub-signature-256": social.meta_sign(a["hook_secret"], raw)}, body=raw
    )
    try:
        return whatsapp_cloud.handle_webhook(conn, a, req)
    except social.ChannelOff as e:
        raise HTTPException(409, str(e)) from e


@router.post("/whatsapp-cloud/{account_id}/simulate-inbound")
def wa_cloud_sim_inbound(customer_id: str, account_id: str, body: WaSimIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    if not body.body and not body.button:
        raise HTTPException(422, "Give a message or a button reply.")
    with db.tx() as conn:
        a = _wa_cloud(conn, customer_id, account_id)
        payload = whatsapp_cloud.sim_payload(
            a, body.from_, body.body, body.id or "wamid.IN" + secrets.token_hex(10), body.name, body.button
        )
        return _wa_sim_post(conn, a, payload)


class WaSimStatus(BaseModel):
    message_id: str
    status: Literal["sent", "delivered", "read", "failed"]


@router.post("/whatsapp-cloud/{account_id}/simulate-status")
def wa_cloud_sim_status(customer_id: str, account_id: str, body: WaSimStatus, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        a = _wa_cloud(conn, customer_id, account_id)
        m = conn.execute(
            """SELECT m.provider_ref, ci.address FROM messages m JOIN conversations c ON c.id = m.conversation_id
               JOIN contact_identities ci ON ci.id = c.identity_id WHERE m.id = %s AND m.customer_id = %s""",
            (body.message_id, customer_id),
        ).fetchone()
        if m is None or not m["provider_ref"]:
            raise HTTPException(404, "That message hasn't been sent yet.")
        return _wa_sim_post(conn, a, whatsapp_cloud.sim_status_payload(a, m["provider_ref"], body.status, m["address"]))


class WaSimTemplate(BaseModel):
    template_id: str
    event: Literal["APPROVED", "REJECTED", "PAUSED", "DISABLED"]
    reason: str = Field(default="", max_length=200)


@router.post("/whatsapp-cloud/{account_id}/simulate-template-status")
def wa_cloud_sim_template(customer_id: str, account_id: str, body: WaSimTemplate, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        a = _wa_cloud(conn, customer_id, account_id)
        tpl = conn.execute(
            "SELECT * FROM whatsapp_templates WHERE id::text = %s AND customer_id = %s", (body.template_id, customer_id)
        ).fetchone()
        if tpl is None or not tpl["provider_template_id"]:
            raise HTTPException(404, "Submit the template first.")
        return _wa_sim_post(conn, a, whatsapp_cloud.sim_template_payload(a, tpl, body.event, body.reason))


@router.post("/whatsapp-cloud/{account_id}/templates/{template_id}/submit")
def wa_cloud_submit_template(customer_id: str, account_id: str, template_id: str, user: UserDep) -> dict:
    """Submit one of the business's templates to Meta for review."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        _require_wa_cloud(conn, customer_id)
        a = _wa_cloud(conn, customer_id, account_id)
        tpl = conn.execute(
            "SELECT * FROM whatsapp_templates WHERE id::text = %s AND customer_id = %s", (template_id, customer_id)
        ).fetchone()
        if tpl is None:
            raise HTTPException(404, "Template not found.")
        try:
            row = whatsapp_cloud.submit_template(conn, a, tpl)
        except providers.ProviderError as e:
            raise HTTPException(502, str(e)) from e
        audit.record(conn, user.actor, "commai.whatsapp_cloud.template_submit", f"{tpl['name']}:{tpl['language']}",
                     customer_id)  # fmt: skip
    return row


@router.post("/whatsapp-cloud/{account_id}/templates/sync")
def wa_cloud_sync_templates(customer_id: str, account_id: str, user: UserDep) -> dict:
    """Pull every template's approval status from the WhatsApp Business Account."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _require_wa_cloud(conn, customer_id)
        a = _wa_cloud(conn, customer_id, account_id)
        try:
            out = whatsapp_cloud.sync_templates(conn, a)
        except providers.ProviderError as e:
            raise HTTPException(502, str(e)) from e
        audit.record(conn, user.actor, "commai.whatsapp_cloud.template_sync", str(a["id"]), customer_id, out)
    return out


class MediaIn(BaseModel):
    filename: str = Field(min_length=1, max_length=120)
    content_type: str = Field(pattern=r"^(image/(jpeg|png)|application/pdf|text/plain|audio/(ogg|mpeg|aac)|video/mp4)$")
    data: str = Field(min_length=4, max_length=7_000_000, description="base64")


@router.post("/whatsapp-cloud/{account_id}/media", status_code=201)
def wa_cloud_upload(customer_id: str, account_id: str, body: MediaIn, user: UserDep) -> dict:
    """Upload media to Meta for sending (up to 5 MB here)."""
    access.check(user, customer_id, "commai:write")
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(422, "The file must be base64.") from e
    if len(data) > 5_000_000:
        raise HTTPException(413, "Files up to 5 MB.")
    with db.tx() as conn:
        _require_wa_cloud(conn, customer_id)
        a = _wa_cloud(conn, customer_id, account_id)
        try:
            mid = whatsapp_cloud.PROVIDERS[a["provider"]].upload_media(conn, a, data, body.content_type, body.filename)
        except providers.ProviderError as e:
            raise HTTPException(502, str(e)) from e
        audit.record(conn, user.actor, "commai.whatsapp_cloud.media_upload", mid, customer_id, {"size": len(data)})
    return {"media_id": mid}


@router.get("/whatsapp-cloud/{account_id}/media/{media_id}")
def wa_cloud_download(customer_id: str, account_id: str, media_id: str, user: UserDep) -> Response:
    """Fetch media a customer sent (its id comes with the inbound message)."""
    access.check(user, customer_id, "commai:read")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", media_id):
        raise HTTPException(422, "Not a media id.")
    with db.tx() as conn:
        a = _wa_cloud(conn, customer_id, account_id)
        try:
            data, ctype = whatsapp_cloud.PROVIDERS[a["provider"]].download_media(conn, a, media_id)
        except providers.ProviderError as e:
            raise HTTPException(404, str(e)) from e
    return Response(
        data, media_type=ctype, headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"}
    )


class InteractiveIn(BaseModel):
    body: str = Field(min_length=1, max_length=1024)
    buttons: list[dict] | None = Field(default=None, max_length=3)
    list_: dict | None = Field(default=None, alias="list")


@router.post("/conversations/{conversation_id}/interactive", status_code=201)
def send_interactive(customer_id: str, conversation_id: str, body: InteractiveIn, user: UserDep) -> dict:
    """Reply with WhatsApp reply buttons or a list (inside the 24-hour window)."""
    access.check(user, customer_id, "commai:write")
    try:
        spec = whatsapp_cloud.check_interactive(body.model_dump(by_alias=True))
    except whatsapp_cloud.InteractiveError as e:
        raise HTTPException(422, str(e)) from e
    with db.tx() as conn:
        access.require_reply_seat(conn, user, customer_id)
        try:
            msg = whatsapp_cloud.send_interactive_reply(conn, customer_id, conversation_id, spec, author=user.email,
                                                        user_id=user.id)  # fmt: skip
        except inbox.InboxError as e:
            raise HTTPException(e.code, str(e)) from e
        audit.record(conn, user.actor, "commai.message.send", str(msg["id"]), customer_id,
                     {"conversation_id": conversation_id, "interactive": spec["kind"]})  # fmt: skip
    return msg


@router.get("/whatsapp-link")
def whatsapp_link(customer_id: str, user: UserDep, account_id: str, text: str = "") -> dict:
    """A click-to-WhatsApp link (wa.me) for one of the business's WhatsApp numbers,
    with a QR code as SVG for posters, receipts and the website."""
    access.check(user, customer_id, "commai:read")
    if len(text) > 500:
        raise HTTPException(422, "Keep the pre-filled message under 500 characters.")
    with db.tx() as conn:
        a = conn.execute(
            "SELECT address FROM channel_accounts WHERE id::text = %s AND customer_id = %s AND channel = 'whatsapp'",
            (account_id, customer_id),
        ).fetchone()
    if a is None:
        raise HTTPException(404, "WhatsApp number not found.")
    url = whatsapp_cloud.click_to_chat(a["address"], text)
    return {"url": url, "qr_svg": whatsapp_cloud.qr_svg(url)}


@public.post("/channels/whatsapp-cloud/hooks/{hook_token}", include_in_schema=False)
async def wa_cloud_hook(hook_token: str, request: Request):
    """Cloud API webhooks for one account (X-Hub-Signature-256): messages, statuses, template updates."""
    raw = await request.body()
    if len(raw) > 2_000_000:
        return JSONResponse({"detail": "Too large."}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}

    def run():
        with db.tx() as conn:
            a = conn.execute(
                "SELECT * FROM channel_accounts WHERE hook_token = %s AND provider = ANY(%s)",
                (hook_token, list(whatsapp_cloud.CLOUD)),
            ).fetchone()
            if a is None:
                return None, "missing"
            try:
                req = providers.InboundRequest(url="", headers=headers, body=raw)
                return a, whatsapp_cloud.handle_webhook(conn, a, req)
            except PermissionError:
                return a, "rejected"
            except social.ChannelOff:
                return a, "off"
            except (ValueError, KeyError, TypeError) as e:
                messaging.log_webhook(conn, a, "unreadable", type(e).__name__)
                return a, "unreadable"

    a, out = await run_in_threadpool(run)
    if a is None:
        return JSONResponse({"detail": "Unknown webhook."}, status_code=404)
    if out in ("rejected", "off"):
        detail = "Signature did not verify." if out == "rejected" else "This channel is not switched on."
        return JSONResponse({"detail": detail}, status_code=403)
    if out == "unreadable":
        return JSONResponse({"detail": "The webhook body couldn't be read."}, status_code=400)
    return out


@public.get("/channels/whatsapp-cloud/hooks/{hook_token}", include_in_schema=False)
def wa_cloud_hook_verify(hook_token: str, request: Request):
    """Meta's hub.challenge handshake for one account."""
    q = request.query_params
    with db.tx() as conn:
        a = conn.execute(
            "SELECT hook_secret FROM channel_accounts WHERE hook_token = %s AND provider = ANY(%s)",
            (hook_token, list(whatsapp_cloud.CLOUD)),
        ).fetchone()
    tok = q.get("hub.verify_token", "")
    if a is None or q.get("hub.mode") != "subscribe" or not tok or not secrets.compare_digest(tok, a["hook_secret"]):
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse(q.get("hub.challenge", "")[:200])
