"""The channel gateway for WhatsApp, SMS and email accounts (ADR 0018).

Inbound: a provider webhook arrives at /api/v1/commai/channels/hooks/{hook_token},
its signature is checked, and each message goes through inbox.receive with a
namespaced external id, so a repeated webhook never stores a message twice.
Receipts update each message through inbox.update_status.

Outbound: the inbox calls Channel.check_send before storing a reply (the rules
are enforced here for people, the AI and the API alike) and Channel.deliver
from the durable send job. deliver checks the rules again, because a queued
message may wait.

Rules:
- WhatsApp: free-form text only inside the 24-hour service window that starts
  with the customer's last message (conversations.last_inbound_at). Outside it,
  only an approved template, with exactly the template's text.
- SMS: opt-out words (STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT; START or
  UNSTOP opts back in) and a daily sending limit per destination country.
- Every provided channel: the account must be live, and an opted-out identity
  is never sent to.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import psycopg

from .. import channels, events, inbox, usage
from . import providers
from .providers import Inbound, InboundRequest, Receipt

WINDOW = dt.timedelta(hours=24)
OPT_OUT = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
OPT_IN = {"START", "UNSTOP"}
DEFAULT_DAILY_LIMIT = 1000  # SMS per destination country per day, per business

events.register("contact.opted_out", "contact.opted_in", "channel.webhook_rejected")

# Country from the E.164 prefix. NANP (+1) numbers are told apart by area code,
# since most of the Caribbean shares +1.
NANP = {
    "242": "BS", "246": "BB", "264": "AI", "268": "AG", "284": "VG", "340": "VI", "345": "KY",
    "441": "BM", "473": "GD", "649": "TC", "658": "JM", "664": "MS", "670": "MP", "671": "GU",
    "684": "AS", "721": "SX", "758": "LC", "767": "DM", "784": "VC", "787": "PR", "809": "DO",
    "829": "DO", "849": "DO", "868": "TT", "869": "KN", "876": "JM", "939": "PR",
}  # fmt: skip
PREFIXES = {
    "297": "AW", "299": "GL", "501": "BZ", "502": "GT", "503": "SV", "504": "HN", "505": "NI",
    "506": "CR", "507": "PA", "509": "HT", "590": "GP", "592": "GY", "594": "GF", "596": "MQ",
    "597": "SR", "598": "UY", "599": "CW", "20": "EG", "27": "ZA", "30": "GR", "31": "NL",
    "32": "BE", "33": "FR", "34": "ES", "39": "IT", "41": "CH", "44": "GB", "45": "DK", "46": "SE",
    "47": "NO", "49": "DE", "51": "PE", "52": "MX", "53": "CU", "54": "AR", "55": "BR", "56": "CL",
    "57": "CO", "58": "VE", "61": "AU", "64": "NZ", "81": "JP", "86": "CN", "91": "IN", "234": "NG",
    "353": "IE", "351": "PT", "7": "RU",
}  # fmt: skip


def country_of(number: str) -> str:
    """ISO country code for an E.164 number ('TT' for +1868...), or '' when unknown."""
    digits = providers.e164(number).lstrip("+")
    if digits.startswith("1"):
        return NANP.get(digits[1:4], "US/CA")
    for n in (3, 2, 1):
        if digits[:n] in PREFIXES:
            return PREFIXES[digits[:n]]
    return ""


# ---- accounts -----------------------------------------------------------------------


def new_hook_token() -> str:
    return "ch_" + secrets.token_urlsafe(24)


def account_for(conn: psycopg.Connection, conv: dict) -> dict | None:
    """The account a conversation's replies leave from: the one it came in on,
    else the business's live (then any) account for that channel."""
    row = conn.execute(
        """SELECT a.* FROM channel_threads t JOIN channel_accounts a ON a.id = t.account_id
           WHERE t.conversation_id = %s""",
        (conv["id"],),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """SELECT * FROM channel_accounts WHERE customer_id = %s AND channel = %s
           ORDER BY (status = 'live') DESC, created_at LIMIT 1""",
        (conv["customer_id"], conv["channel"]),
    ).fetchone()


def remember_thread(conn: psycopg.Connection, conv_id: Any, customer_id: Any, account_id: Any) -> None:
    conn.execute(
        """INSERT INTO channel_threads (conversation_id, customer_id, account_id) VALUES (%s, %s, %s)
           ON CONFLICT (conversation_id) DO UPDATE SET account_id = EXCLUDED.account_id, updated_at = now()""",
        (conv_id, customer_id, account_id),
    )


def identity_of(conn: psycopg.Connection, conv: dict) -> dict:
    row = conn.execute("SELECT * FROM contact_identities WHERE id = %s", (conv["identity_id"],)).fetchone()
    if row is None:
        raise channels.SendBlocked("This conversation has no address to reply to.")
    return row


