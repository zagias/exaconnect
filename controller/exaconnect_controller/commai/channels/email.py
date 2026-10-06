"""Email (ADR 0018): inbound by webhook, outbound by SMTP behind an interface.

Inbound arrives at the account's hook URL with its shared secret (header
X-Exa-Email-Secret, or ?secret= for providers that can't set headers), in one
of two formats:

- generic JSON: {"from", "from_name", "to", "subject", "text", "message_id",
  "in_reply_to", "references"}
- Mailgun's form format (the "forward" route action): sender, from, subject,
  body-plain, stripped-text, Message-Id, In-Reply-To, References.

Threading: every Message-ID we send or receive is stored with its
conversation; a reply whose In-Reply-To or References names one of them joins
that conversation. Unsubscribing (a List-Unsubscribe link, or an email whose
subject or first line is "unsubscribe") opts the address out: after that, only
a reply to something the person wrote in the last 24 hours goes out.

Outbound uses SMTP when EXA_SMTP_HOST is set (EXA_SMTP_PORT, EXA_SMTP_USER,
EXA_SMTP_PASSWORD, EXA_SMTP_STARTTLS); otherwise a simulated sender records
the email in sim_channel_outbox. Accounts say which they use.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import channels, inbox, usage
from . import messaging

UNSUB_WINDOW = dt.timedelta(hours=24)
MSGID = re.compile(r"<[^<>\s]+>")


class Sender:
    name = ""
    simulated = False

    def send(self, conn: psycopg.Connection, account: dict, msg: EmailMessage) -> None:
        raise NotImplementedError


class SimulatedSender(Sender):
    name = "simulated"
    simulated = True

    def send(self, conn, account, msg) -> None:
        headers = {k: str(v) for k, v in msg.items()}
        conn.execute(
            """INSERT INTO sim_channel_outbox (customer_id, account_id, channel, to_address, body, headers,
                                                 provider_ref) VALUES (%s, %s, 'email', %s, %s, %s, %s)""",
            (account["customer_id"], account["id"], msg["To"], msg.get_content(), Jsonb(headers), msg["Message-ID"]),
        )


class SmtpSender(Sender):
    """Plain SMTP with STARTTLS. NOT live until EXA_SMTP_HOST and its login are set."""

    name = "smtp"

    @staticmethod
    def missing() -> list[str]:
        return [n for n in ("EXA_SMTP_HOST",) if not os.environ.get(n)]

    def send(self, conn, account, msg) -> None:  # pragma: no cover - network
        host = os.environ.get("EXA_SMTP_HOST", "")
        if not host:
            raise RuntimeError("SMTP is not configured: set EXA_SMTP_HOST.")
        port = int(os.environ.get("EXA_SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=15) as s:
            if os.environ.get("EXA_SMTP_STARTTLS", "true").lower() != "false":
                s.starttls()
            user = os.environ.get("EXA_SMTP_USER", "")
            if user:
                s.login(user, os.environ.get("EXA_SMTP_PASSWORD", ""))
            s.send_message(msg)


SENDERS: dict[str, Sender] = {"simulated": SimulatedSender(), "smtp": SmtpSender()}


def _domain(address: str) -> str:
    return address.rpartition("@")[2] or "exacarib.invalid"


def message_id_for(account: dict, chat_message_id: Any) -> str:
    """Deterministic, so a retried send carries the same Message-ID."""
    return f"<{chat_message_id}@{_domain(account['address'])}>"


def unsubscribe_token(account: dict, identity_id: Any) -> str:
    raw = str(identity_id)
    sig = hmac.new(account["hook_secret"].encode(), f"unsub:{raw}".encode(), hashlib.sha256).digest()[:16]
    return (
        base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(sig).decode().rstrip("=")
    )


def check_unsubscribe(conn: psycopg.Connection, hook_token: str, token: str) -> dict | None:
    acct = conn.execute("SELECT * FROM channel_accounts WHERE hook_token = %s", (hook_token,)).fetchone()
    if acct is None or "." not in token:
        return None
    raw_b64 = token.split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4)).decode()
    except ValueError:
        return None
    if not hmac.compare_digest(unsubscribe_token(acct, raw), token):
        return None
    ident = conn.execute(
        "SELECT * FROM contact_identities WHERE id = %s AND customer_id = %s", (raw, acct["customer_id"])
    ).fetchone()
    if ident:
        messaging.set_opt_out(conn, ident, True, "unsubscribe link")
    return ident


class Email(messaging.ProvidedChannel):
    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        pass

    def check_send(self, conn: psycopg.Connection, conversation: dict, body: str, template: str) -> None:
        self.account(conn, conversation)
        ident = messaging.identity_of(conn, conversation)
        if ident["opted_out"]:
            last = conversation.get("last_inbound_at")
            if last is None or dt.datetime.now(dt.UTC) - last > UNSUB_WINDOW:
                raise channels.SendBlocked(
                    f"{ident['address']} has unsubscribed. You can only reply within 24 hours of an email from them."
                )

    def deliver(self, conn: psycopg.Connection, conversation: dict, message: dict) -> dict:
        self.check_send(conn, conversation, message["body"], message["template"])
        acct = self.account(conn, conversation)
        ident = messaging.identity_of(conn, conversation)
        sender = SENDERS.get(acct["provider"]) or SENDERS["simulated"]
        mid = message_id_for(acct, message["id"])
        refs = [
            r["message_id"]
            for r in conn.execute(
                """SELECT message_id FROM email_message_ids WHERE conversation_id = %s AND customer_id = %s
                   ORDER BY chat_message_id IS NULL, message_id LIMIT 20""",
                (conversation["id"], conversation["customer_id"]),
            ).fetchall()
        ]
        last_in = conn.execute(
            """SELECT e.message_id FROM email_message_ids e JOIN messages m ON m.id = e.chat_message_id
               WHERE e.conversation_id = %s AND m.direction = 'in' ORDER BY m.created_at DESC LIMIT 1""",
            (conversation["id"],),
        ).fetchone()
        subject = conversation["subject"] or "Your enquiry"
        msg = EmailMessage()
        msg["From"] = formataddr(((acct.get("settings") or {}).get("from_name", acct["name"]) or "", acct["address"]))
        msg["To"] = ident["address"]
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Message-ID"] = mid
        if last_in:
            msg["In-Reply-To"] = last_in["message_id"]
        if refs:
            msg["References"] = " ".join(refs)
        base = os.environ.get("EXA_PUBLIC_URL", "").rstrip("/")
        unsub = [f"<mailto:{acct['address']}?subject=unsubscribe>"]
        if base:
            unsub.insert(
                0,
                f"<{base}/api/v1/commai/channels/email/{acct['hook_token']}/unsubscribe/"
                f"{unsubscribe_token(acct, ident['id'])}>",
            )
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg["List-Unsubscribe"] = ", ".join(unsub)
        msg.set_content(message["body"])
        try:
            sender.send(conn, acct, msg)
        except Exception as e:
            conn.execute(
                "UPDATE channel_accounts SET last_error = %s, last_error_at = now() WHERE id = %s",
                (f"{type(e).__name__}: {e}"[:500], acct["id"]),
            )
            raise
        conn.execute(
            """INSERT INTO email_message_ids (customer_id, message_id, conversation_id, chat_message_id)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (conversation["customer_id"], mid, conversation["id"], message["id"]),
        )
        conn.execute("UPDATE channel_accounts SET last_sent_at = now() WHERE id = %s", (acct["id"],))
        messaging.remember_thread(conn, conversation["id"], conversation["customer_id"], acct["id"])
        usage.record(conn, conversation["customer_id"], "message_out:email", 1, ref=str(message["id"]))
        return {"status": "sent", "provider_ref": mid}


