"""Messaging providers for WhatsApp and SMS (ADR 0018).

CommAI reaches WhatsApp through the official WhatsApp Business Platform via an
approved provider, and SMS through the same provider. Each provider adapter
does five things behind one interface: send text, send a template, verify an
inbound webhook's signature, parse inbound messages, and map delivery and
read receipts to CommAI's statuses.

- `simulated` works end to end with no account. It is the default, and every
  screen that uses it says "Simulated".
- `twilio` (WhatsApp and SMS) and `360dialog` (WhatsApp) are written from the
  providers' public API documentation. They are NOT live until Dudley picks the
  provider, creates the account and sets its credentials in the environment.
  They have not been run against the real services.

Credentials are read only from environment variables, never from the database
or the API. An account may name a different variable prefix (for a second
Twilio account, say), but only one that starts with the provider's own prefix,
so an account can never point an adapter at another service's secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

TIMEOUT_S = 10


class ProviderError(Exception):
    """The provider refused or failed. The message is safe to show to staff."""


@dataclass
class InboundRequest:
    url: str  # the public URL the provider called (for Twilio's signature)
    headers: dict[str, str]  # lower-case names
    body: bytes
    form: dict[str, str] = field(default_factory=dict)  # parsed form fields, when form-encoded


@dataclass
class Inbound:
    address: str  # E.164 with "+"
    body: str
    external_id: str
    name: str = ""
    attachments: list[dict] = field(default_factory=list)


@dataclass
class Receipt:
    provider_ref: str
    status: str  # sent | delivered | read | failed
    error: str = ""


def _prefix_env(account: dict, default: str) -> str:
    prefix = str((account.get("settings") or {}).get("credentials_env") or default)
    if not re.fullmatch(r"[A-Z0-9_]+", prefix) or not prefix.startswith(default):
        raise ProviderError(f"credentials_env must start with {default}.")
    return prefix


def e164(raw: str) -> str:
    """'+18685550101' from 'whatsapp:+1 868-555-0101', '18685550101' and the like."""
    raw = (raw or "").strip()
    if raw.lower().startswith(("whatsapp:", "sms:")):
        raw = raw.split(":", 1)[1]
    digits = re.sub(r"\D", "", raw)
    return f"+{digits}" if digits else ""


def render(template_body: str, params: list[str]) -> str:
    """Fill {{1}}, {{2}}... with the values given."""

    def sub(m: re.Match) -> str:
        i = int(m.group(1)) - 1
        return params[i] if 0 <= i < len(params) else m.group(0)

    return re.sub(r"\{\{(\d+)\}\}", sub, template_body)


def placeholders(template_body: str) -> int:
    nums = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", template_body)]
    return max(nums) if nums else 0


def match_params(template_body: str, text: str) -> list[str] | None:
    """The values that turn the template into `text`, or None if it isn't that template."""
    parts = re.split(r"(\{\{\d+\}\})", template_body)
    order: list[int] = []
    pattern = ""
    for p in parts:
        m = re.fullmatch(r"\{\{(\d+)\}\}", p)
        if m:
            order.append(int(m.group(1)))
            pattern += "(.+?)"
        else:
            pattern += re.escape(p)
    m = re.fullmatch(pattern, text, re.DOTALL)
    if not m:
        return None
    values: dict[int, str] = {}
    for n, v in zip(order, m.groups(), strict=True):
        if n in values and values[n] != v:
            return None
        values[n] = v
    return [values.get(i, "") for i in range(1, max(order, default=0) + 1)]


class Provider:
    name = ""
    label = ""
    simulated = False
    channels: tuple[str, ...] = ()

    def missing(self, account: dict) -> list[str]:
        """Environment variables this account still needs (empty when configured)."""
        return []

    def send_text(self, account: dict, to: str, body: str, *, ref: str, conn: Any = None) -> str:
        """Send free-form text. Returns the provider's message id."""
        raise NotImplementedError

    def send_template(
        self, account: dict, to: str, template: dict, params: list[str], *, ref: str, conn: Any = None
    ) -> str:
        raise NotImplementedError

    def verify(self, account: dict, req: InboundRequest) -> bool:
        raise NotImplementedError

    def parse(self, account: dict, req: InboundRequest) -> list[Inbound | Receipt]:
        raise NotImplementedError

    def response(self) -> tuple[str, str]:
        """What to answer a webhook with: (content type, body)."""
        return "application/json", '{"ok": true}'


