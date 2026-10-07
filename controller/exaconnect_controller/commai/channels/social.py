"""Facebook Messenger, Instagram direct messages and Telegram (ADR 0023).

Three more channels on the phase 2 interface (ADR 0018): accounts live in
``channel_accounts``, inbound messages go through ``messaging.receive_one`` (a
repeated webhook never stores a message twice, STOP words opt out), and replies
go out through ``Channel.deliver`` from the durable send job. Private notes are
in another table and never reach a channel.

Each channel is declared in the go-live registry (kind "channel") and refuses
to connect, send or accept webhooks for a business until it is switched on for
them.

Rules
- Messenger: free-form replies within 24 hours of the person's last message
  (Meta's standard messaging window). Outside it, only with a message tag:
  HUMAN_AGENT (a person, not the AI, within 7 days), or CONFIRMED_EVENT_UPDATE,
  POST_PURCHASE_UPDATE, ACCOUNT_UPDATE. A business cannot write first.
- Instagram: the same window; outside it only the HUMAN_AGENT tag, within 7 days.
- Telegram: no window. The bot can only write to people who started it, so
  conversations always begin with them. /stop, or blocking the bot, opts out;
  /start opts back in.
- Tags are only for people: an AI reply outside the window is refused.

Providers (behind ``providers.Provider``)
- ``meta-simulated`` and ``telegram-simulated``: work with no account. They use
  the platforms' own webhook formats and signature schemes, with the account's
  webhook secret standing in for the app secret, so the same verification and
  parsing code is tested. Sends land in ``sim_channel_outbox``; a repeated send
  of the same message returns the first reference (no duplicate).
- ``meta`` and ``telegram``: written from the platforms' public documentation and
  NOT live. Meta needs ExaCarib's Meta app to pass app review, and
  EXA_META_APP_SECRET; each business connects its Page (and Instagram account)
  with a Page access token. Telegram needs the business's bot token from
  @BotFather. Tokens are entered once through secure entry, stored encrypted in
  the automation vault (``commai_secrets``) and never shown or logged.

Webhooks
- Meta: ``X-Hub-Signature-256: sha256=<hex HMAC-SHA256(app secret, raw body)>``.
  Fail closed when no secret is set.
- Telegram: ``X-Telegram-Bot-Api-Secret-Token`` equal to the secret set with
  setWebhook (the account's webhook secret). Compared in constant time.

Receipts: Messenger sends delivery and read receipts (by message id, or by a
"watermark": everything sent before it). Instagram sends read receipts.
Telegram's Bot API has none.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import channels, events, golive, inbox, usage
from . import messaging, providers
from .providers import Inbound, InboundRequest, ProviderError, Receipt

GRAPH = "https://graph.facebook.com/v21.0"
TELEGRAM_API = "https://api.telegram.org"
HUMAN_AGENT_WINDOW = dt.timedelta(days=7)
TELEGRAM_TOKEN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")

# Meta's message tags, and how long after the person's last message each may be used
# (None: no limit beyond the tag's purpose). To be rechecked against Meta's policy at
# app review: Meta has changed the list before.
TAGS: dict[str, dict[str, dt.timedelta | None]] = {
    "messenger": {
        "HUMAN_AGENT": HUMAN_AGENT_WINDOW,
        "CONFIRMED_EVENT_UPDATE": None,
        "POST_PURCHASE_UPDATE": None,
        "ACCOUNT_UPDATE": None,
    },
    "instagram": {"HUMAN_AGENT": HUMAN_AGENT_WINDOW},
}

LABELS = {"messenger": "Messenger", "instagram": "Instagram", "telegram": "Telegram"}

golive.declare(
    "channel",
    "messenger",
    "Facebook Messenger",
    {
        "meta-app-review": "ExaCarib's Meta app has passed app review with Advanced Access to pages_messaging, "
        "and Meta business verification is complete.",
        "webhook-signature": "Webhooks are verified with X-Hub-Signature-256 against the app secret; tested with "
        "good, bad and missing signatures.",
        "messaging-policy": "The 24-hour window and message-tag rules checked against Meta's current Messenger policy.",
        "token-storage": "Page access tokens are stored only in the encrypted vault and never shown or logged "
        "(reviewed).",
    },
    {"platform": "Meta Messenger Platform", "window_hours": 24, "tags": sorted(TAGS["messenger"])},
)
golive.declare(
    "channel",
    "instagram",
    "Instagram direct messages",
    {
        "meta-app-review": "ExaCarib's Meta app has passed app review with Advanced Access to "
        "instagram_manage_messages (and pages_messaging), and business verification is complete.",
        "webhook-signature": "Webhooks are verified with X-Hub-Signature-256 against the app secret; tested with "
        "good, bad and missing signatures.",
        "messaging-policy": "The 24-hour window and the human agent tag checked against Meta's current Instagram "
        "messaging policy.",
        "token-storage": "Page access tokens are stored only in the encrypted vault and never shown or logged "
        "(reviewed).",
    },
    {"platform": "Meta Messenger Platform (Instagram)", "window_hours": 24, "tags": sorted(TAGS["instagram"])},
)
golive.declare(
    "channel",
    "telegram",
    "Telegram",
    {
        "bot-token-storage": "Bot tokens are stored only in the encrypted vault and never shown or logged (reviewed).",
        "webhook-secret": "Webhooks are checked against X-Telegram-Bot-Api-Secret-Token; tested with good, bad "
        "and missing secrets.",
        "bot-terms": "Telegram's Bot API terms reviewed for business messaging.",
    },
    {"platform": "Telegram Bot API", "window_hours": None},
)

events.register("channel.connected")


class ChannelOff(Exception):
    """The channel is not switched on for this business (go-live registry)."""


class Blocked(ProviderError):
    """The person has blocked the page or bot: opt them out."""


@dataclass
class Watermark:
    address: str
    status: str  # delivered | read
    watermark_ms: int


@dataclass
class OptChange:
    address: str
    opted_out: bool
    why: str
    name: str = ""


# ---- secrets (the automation vault) ----------------------------------------------------------


def token_for(conn: psycopg.Connection, account: dict) -> str:
    from ..automation import vault

    ref = (account.get("settings") or {}).get("token_ref", "")
    got = vault.get(conn, account["customer_id"], ref) if ref else None
    if not got or not got.get("token"):
        raise ProviderError("No access token is saved for this account.")
    return str(got["token"])


def save_token(conn: psycopg.Connection, account: dict, token: str, actor: str) -> dict:
    """Encrypt and store the token; returns the account's new settings. The token
    itself is never returned, logged or audited."""
    from ..automation import vault

    settings = dict(account.get("settings") or {})
    ref = vault.put(
        conn, account["customer_id"], f"{account['channel']}_token", {"token": token}, settings.get("token_ref", "")
    )
    settings.update(token_ref=ref, token_saved_at=dt.datetime.now(dt.UTC).isoformat(), token_saved_by=actor)
    conn.execute(
        "UPDATE channel_accounts SET settings = %s, updated_at = now() WHERE id = %s", (Jsonb(settings), account["id"])
    )
    return settings


def drop_token(conn: psycopg.Connection, account: dict) -> None:
    from ..automation import vault

    vault.delete(conn, account["customer_id"], (account.get("settings") or {}).get("token_ref", ""))


# ---- providers ----------------------------------------------------------------------------


def meta_sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sim_record(conn: Any, account: dict, to: str, body: str, tag: str, ref: str) -> str:
    """A simulated send. The same message (ref) is recorded once."""
    seen = conn.execute(
        "SELECT provider_ref FROM sim_channel_outbox WHERE account_id = %s AND headers->>'ref' = %s",
        (account["id"], ref),
    ).fetchone()
    if seen:
        return seen["provider_ref"]
    if (account.get("settings") or {}).get("fail_sends"):
        raise ProviderError("Simulated failure: this account is set to fail sends.")
    pref = ("m_sim" if account["channel"] != "telegram" else "tg-sim-") + secrets.token_hex(10)
    conn.execute(
        """INSERT INTO sim_channel_outbox (customer_id, account_id, channel, to_address, body, template, headers,
                                           provider_ref) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (account["customer_id"], account["id"], account["channel"], to, body, tag, Jsonb({"ref": ref}), pref),
    )
    return pref