def log_webhook(conn: psycopg.Connection, account: dict, outcome: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO channel_webhook_log (customer_id, account_id, outcome, detail) VALUES (%s, %s, %s, %s)",
        (account["customer_id"], account["id"], outcome, detail[:500]),
    )


def set_opt_out(conn: psycopg.Connection, identity: dict, opted_out: bool, why: str) -> None:
    if bool(identity["opted_out"]) == opted_out:
        return
    conn.execute("UPDATE contact_identities SET opted_out = %s WHERE id = %s", (opted_out, identity["id"]))
    events.emit(
        conn,
        identity["customer_id"],
        "contact.opted_out" if opted_out else "contact.opted_in",
        {"contact_id": str(identity["contact_id"]), "channel": identity["channel"], "reason": why},
        identity["contact_id"],
    )


# ---- templates ------------------------------------------------------------------------


def find_template(conn: psycopg.Connection, customer_id: Any, ref: str) -> dict | None:
    """A template by "name" or "name:language"."""
    name, _, lang = ref.partition(":")
    return conn.execute(
        """SELECT * FROM whatsapp_templates WHERE customer_id = %s AND name = %s AND (%s = '' OR language = %s)
           ORDER BY (status = 'approved') DESC, language LIMIT 1""",
        (customer_id, name, lang, lang),
    ).fetchone()


def template_params(conn: psycopg.Connection, msg: dict, tpl: dict) -> list[str]:
    row = conn.execute("SELECT params FROM template_sends WHERE message_id = %s", (msg["id"],)).fetchone()
    if row:
        return list(row["params"])
    if not msg["body"]:
        return []
    return providers.match_params(tpl["body"], msg["body"]) or []


# ---- channels ---------------------------------------------------------------------------


class ProvidedChannel(channels.Channel):
    """A channel whose replies go out through a provider account."""

    external = True

    def __init__(self, name: str, label: str):
        self.name = name
        self.label = label

    def account(self, conn: psycopg.Connection, conv: dict) -> dict:
        acct = account_for(conn, conv)
        if acct is None:
            raise channels.SendBlocked(f"No {self.label} account is set up for this business.")
        if acct["status"] == "paused":
            raise channels.SendBlocked(f"The {self.label} account {acct['address']} is paused.")
        if acct["status"] == "setup":
            raise channels.SendBlocked(f"The {self.label} account {acct['address']} is still being set up.")
        return acct

    def check_send(self, conn: psycopg.Connection, conversation: dict, body: str, template: str) -> None:
        self.account(conn, conversation)
        ident = identity_of(conn, conversation)
        if ident["opted_out"]:
            raise channels.SendBlocked(f"{ident['address']} has opted out of {self.label} messages.")
        check_spend(conn, conversation["customer_id"], self.name, self.label)
        self.rules(conn, conversation, ident, body, template)

    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        """Channel-specific rules. Raise SendBlocked."""

    def deliver(self, conn: psycopg.Connection, conversation: dict, message: dict) -> dict:
        # The rules still hold when the queued message is sent.
        self.check_send(conn, conversation, message["body"], message["template"])
        acct = self.account(conn, conversation)
        ident = identity_of(conn, conversation)
        prov = providers.get(acct["provider"])
        try:
            if message["template"]:
                tpl = find_template(conn, conversation["customer_id"], message["template"])
                ref = prov.send_template(
                    acct, ident["address"], tpl, template_params(conn, message, tpl), ref=str(message["id"]), conn=conn
                )
            elif message.get("attachments"):
                files = [a for a in message["attachments"] if isinstance(a, dict) and a.get("id")]
                ref = prov.send_media(acct, ident["address"], message["body"], files, ref=str(message["id"]), conn=conn)
            else:
                ref = prov.send_text(acct, ident["address"], message["body"], ref=str(message["id"]), conn=conn)
        except Exception as e:
            conn.execute(
                "UPDATE channel_accounts SET last_error = %s, last_error_at = now() WHERE id = %s",
                (f"{type(e).__name__}: {e}"[:500], acct["id"]),
            )
            raise
        conn.execute("UPDATE channel_accounts SET last_sent_at = now() WHERE id = %s", (acct["id"],))
        remember_thread(conn, conversation["id"], conversation["customer_id"], acct["id"])
        usage.record(conn, conversation["customer_id"], f"message_out:{self.name}", 1, ref=str(message["id"]))
        return {"status": "sent", "provider_ref": ref}


def check_spend(conn: psycopg.Connection, customer_id: Any, channel: str, label: str) -> None:
    """The business's hard monthly limit for this channel's messages (usage.allowed)."""
    if not usage.allowed(conn, customer_id, f"message_out:{channel}"):
        raise channels.SendBlocked(f"This month's {label} limit is used up. An administrator can raise it under Usage.")