# ---- simulated ------------------------------------------------------------------


class Simulated(Provider):
    """Works end to end with no account: sends are recorded in sim_channel_outbox,
    inbound webhooks are JSON signed with the account's hook secret:

      X-Exa-Signature: sha256=<hex HMAC-SHA256(hook_secret, raw body)>
      {"messages": [{"id", "from", "name", "body"}], "statuses": [{"ref", "status", "error"}]}
    """

    name = "simulated"
    label = "Simulated"
    simulated = True
    channels = ("whatsapp", "sms")

    def _record(self, conn: Any, account: dict, to: str, body: str, template: str) -> str:
        if (account.get("settings") or {}).get("fail_sends"):
            raise ProviderError("Simulated failure: this account is set to fail sends.")
        ref = f"sim-{secrets.token_hex(12)}"
        if conn is not None:
            conn.execute(
                """INSERT INTO sim_channel_outbox (customer_id, account_id, channel, to_address, body, template,
                                                   provider_ref) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (account["customer_id"], account["id"], account["channel"], to, body, template, ref),
            )
        return ref

    def send_text(self, account: dict, to: str, body: str, *, ref: str, conn: Any = None) -> str:
        return self._record(conn, account, to, body, "")

    def send_template(
        self, account: dict, to: str, template: dict, params: list[str], *, ref: str, conn: Any = None
    ) -> str:
        return self._record(
            conn, account, to, render(template["body"], params), f"{template['name']}:{template['language']}"
        )

    @staticmethod
    def sign(secret: str, body: bytes) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def verify(self, account: dict, req: InboundRequest) -> bool:
        got = req.headers.get("x-exa-signature", "")
        return bool(got) and hmac.compare_digest(got, self.sign(account["hook_secret"], req.body))

    def parse(self, account: dict, req: InboundRequest) -> list[Inbound | Receipt]:
        data = json.loads(req.body or b"{}")
        out: list[Inbound | Receipt] = []
        for m in data.get("messages") or []:
            out.append(
                Inbound(
                    address=e164(str(m.get("from", ""))),
                    body=str(m.get("body", "")),
                    external_id=str(m.get("id", "")),
                    name=str(m.get("name", "")),
                )
            )
        for s in data.get("statuses") or []:
            out.append(Receipt(str(s.get("ref", "")), str(s.get("status", "")), str(s.get("error", ""))))
        return out


# ---- Twilio -----------------------------------------------------------------------


TWILIO_STATUS = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
}


class Twilio(Provider):
    """Twilio Programmable Messaging, for WhatsApp and SMS. NOT live until a
    Twilio account exists and EXA_TWILIO_ACCOUNT_SID / EXA_TWILIO_AUTH_TOKEN are set.

    Send:    POST https://api.twilio.com/2010-04-01/Accounts/{Sid}/Messages.json
             (form: From, To, Body | ContentSid + ContentVariables), basic auth.
             WhatsApp numbers are written "whatsapp:+1868...".
    Inbound: form-encoded POST (MessageSid, From, To, Body, ProfileName, NumMedia...)
             signed with X-Twilio-Signature = base64(HMAC-SHA1(auth token,
             full URL + each POST field name and value, sorted by name)).
    Receipts: the same webhook with MessageStatus (sent, delivered, read,
             failed, undelivered) and ErrorCode.
    """

    name = "twilio"
    label = "Twilio"
    channels = ("whatsapp", "sms")
    api_base = "https://api.twilio.com/2010-04-01"

    def _creds(self, account: dict) -> tuple[str, str]:
        p = _prefix_env(account, "EXA_TWILIO")
        return os.environ.get(f"{p}_ACCOUNT_SID", ""), os.environ.get(f"{p}_AUTH_TOKEN", "")

    def missing(self, account: dict) -> list[str]:
        p = _prefix_env(account, "EXA_TWILIO")
        sid, token = self._creds(account)
        return [n for n, v in ((f"{p}_ACCOUNT_SID", sid), (f"{p}_AUTH_TOKEN", token)) if not v]

    def _addr(self, account: dict, number: str) -> str:
        return f"whatsapp:{number}" if account["channel"] == "whatsapp" else number

    def _post(self, url: str, fields: dict, sid: str, token: str) -> dict:  # pragma: no cover - network
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("message", "")
            except ValueError:
                detail = ""
            raise ProviderError(f"Twilio answered {e.code}: {detail}"[:300]) from e
        except OSError as e:
            raise ProviderError(f"Twilio could not be reached: {type(e).__name__}") from e

    def _send(self, account: dict, fields: dict) -> str:
        sid, token = self._creds(account)
        if not sid or not token:
            raise ProviderError("Twilio is not configured: set " + ", ".join(self.missing(account)) + ".")
        status_url = (account.get("settings") or {}).get("status_callback_url")
        if status_url:
            fields["StatusCallback"] = status_url
        out = self._post(f"{self.api_base}/Accounts/{sid}/Messages.json", fields, sid, token)
        if not out.get("sid"):
            raise ProviderError("Twilio did not return a message id.")
        return str(out["sid"])

    def send_text(self, account: dict, to: str, body: str, *, ref: str, conn: Any = None) -> str:
        return self._send(
            account, {"From": self._addr(account, account["address"]), "To": self._addr(account, to), "Body": body}
        )

    def send_template(
        self, account: dict, to: str, template: dict, params: list[str], *, ref: str, conn: Any = None
    ) -> str:
        if not template.get("provider_template_id"):
            raise ProviderError("This template has no Twilio Content SID (HX...). Add it on the template first.")
        return self._send(
            account,
            {
                "From": self._addr(account, account["address"]),
                "To": self._addr(account, to),
                "ContentSid": template["provider_template_id"],
                "ContentVariables": json.dumps({str(i + 1): v for i, v in enumerate(params)}),
            },
        )

    @staticmethod
    def signature(token: str, url: str, form: dict[str, str]) -> str:
        data = url + "".join(k + form[k] for k in sorted(form))
        return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()

    def verify(self, account: dict, req: InboundRequest) -> bool:
        _, token = self._creds(account)
        got = req.headers.get("x-twilio-signature", "")
        if not token or not got:
            return False
        return hmac.compare_digest(got, self.signature(token, req.url, req.form))

    def parse(self, account: dict, req: InboundRequest) -> list[Inbound | Receipt]:
        f = req.form
        status = f.get("MessageStatus") or f.get("SmsStatus") or ""
        if status and status not in ("received", "receiving"):
            mapped = TWILIO_STATUS.get(status)
            if not mapped:
                return []  # queued, accepted, sending: nothing to record yet
            err = f"Twilio error {f['ErrorCode']}" if f.get("ErrorCode") else ""
            return [Receipt(f.get("MessageSid", ""), mapped, err)]
        attachments = []
        for i in range(min(int(f.get("NumMedia", "0") or 0), 10)):
            attachments.append({"url": f.get(f"MediaUrl{i}", ""), "type": f.get(f"MediaContentType{i}", "")})
        return [
            Inbound(
                address=e164(f.get("From", "")),
                body=f.get("Body", ""),
                external_id=f.get("MessageSid", ""),
                name=f.get("ProfileName", ""),
                attachments=attachments,
            )
        ]

    def response(self) -> tuple[str, str]:
        return "text/xml", "<Response/>"  # empty TwiML: no automatic reply


# ---- 360dialog (WhatsApp Cloud API format) ----------------------------------------------


class Dialog360(Provider):
    """360dialog, for WhatsApp. NOT live until a 360dialog account exists and
    EXA_360DIALOG_API_KEY (and EXA_360DIALOG_WEBHOOK_SECRET) are set.

    Send:    POST https://waba-v2.360dialog.io/messages with header D360-API-KEY and
             the Cloud API body ({"messaging_product": "whatsapp", "to", "type": "text" | "template"...}).
    Inbound: the Cloud API webhook body (entry[].changes[].value.messages / .statuses).
    Signature: X-Hub-Signature-256: sha256=<hex HMAC-SHA256(secret, raw body)>, as Meta
             signs Cloud API webhooks. Whether 360dialog signs its forwarded webhooks
             this way must be confirmed when the account is set up; until a secret is
             configured, inbound webhooks are refused (fail closed).
    """

    name = "360dialog"
    label = "360dialog"
    channels = ("whatsapp",)
    api_base = "https://waba-v2.360dialog.io"

    def _creds(self, account: dict) -> tuple[str, str]:
        p = _prefix_env(account, "EXA_360DIALOG")
        return os.environ.get(f"{p}_API_KEY", ""), os.environ.get(f"{p}_WEBHOOK_SECRET", "")

    def missing(self, account: dict) -> list[str]:
        p = _prefix_env(account, "EXA_360DIALOG")
        key, secret = self._creds(account)
        return [n for n, v in ((f"{p}_API_KEY", key), (f"{p}_WEBHOOK_SECRET", secret)) if not v]

    def _post(self, url: str, payload: dict, key: str) -> dict:  # pragma: no cover - network
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("D360-API-KEY", key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise ProviderError(f"360dialog answered {e.code}.") from e
        except OSError as e:
            raise ProviderError(f"360dialog could not be reached: {type(e).__name__}") from e

    def _send(self, account: dict, payload: dict) -> str:
        key, _ = self._creds(account)
        if not key:
            raise ProviderError("360dialog is not configured: set " + ", ".join(self.missing(account)) + ".")
        out = self._post(f"{self.api_base}/messages", {"messaging_product": "whatsapp", **payload}, key)
        msgs = out.get("messages") or []
        if not msgs or not msgs[0].get("id"):
            raise ProviderError("360dialog did not return a message id.")
        return str(msgs[0]["id"])

    def send_text(self, account: dict, to: str, body: str, *, ref: str, conn: Any = None) -> str:
        return self._send(
            account, {"recipient_type": "individual", "to": to.lstrip("+"), "type": "text", "text": {"body": body}}
        )

    def send_template(
        self, account: dict, to: str, template: dict, params: list[str], *, ref: str, conn: Any = None
    ) -> str:
        tpl: dict[str, Any] = {"name": template["name"], "language": {"code": template["language"]}}
        if params:
            tpl["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._send(account, {"to": to.lstrip("+"), "type": "template", "template": tpl})

    def verify(self, account: dict, req: InboundRequest) -> bool:
        _, secret = self._creds(account)
        got = req.headers.get("x-hub-signature-256", "")
        if not secret or not got:
            return False
        want = "sha256=" + hmac.new(secret.encode(), req.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(got, want)

    def parse(self, account: dict, req: InboundRequest) -> list[Inbound | Receipt]:
        return parse_cloud_api(json.loads(req.body or b"{}"))


def parse_cloud_api(data: dict) -> list[Inbound | Receipt]:
    """Messages and statuses from a WhatsApp Cloud API webhook body."""
    out: list[Inbound | Receipt] = []
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts") or []}
            for m in value.get("messages") or []:
                kind = m.get("type")
                if kind == "text":
                    body = (m.get("text") or {}).get("body", "")
                elif kind == "button":
                    body = (m.get("button") or {}).get("text", "")
                elif kind == "interactive":
                    i = m.get("interactive") or {}
                    body = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                else:
                    body = f"[{kind} message]"
                attachments = []
                if kind in ("image", "document", "audio", "video"):
                    media = m.get(kind) or {}
                    attachments.append({"media_id": media.get("id", ""), "type": media.get("mime_type", "")})
                    body = media.get("caption", "") or body
                out.append(
                    Inbound(
                        address=e164(str(m.get("from", ""))),
                        body=body,
                        external_id=str(m.get("id", "")),
                        name=names.get(m.get("from"), ""),
                        attachments=attachments,
                    )
                )
            for s in value.get("statuses") or []:
                status = s.get("status", "")
                if status not in ("sent", "delivered", "read", "failed"):
                    continue
                err = "; ".join(f"{e.get('code', '')} {e.get('title', '')}".strip() for e in s.get("errors") or [])
                out.append(Receipt(str(s.get("id", "")), status, err))
    return out


_providers: dict[str, Provider] = {p.name: p for p in (Simulated(), Twilio(), Dialog360())}


def get(name: str) -> Provider:
    p = _providers.get(name)
    if p is None:
        raise ProviderError(f"Unknown provider {name}.")
    return p


def for_channel(channel: str) -> list[Provider]:
    return [p for p in _providers.values() if channel in p.channels]