class SocialProvider(providers.Provider):
    """Messenger, Instagram and Telegram providers. The generic webhook path
    (/channels/hooks/{token}) is not theirs: they have their own (see the API)."""

    def parse(self, account: dict, req: InboundRequest) -> list[Inbound | Receipt]:
        raise ValueError("This channel's webhooks have their own address.")

    def events(self, account: dict, data: dict) -> list:
        raise NotImplementedError

    def send_message(self, conn: Any, account: dict, to: str, body: str, *, tag: str, ref: str) -> str:
        raise NotImplementedError

    def connect(self, conn: Any, account: dict, webhook_url: str) -> dict:
        """Point the platform at our webhook. Returns facts to show (never a token)."""
        raise NotImplementedError


class Meta(SocialProvider):
    """Meta's Messenger Platform, for Messenger and Instagram. NOT live: needs
    ExaCarib's Meta app (app review passed) with EXA_META_APP_SECRET set, and the
    business's Page access token saved through secure entry.

    Send:    POST {GRAPH}/me/messages, Authorization: Bearer <page token>,
             {"recipient": {"id": PSID or IGSID}, "messaging_type": "RESPONSE" |
             "MESSAGE_TAG", "tag": ..., "message": {"text": ...}}
    Connect: POST {GRAPH}/{page-id}/subscribed_apps?subscribed_fields=...
    """

    name = "meta"
    label = "Meta"
    channels = ("messenger", "instagram")

    def app_secret(self, account: dict) -> str:
        return os.environ.get("EXA_META_APP_SECRET", "")

    def missing(self, account: dict) -> list[str]:
        out = [] if self.app_secret(account) else ["EXA_META_APP_SECRET"]
        if not (account.get("settings") or {}).get("token_ref"):
            out.append("the Page access token (secure entry on the Channels page)")
        return out

    def verify(self, account: dict, req: InboundRequest) -> bool:
        secret = self.app_secret(account)
        got = req.headers.get("x-hub-signature-256", "")
        if not secret or not got:
            return False
        return hmac.compare_digest(got, meta_sign(secret, req.body))

    def events(self, account: dict, data: dict) -> list:
        return [item for _, item in parse_meta(data)]

    def _graph(self, path: str, payload: dict, token: str) -> dict:  # pragma: no cover - network
        req = urllib.request.Request(f"{GRAPH}{path}", data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=providers.TIMEOUT_S) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}").get("error") or {}
            except ValueError:
                err = {}
            if err.get("code") == 551:
                raise Blocked("The person isn't available on Meta (they may have blocked the Page).") from e
            raise ProviderError(f"Meta answered {e.code}: {str(err.get('message', ''))[:200]}") from e
        except OSError as e:
            raise ProviderError(f"Meta could not be reached: {type(e).__name__}") from e

    def send_message(self, conn: Any, account: dict, to: str, body: str, *, tag: str, ref: str) -> str:
        if not self.app_secret(account):
            raise ProviderError("Meta is not configured: set EXA_META_APP_SECRET.")
        payload: dict[str, Any] = {"recipient": {"id": to}, "message": {"text": body}}
        payload.update({"messaging_type": "MESSAGE_TAG", "tag": tag} if tag else {"messaging_type": "RESPONSE"})
        out = self._graph("/me/messages", payload, token_for(conn, account))
        if not out.get("message_id"):
            raise ProviderError("Meta did not return a message id.")
        return str(out["message_id"])

    def connect(self, conn: Any, account: dict, webhook_url: str) -> dict:
        if self.missing(account):
            raise ProviderError("Meta is not ready: " + ", ".join(self.missing(account)) + ".")
        fields = (
            "messages,messaging_postbacks,message_deliveries,message_reads"
            if account["channel"] == "messenger"
            else "messages,messaging_seen"
        )
        self._graph(f"/{account['address']}/subscribed_apps?subscribed_fields={fields}", {}, token_for(conn, account))
        return {"subscribed": fields, "webhook": "app-wide (/api/v1/commai/channels/meta/webhook)"}


