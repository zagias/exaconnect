"""CommAI phase 3 channels API (ADR 0023): Messenger, Instagram and Telegram
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

import json
import os
import re
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ... import audit, db
from ...api.deps import User, UserDep, require_admin
from .. import access, events, golive
from ..channels import countries, messaging, providers, sms_routing, social

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