EMAIL = Email("email", "Email")
channels.register(EMAIL)


# ---- inbound -----------------------------------------------------------------------------


def parse_inbound(content_type: str, body: bytes, form: dict[str, str]) -> dict:
    """One inbound email as {"from", "name", "subject", "text", "message_id", "in_reply_to", "references"}."""
    if "json" in content_type:
        d = json.loads(body or b"{}")
        name, addr = parseaddr(str(d.get("from", "")))
        return {
            "from": addr,
            "name": str(d.get("from_name") or name),
            "subject": str(d.get("subject", "")),
            "text": str(d.get("text", "")),
            "message_id": str(d.get("message_id", "")),
            "in_reply_to": str(d.get("in_reply_to", "")),
            "references": str(d.get("references", "")),
        }
    name, addr = parseaddr(form.get("from") or form.get("sender", ""))
    return {
        "from": addr or form.get("sender", ""),
        "name": name,
        "subject": form.get("subject", ""),
        "text": form.get("stripped-text") or form.get("body-plain", ""),
        "message_id": form.get("Message-Id", ""),
        "in_reply_to": form.get("In-Reply-To", ""),
        "references": form.get("References", ""),
    }


def verify_secret(account: dict, given: str) -> bool:
    return bool(given) and hmac.compare_digest(given, account["hook_secret"])


def receive_email(conn: psycopg.Connection, account: dict, mail: dict) -> dict:
    """Store one inbound email, threaded into its conversation. Stored once per Message-ID."""
    addr = mail["from"].strip().lower()
    if not addr or "@" not in addr:
        raise ValueError("The email has no sender address.")
    cid = account["customer_id"]
    msgid = (MSGID.findall(mail["message_id"]) or [""])[0] or f"<{secrets.token_hex(16)}@unknown>"
    wanted = MSGID.findall(mail["in_reply_to"]) + MSGID.findall(mail["references"])[::-1]
    conv_id = None
    if wanted:
        row = conn.execute(
            """SELECT e.conversation_id FROM email_message_ids e JOIN conversations c ON c.id = e.conversation_id
               JOIN contact_identities ci ON ci.id = c.identity_id
               WHERE e.customer_id = %s AND e.message_id = ANY(%s) AND ci.address = %s
               ORDER BY c.created_at DESC LIMIT 1""",
            (cid, wanted, addr),
        ).fetchone()
        conv_id = row["conversation_id"] if row else None
    got = inbox.receive(
        conn,
        cid,
        "email",
        addr,
        mail["text"].strip() or "(no text)",
        external_id=f"email:{msgid}",
        name=mail["name"],
        subject=re.sub(r"^(re|fwd?):\s*", "", mail["subject"], flags=re.I)[:200],
        conversation_id=conv_id,
    )
    if got["duplicate"]:
        return got
    conv = got["conversation"]
    conn.execute(
        """INSERT INTO email_message_ids (customer_id, message_id, conversation_id, chat_message_id)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (cid, msgid, conv["id"], got["message"]["id"]),
    )
    messaging.remember_thread(conn, conv["id"], cid, account["id"])
    conn.execute("UPDATE channel_accounts SET last_inbound_at = now() WHERE id = %s", (account["id"],))
    first_line = (mail["text"].strip().splitlines() or [""])[0].strip().lower().rstrip(".!")
    if mail["subject"].strip().lower() == "unsubscribe" or first_line == "unsubscribe":
        ident = conn.execute("SELECT * FROM contact_identities WHERE id = %s", (conv["identity_id"],)).fetchone()
        messaging.set_opt_out(conn, ident, True, "unsubscribe email")
    return got