class MetaSimulated(Meta):
    name = "meta-simulated"
    label = "Simulated Meta"
    simulated = True

    def app_secret(self, account: dict) -> str:
        return account["hook_secret"]

    def missing(self, account: dict) -> list[str]:
        return []

    def send_message(self, conn: Any, account: dict, to: str, body: str, *, tag: str, ref: str) -> str:
        return _sim_record(conn, account, to, body, tag, ref)

    def connect(self, conn: Any, account: dict, webhook_url: str) -> dict:
        return {"subscribed": "simulated", "webhook": webhook_url}


class Telegram(SocialProvider):
    """Telegram Bot API. NOT live until the business's bot token (from @BotFather)
    is saved through secure entry and the account is connected.

    Send:    POST {TELEGRAM_API}/bot<token>/sendMessage {"chat_id", "text"}
    Connect: getMe, then setWebhook {"url", "secret_token", "allowed_updates"}
    Inbound: Update objects; private chats only.
    """

    name = "telegram"
    label = "Telegram"
    channels = ("telegram",)

    def missing(self, account: dict) -> list[str]:
        return (
            []
            if (account.get("settings") or {}).get("token_ref")
            else ["the bot token (secure entry on the Channels page)"]
        )

    def verify(self, account: dict, req: InboundRequest) -> bool:
        got = req.headers.get("x-telegram-bot-api-secret-token", "")
        return bool(got) and hmac.compare_digest(got.encode(), account["hook_secret"].encode())

    def events(self, account: dict, data: dict) -> list:
        return parse_telegram(data)

    def _call(self, token: str, method: str, payload: dict) -> dict:  # pragma: no cover - network
        req = urllib.request.Request(
            f"{TELEGRAM_API}/bot{token}/{method}", data=json.dumps(payload).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=providers.TIMEOUT_S) as r:
                out = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                desc = str(json.loads(e.read() or b"{}").get("description", ""))
            except ValueError:
                desc = ""
            if e.code == 403 and "blocked" in desc:
                raise Blocked("The person has blocked the bot.") from e
            # Never include the URL: it carries the token.
            raise ProviderError(f"Telegram answered {e.code}: {desc[:200]}") from e
        except OSError as e:
            raise ProviderError(f"Telegram could not be reached: {type(e).__name__}") from e
        if not out.get("ok"):
            raise ProviderError(f"Telegram refused: {str(out.get('description', ''))[:200]}")
        return out.get("result") or {}

    def send_message(self, conn: Any, account: dict, to: str, body: str, *, tag: str, ref: str) -> str:
        res = self._call(token_for(conn, account), "sendMessage", {"chat_id": to, "text": body})
        return f"{to}:{res.get('message_id', '')}"

    def connect(self, conn: Any, account: dict, webhook_url: str) -> dict:
        token = token_for(conn, account)
        me = self._call(token, "getMe", {})
        self._call(
            token,
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": account["hook_secret"],
                "allowed_updates": ["message", "my_chat_member"],
            },
        )
        return {"bot": "@" + str(me.get("username", "")), "webhook": webhook_url}