class WhatsApp(ProvidedChannel):
    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        if template:
            tpl = find_template(conn, conversation["customer_id"], template)
            if tpl is None or tpl["status"] != "approved":
                state = "is not approved" if tpl else "does not exist"
                raise channels.SendBlocked(f"The WhatsApp template {template} {state}. Only approved templates go out.")
            if body:
                if providers.match_params(tpl["body"], body) is None:
                    raise channels.SendBlocked(
                        f"The text must be the approved template {tpl['name']} with its values filled in."
                    )
            elif providers.placeholders(tpl["body"]):
                raise channels.SendBlocked(f"Fill in the values for the template {tpl['name']}.")
            return
        last = conversation.get("last_inbound_at")
        now = dt.datetime.now(dt.UTC)
        if last is None or now - last > WINDOW:
            since = "has never written" if last is None else f"last wrote {_ago(now - last)} ago"
            raise channels.SendBlocked(
                f"Outside WhatsApp's 24-hour service window: the customer {since}. "
                "Only an approved template can be sent until they write again."
            )


class Sms(ProvidedChannel):
    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        country = country_of(identity["address"])
        acct = self.account(conn, conversation)
        limits = (acct.get("settings") or {}).get("daily_limits") or {}
        limit = int(limits.get(country, limits.get("*", DEFAULT_DAILY_LIMIT)))
        sent = sms_sent_today(conn, conversation["customer_id"], country)
        if sent >= limit:
            raise channels.SendBlocked(
                f"Today's SMS limit for {country or 'this country'} ({limit}) is used up. It resets at midnight UTC."
            )


def sms_sent_today(conn: psycopg.Connection, customer_id: Any, country: str) -> int:
    rows = conn.execute(
        """SELECT ci.address, count(*) AS n FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           JOIN contact_identities ci ON ci.id = c.identity_id
           WHERE m.customer_id = %s AND c.channel = 'sms' AND m.direction = 'out'
             AND m.status NOT IN ('blocked', 'failed') AND m.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
             AT TIME ZONE 'UTC'
           GROUP BY ci.address""",
        (customer_id,),
    ).fetchall()
    return sum(r["n"] for r in rows if country_of(r["address"]) == country)


def _ago(d: dt.timedelta) -> str:
    h = d.total_seconds() / 3600
    return f"{h:.0f} hours" if h < 48 else f"{h / 24:.0f} days"


WHATSAPP = WhatsApp("whatsapp", "WhatsApp")
SMS = Sms("sms", "SMS")
channels.register(WHATSAPP)
channels.register(SMS)


# ---- inbound ------------------------------------------------------------------------


def handle_provider_webhook(conn: psycopg.Connection, account: dict, req: InboundRequest) -> dict:
    """Verify, parse and store one provider webhook. Returns counts."""
    prov = providers.get(account["provider"])
    if not prov.verify(account, req):
        log_webhook(conn, account, "rejected", "signature did not verify")
        events.emit(
            conn,
            account["customer_id"],
            "channel.webhook_rejected",
            {"account_id": str(account["id"]), "channel": account["channel"], "reason": "bad signature"},
            account["id"],
        )
        raise PermissionError("The webhook signature did not verify.")
    out = {"received": 0, "duplicates": 0, "receipts": 0}
    for item in prov.parse(account, req):
        if isinstance(item, Inbound):
            if not item.address or not item.external_id:
                continue
            got = receive_one(conn, account, item)
            out["duplicates" if got["duplicate"] else "received"] += 1
        elif isinstance(item, Receipt) and item.provider_ref:
            # Only this business's own messages: a webhook can't touch another tenant's.
            mine = conn.execute(
                "SELECT 1 FROM messages WHERE provider_ref = %s AND customer_id = %s",
                (item.provider_ref, account["customer_id"]),
            ).fetchone()
            if mine:
                inbox.update_status(conn, item.provider_ref, item.status, item.error)
                out["receipts"] += 1
                if item.status == "failed":
                    conn.execute(
                        "UPDATE channel_accounts SET last_error = %s, last_error_at = now() WHERE id = %s",
                        (f"Delivery failed: {item.error or 'no reason given'}"[:500], account["id"]),
                    )
    log_webhook(conn, account, "accepted", f"{out}")
    return out


def receive_one(conn: psycopg.Connection, account: dict, item: Inbound) -> dict:
    """One inbound message on a provided channel, stored at most once."""
    ext = f"{account['channel']}:{account['provider']}:{item.external_id}"
    got = inbox.receive(
        conn,
        account["customer_id"],
        account["channel"],
        item.address,
        item.body,
        external_id=ext,
        name=item.name,
        attachments=item.attachments,
    )
    if got["duplicate"]:
        return got
    conv = got["conversation"]
    remember_thread(conn, conv["id"], account["customer_id"], account["id"])
    conn.execute("UPDATE channel_accounts SET last_inbound_at = now() WHERE id = %s", (account["id"],))
    word = item.body.strip().upper().rstrip(".!")
    if word in OPT_OUT or word in OPT_IN:
        ident = conn.execute("SELECT * FROM contact_identities WHERE id = %s", (conv["identity_id"],)).fetchone()
        set_opt_out(conn, ident, word in OPT_OUT, f"replied {word}")
    return got