class TelegramSimulated(Telegram):
    name = "telegram-simulated"
    label = "Simulated Telegram"
    simulated = True

    def missing(self, account: dict) -> list[str]:
        return []

    def send_message(self, conn: Any, account: dict, to: str, body: str, *, tag: str, ref: str) -> str:
        return _sim_record(conn, account, to, body, "", ref)

    def connect(self, conn: Any, account: dict, webhook_url: str) -> dict:
        return {"bot": account["address"], "webhook": webhook_url}


PROVIDERS = {p.name: p for p in (Meta(), MetaSimulated(), Telegram(), TelegramSimulated())}
# Known to the phase 2 registry, so the shared account list and diagnostics can describe them.
providers._providers.update(PROVIDERS)


def provider_for(channel: str, simulated: bool) -> SocialProvider:
    if channel == "telegram":
        return PROVIDERS["telegram-simulated" if simulated else "telegram"]
    return PROVIDERS["meta-simulated" if simulated else "meta"]


# ---- parsing ------------------------------------------------------------------------------


def parse_meta(data: dict) -> list[tuple[str, Any]]:
    """(entry id, item) pairs from a Messenger Platform webhook (Messenger or Instagram)."""
    out: list[tuple[str, Any]] = []
    for entry in data.get("entry") or []:
        eid = str(entry.get("id", ""))
        for ev in entry.get("messaging") or []:
            sender = str((ev.get("sender") or {}).get("id", ""))
            if "message" in ev:
                m = ev.get("message") or {}
                if m.get("is_echo") or m.get("is_deleted"):
                    continue  # our own sends echoed back, or an unsent message
                atts = [
                    {"type": a.get("type", ""), "url": (a.get("payload") or {}).get("url", "")}
                    for a in (m.get("attachments") or [])[:10]
                ]
                body = m.get("text") or ("[" + ", ".join(a["type"] or "attachment" for a in atts) + "]" if atts else "")
                out.append(
                    (eid, Inbound(address=sender, body=body, external_id=str(m.get("mid", "")), attachments=atts))
                )
            elif "postback" in ev:
                p = ev.get("postback") or {}
                ext = str(p.get("mid") or f"postback:{sender}:{ev.get('timestamp', '')}")
                out.append((eid, Inbound(address=sender, body=str(p.get("title", "")), external_id=ext)))
            elif "delivery" in ev:
                d = ev.get("delivery") or {}
                for mid in d.get("mids") or []:
                    out.append((eid, Receipt(str(mid), "delivered")))
                if d.get("watermark"):
                    out.append((eid, Watermark(sender, "delivered", int(d["watermark"]))))
            elif "read" in ev:
                r = ev.get("read") or {}
                if r.get("mid"):
                    out.append((eid, Receipt(str(r["mid"]), "read")))
                if r.get("watermark"):
                    out.append((eid, Watermark(sender, "read", int(r["watermark"]))))
    return out


def _tg_name(u: dict) -> str:
    return " ".join(x for x in (u.get("first_name", ""), u.get("last_name", "")) if x).strip()


def parse_telegram(update: dict) -> list:
    """Items from one Telegram Update. Private chats only."""
    out: list = []
    m = update.get("message")
    if m:
        chat = m.get("chat") or {}
        if chat.get("type") != "private":
            return out
        chat_id = str(chat.get("id", ""))
        text = m.get("text") or m.get("caption") or ""
        atts = []
        for kind in ("photo", "document", "voice", "video", "audio", "sticker"):
            if kind in m:
                atts.append({"type": kind})
        if not text and atts:
            text = "[" + atts[0]["type"] + "]"
        name = _tg_name(m.get("from") or {})
        out.append(
            Inbound(
                address=chat_id,
                body=text,
                external_id=f"{chat_id}:{m.get('message_id', '')}",
                name=name,
                attachments=atts,
            )
        )
        cmd = text.strip().split("@", 1)[0].lower()
        if cmd == "/stop":
            out.append(OptChange(chat_id, True, "sent /stop", name))
        elif cmd == "/start":
            out.append(OptChange(chat_id, False, "sent /start", name))
    mcm = update.get("my_chat_member")
    if mcm and (mcm.get("chat") or {}).get("type") == "private":
        chat_id = str(mcm["chat"].get("id", ""))
        status = (mcm.get("new_chat_member") or {}).get("status", "")
        if status == "kicked":
            out.append(OptChange(chat_id, True, "blocked the bot", _tg_name(mcm.get("from") or {})))
        elif status == "member":
            out.append(OptChange(chat_id, False, "restarted the bot", _tg_name(mcm.get("from") or {})))
    return out


# ---- inbound ------------------------------------------------------------------------------


def check_on(conn: psycopg.Connection, account: dict) -> None:
    if not golive.enabled(conn, "channel", account["channel"], account["customer_id"]):
        raise ChannelOff(f"{LABELS[account['channel']]} is not switched on for this business.")


def handle_webhook(conn: psycopg.Connection, account: dict, req: InboundRequest) -> dict:
    """Verify and store one webhook for an account. Raises PermissionError (bad
    signature), ChannelOff, or ValueError (unreadable)."""
    prov = PROVIDERS.get(account["provider"])
    if prov is None:
        raise ValueError("Not a Messenger, Instagram or Telegram account.")
    if not prov.verify(account, req):
        messaging.log_webhook(conn, account, "rejected", "signature did not verify")
        events.emit(
            conn,
            account["customer_id"],
            "channel.webhook_rejected",
            {"account_id": str(account["id"]), "channel": account["channel"], "reason": "bad signature"},
            account["id"],
        )
        raise PermissionError("The webhook signature did not verify.")
    try:
        check_on(conn, account)
    except ChannelOff:
        messaging.log_webhook(conn, account, "refused", "channel not switched on")
        raise
    data = json.loads(req.body or b"{}")
    out = apply(conn, account, prov.events(account, data))
    messaging.log_webhook(conn, account, "accepted", f"{out}")
    return out


def apply(conn: psycopg.Connection, account: dict, items: list) -> dict:
    out = {"received": 0, "duplicates": 0, "receipts": 0, "opt_changes": 0}
    for item in items:
        if isinstance(item, Inbound):
            if not item.address or not item.external_id:
                continue
            got = messaging.receive_one(conn, account, item)
            out["duplicates" if got["duplicate"] else "received"] += 1
        elif isinstance(item, Receipt) and item.provider_ref:
            mine = conn.execute(
                "SELECT 1 FROM messages WHERE provider_ref = %s AND customer_id = %s",
                (item.provider_ref, account["customer_id"]),
            ).fetchone()
            if mine:
                inbox.update_status(conn, item.provider_ref, item.status, item.error)
                out["receipts"] += 1
        elif isinstance(item, Watermark):
            out["receipts"] += _watermark(conn, account, item)
        elif isinstance(item, OptChange) and item.address:
            ident = inbox.find_or_create_identity(
                conn, account["customer_id"], account["channel"], item.address, name=item.name
            )
            if bool(ident["opted_out"]) != item.opted_out:
                messaging.set_opt_out(conn, ident, item.opted_out, item.why)
                out["opt_changes"] += 1
    return out


def _watermark(conn: psycopg.Connection, account: dict, w: Watermark) -> int:
    """Everything sent to this person before the watermark is delivered (or read)."""
    rows = conn.execute(
        """SELECT m.provider_ref FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           JOIN contact_identities ci ON ci.id = c.identity_id
           WHERE m.customer_id = %s AND c.channel = %s AND ci.address = %s AND m.direction = 'out'
             AND m.provider_ref <> '' AND m.status IN ('sent', 'delivered')
             AND m.created_at <= to_timestamp(%s / 1000.0)""",
        (account["customer_id"], account["channel"], w.address, w.watermark_ms),
    ).fetchall()
    for r in rows:
        inbox.update_status(conn, r["provider_ref"], w.status)
    return len(rows)


def meta_app_webhook(conn: psycopg.Connection, req: InboundRequest) -> dict:
    """Meta's one app-wide webhook: verified with EXA_META_APP_SECRET, then each
    entry routed to the business that connected that Page, Instagram account or
    WhatsApp number (Cloud API)."""
    secret = os.environ.get("EXA_META_APP_SECRET", "")
    got = req.headers.get("x-hub-signature-256", "")
    if not secret or not got or not hmac.compare_digest(got, meta_sign(secret, req.body)):
        raise PermissionError("The webhook signature did not verify.")
    data = json.loads(req.body or b"{}")
    if data.get("object") == "whatsapp_business_account":
        from . import whatsapp_cloud

        return whatsapp_cloud.app_webhook(conn, data)
    channel = "instagram" if data.get("object") == "instagram" else "messenger"
    out = {"received": 0, "duplicates": 0, "receipts": 0, "opt_changes": 0, "unknown_accounts": 0, "refused": 0}
    by_entry: dict[str, list] = {}
    for eid, item in parse_meta(data):
        by_entry.setdefault(eid, []).append(item)
    for eid, items in by_entry.items():
        a = conn.execute(
            "SELECT * FROM channel_accounts WHERE channel = %s AND address = %s AND provider = 'meta'", (channel, eid)
        ).fetchone()
        if a is None:
            out["unknown_accounts"] += 1
            continue
        try:
            check_on(conn, a)
        except ChannelOff:
            messaging.log_webhook(conn, a, "refused", "channel not switched on")
            out["refused"] += 1
            continue
        got = apply(conn, a, items)
        messaging.log_webhook(conn, a, "accepted", f"{got}")
        for k, v in got.items():
            out[k] += v
    return out


# ---- simulated traffic (the setup screen and tests) ----------------------------------------


def sim_payload(account: dict, sender: str, text: str, msg_id: str, name: str = "") -> dict:
    now = int(time.time() * 1000)
    if account["channel"] == "telegram":
        first, _, last = (name or "Test").partition(" ")
        chat = int(sender) if sender.lstrip("-").isdigit() else sender
        mid = int(msg_id) if msg_id.isdigit() else zlib.crc32(msg_id.encode()) % 10**9
        return {
            "update_id": mid,
            "message": {
                "message_id": mid,
                "from": {"id": chat, "is_bot": False, "first_name": first, "last_name": last},
                "chat": {"id": chat, "type": "private"},
                "date": now // 1000,
                "text": text,
            },
        }
    return {
        "object": "instagram" if account["channel"] == "instagram" else "page",
        "entry": [
            {
                "id": account["address"],
                "time": now,
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": account["address"]},
                        "timestamp": now,
                        "message": {"mid": msg_id, "text": text},
                    }
                ],
            }
        ],
    }


def sim_receipt_payload(account: dict, recipient: str, status: str, mid: str, watermark_ms: int | None = None) -> dict:
    ev: dict[str, Any] = {"sender": {"id": recipient}, "recipient": {"id": account["address"]}}
    if status == "delivered":
        ev["delivery"] = {"mids": [mid], "watermark": watermark_ms or int(time.time() * 1000)}
    elif account["channel"] == "instagram":
        ev["read"] = {"mid": mid}
    else:
        ev["read"] = {"watermark": watermark_ms or int(time.time() * 1000)}
    return {
        "object": "instagram" if account["channel"] == "instagram" else "page",
        "entry": [{"id": account["address"], "time": int(time.time() * 1000), "messaging": [ev]}],
    }


def sim_headers(account: dict, raw: bytes) -> dict[str, str]:
    if account["channel"] == "telegram":
        return {"x-telegram-bot-api-secret-token": account["hook_secret"]}
    return {"x-hub-signature-256": meta_sign(account["hook_secret"], raw)}


# ---- channels ----------------------------------------------------------------------------


def _ago(d: dt.timedelta) -> str:
    h = d.total_seconds() / 3600
    return f"{h:.0f} hours" if h < 48 else f"{h / 24:.0f} days"


class SocialChannel(messaging.ProvidedChannel):
    def check_send(self, conn: psycopg.Connection, conversation: dict, body: str, template: str) -> None:
        if not golive.enabled(conn, "channel", self.name, conversation["customer_id"]):
            raise channels.SendBlocked(
                f"{self.label} is not switched on for this business yet. ExaCarib switches it on once its "
                "go-live checks pass."
            )
        super().check_send(conn, conversation, body, template)

    def tag(self, conversation: dict, template: str) -> str:
        """The message tag to send with, or "" for a standard reply."""
        return ""

    def deliver(self, conn: psycopg.Connection, conversation: dict, message: dict) -> dict:
        self.check_send(conn, conversation, message["body"], message["template"])
        tag = self.tag(conversation, message["template"])
        if tag and message["author_kind"] != "user":
            raise channels.SendBlocked(
                f"Only a person can send a tagged {self.label} message outside the 24-hour window, not the AI."
            )
        acct = self.account(conn, conversation)
        ident = messaging.identity_of(conn, conversation)
        prov = PROVIDERS.get(acct["provider"])
        if prov is None:
            raise channels.SendBlocked(f"The {self.label} account has an unknown provider.")
        try:
            ref = prov.send_message(conn, acct, ident["address"], message["body"], tag=tag, ref=str(message["id"]))
        except Blocked as e:
            messaging.set_opt_out(conn, ident, True, str(e))
            raise channels.SendBlocked(f"{e} They are opted out.") from e
        except Exception as e:
            conn.execute(
                "UPDATE channel_accounts SET last_error = %s, last_error_at = now() WHERE id = %s",
                (f"{type(e).__name__}: {e}"[:500], acct["id"]),
            )
            raise
        conn.execute("UPDATE channel_accounts SET last_sent_at = now() WHERE id = %s", (acct["id"],))
        messaging.remember_thread(conn, conversation["id"], conversation["customer_id"], acct["id"])
        usage.record(conn, conversation["customer_id"], f"message_out:{self.name}", 1, ref=str(message["id"]))
        return {"status": "sent", "provider_ref": ref}


def parse_tag(channel: str, template: str) -> str:
    t = (template or "").strip()
    if not t:
        return ""
    if not t.lower().startswith("tag:"):
        raise channels.SendBlocked(
            f"{LABELS[channel]} has no message templates. Outside the 24-hour window a person can reply with a "
            "message tag, written as tag:HUMAN_AGENT."
        )
    tag = t[4:].strip().upper()
    if tag not in TAGS[channel]:
        raise channels.SendBlocked(f"{LABELS[channel]} accepts only these tags: {', '.join(sorted(TAGS[channel]))}.")
    return tag


class MetaChannel(SocialChannel):
    """Messenger or Instagram: Meta's 24-hour window and message tags."""

    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        tag = parse_tag(self.name, template)
        last = conversation.get("last_inbound_at")
        if last is None:
            raise channels.SendBlocked(f"On {self.label} the person must write first.")
        age = dt.datetime.now(dt.UTC) - last
        if age <= messaging.WINDOW:
            return
        if not tag:
            more = (
                " A person can reply with tag:HUMAN_AGENT within 7 days of their message."
                if age <= HUMAN_AGENT_WINDOW
                else ""
            )
            raise channels.SendBlocked(
                f"Outside {self.label}'s 24-hour messaging window: the person last wrote {_ago(age)} ago.{more}"
            )
        limit = TAGS[self.name][tag]
        if limit is not None and age > limit:
            raise channels.SendBlocked(
                f"The {tag} tag can only be used within {limit.days} days of the person's last message; "
                f"they last wrote {_ago(age)} ago."
            )

    def tag(self, conversation: dict, template: str) -> str:
        tag = parse_tag(self.name, template)
        last = conversation.get("last_inbound_at")
        if tag and last is not None and dt.datetime.now(dt.UTC) - last <= messaging.WINDOW:
            return ""  # inside the window a standard reply needs no tag
        return tag


class TelegramChannel(SocialChannel):
    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        if template:
            raise channels.SendBlocked("Telegram has no message templates or tags. Write the message as text.")


MESSENGER = MetaChannel("messenger", "Messenger")
INSTAGRAM = MetaChannel("instagram", "Instagram")
TELEGRAM = TelegramChannel("telegram", "Telegram")
for _ch in (MESSENGER, INSTAGRAM, TELEGRAM):
    channels.register(_ch)


# ---- diagnostics ------------------------------------------------------------------------


def _check(channel: str) -> None:
    from .. import diagnostics
    from . import checks

    @diagnostics.register(channel)
    def run(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
        label = LABELS[channel]
        if not golive.enabled(conn, "channel", channel, customer_id):
            return [
                checks._finding(
                    channel,
                    "ok",
                    f"{label} is not switched on for this business yet, so nothing is sent or received on it.",
                    ["ExaCarib switches a channel on once its go-live checks pass."],
                )
            ]
        return checks._account_findings(conn, customer_id, channel, label) + checks._recent_findings(
            conn, customer_id, channel, label
        )


for _name in LABELS:
    _check(_name)
