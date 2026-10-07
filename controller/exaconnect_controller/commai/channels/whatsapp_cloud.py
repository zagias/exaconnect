"""WhatsApp directly through Meta's Cloud API, and interactive messages (ADR 0029).

A third WhatsApp provider beside Twilio and 360dialog (ADR 0018), on the same
provider interface and under the same rules: the 24-hour service window,
approved templates outside it, opt-out words. It is its own go-live capability
(kind "channel", key "whatsapp-cloud") and refuses to connect, send or accept
webhooks for a business until switched on for it.

- ``meta-cloud``: Graph API, written from Meta's public documentation and NOT
  live. It needs ExaCarib's Meta app to be a Tech Provider with the
  whatsapp_business_messaging and whatsapp_business_management permissions
  approved, EXA_META_APP_ID, EXA_META_APP_SECRET and EXA_META_ES_CONFIG_ID (the
  Embedded Signup configuration). The business connects its WhatsApp Business
  Account (WABA) through Embedded Signup; the business token from the code
  exchange is stored encrypted in the vault, never shown.
    Send:      POST /{phone-number-id}/messages (text, template, interactive)
    Media:     POST /{phone-number-id}/media; GET /{media-id} then its URL
    Templates: GET/POST /{waba-id}/message_templates
    Signup:    GET /oauth/access_token (code exchange), POST /{waba-id}/subscribed_apps,
               POST /{phone-number-id}/register
    Webhooks:  X-Hub-Signature-256 (same code as Messenger), hub.challenge handshake;
               messages, statuses and message_template_status_update.
- ``meta-cloud-simulated``: works with no account, using the Cloud API's own
  webhook format and signature scheme with the account's webhook secret as the
  app secret. Sends land in ``sim_channel_outbox`` (one per message, even on a
  retry), media in ``channel_files``, and Meta's template review is simulated
  (submitted templates are approved on the next sync unless a simulated status
  webhook says otherwise).

Interactive messages (reply buttons, up to 3; lists, up to 10 rows) are allowed
inside the 24-hour window only, for the simulated provider, 360dialog and the
Cloud API. Twilio sends buttons and lists only as pre-created Content
templates, so a free-form interactive message through Twilio is refused with
that reason.
"""

from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import channels, golive, inbox
from . import messaging, providers, social
from .providers import InboundRequest, ProviderError

GRAPH = social.GRAPH
CLOUD = ("meta-cloud", "meta-cloud-simulated")
KEY = "whatsapp-cloud"

golive.declare(
    "channel",
    KEY,
    "WhatsApp through Meta's Cloud API",
    {
        "meta-tech-provider": "ExaCarib's Meta app is a Tech Provider with whatsapp_business_messaging and "
        "whatsapp_business_management approved in app review, and business verification is complete.",
        "embedded-signup": "Embedded Signup configured (EXA_META_ES_CONFIG_ID) and tested end to end with a "
        "test WABA: code exchange, app subscription and phone registration.",
        "webhook-signature": "Webhooks verified with X-Hub-Signature-256 against the app secret, and the "
        "hub.challenge handshake checked; tested with good, bad and missing signatures.",
        "token-storage": "Business tokens are stored only in the encrypted vault and never shown or logged (reviewed).",
        "templates": "Template create, submit and status sync tested against a real WABA.",
    },
    {"platform": "Meta WhatsApp Cloud API", "window_hours": 24},
)

TEMPLATE_STATUS = {
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "PENDING": "pending",
    "IN_APPEAL": "pending",
    "PENDING_DELETION": "rejected",
    "DELETED": "rejected",
    "DISABLED": "rejected",
    "PAUSED": "rejected",
    "LIMIT_EXCEEDED": "rejected",
    "FLAGGED": "approved",  # still sendable while flagged
    "REINSTATED": "approved",
}


class InteractiveError(ValueError):
    """The buttons or list break WhatsApp's limits; the message says which."""


# ---- interactive messages ----------------------------------------------------------------


def check_interactive(spec: dict) -> dict:
    """Validate reply buttons or a list against WhatsApp's limits. Returns the clean spec."""
    body = str(spec.get("body", "")).strip()
    if not body or len(body) > 1024:
        raise InteractiveError("The message text is required, up to 1,024 characters.")
    if bool(spec.get("buttons")) == bool(spec.get("list")):
        raise InteractiveError("Give either reply buttons or a list.")
    if spec.get("buttons"):
        btns = spec["buttons"]
        if not 1 <= len(btns) <= 3:
            raise InteractiveError("Reply buttons: one to three.")
        clean = []
        for i, b in enumerate(btns):
            title = str(b.get("title", "")).strip()
            if not title or len(title) > 20:
                raise InteractiveError("Each button title is 1 to 20 characters.")
            clean.append({"id": str(b.get("id") or f"b{i + 1}")[:256], "title": title})
        if len({b["id"] for b in clean}) != len(clean) or len({b["title"] for b in clean}) != len(clean):
            raise InteractiveError("Button ids and titles must be different from each other.")
        return {"kind": "button", "body": body, "buttons": clean}
    lst = spec["list"]
    label = str(lst.get("button", "")).strip()
    if not label or len(label) > 20:
        raise InteractiveError("The list's button label is 1 to 20 characters.")
    sections = lst.get("sections") or []
    if not 1 <= len(sections) <= 10:
        raise InteractiveError("A list has one to ten sections.")
    rows_total, ids, out = 0, set(), []
    for s in sections:
        rows = []
        for r in s.get("rows") or []:
            title = str(r.get("title", "")).strip()
            desc = str(r.get("description", "")).strip()
            rid = str(r.get("id") or f"r{rows_total + len(rows) + 1}")[:200]
            if not title or len(title) > 24 or len(desc) > 72:
                raise InteractiveError("Each row title is 1 to 24 characters, its description up to 72.")
            if rid in ids:
                raise InteractiveError("Row ids must be different from each other.")
            ids.add(rid)
            rows.append({"id": rid, "title": title, **({"description": desc} if desc else {})})
        if not rows:
            raise InteractiveError("Every section needs at least one row.")
        rows_total += len(rows)
        stitle = str(s.get("title", "")).strip()[:24]
        if len(sections) > 1 and not stitle:
            raise InteractiveError("With more than one section, each needs a title.")
        out.append({"title": stitle, "rows": rows} if stitle else {"rows": rows})
    if rows_total > 10:
        raise InteractiveError("A list has at most ten rows in all.")
    return {"kind": "list", "body": body, "button": label, "sections": out}


def cloud_interactive(spec: dict) -> dict:
    """The Cloud API's "interactive" object (also 360dialog's)."""
    if spec["kind"] == "button":
        return {
            "type": "button",
            "body": {"text": spec["body"]},
            "action": {"buttons": [{"type": "reply", "reply": b} for b in spec["buttons"]]},
        }
    return {
        "type": "list",
        "body": {"text": spec["body"]},
        "action": {"button": spec["button"], "sections": spec["sections"]},
    }


def render_interactive(spec: dict) -> str:
    """Plain-text rendering, for the simulator's outbox."""
    if spec["kind"] == "button":
        opts = [b["title"] for b in spec["buttons"]]
    else:
        opts = [r["title"] for s in spec["sections"] for r in s["rows"]]
    return spec["body"] + "\n" + " | ".join(f"[{o}]" for o in opts)


def supports_interactive(provider: str) -> str:
    """'' when the provider can send free-form interactive messages, else why not."""
    if provider == "twilio":
        return (
            "Twilio sends WhatsApp buttons and lists only as pre-created Content templates. Use a template, "
            "or plain text."
        )
    return ""


def send_interactive(conn: Any, account: dict, to: str, spec: dict, ref: str) -> str:
    prov = providers.get(account["provider"])
    why = supports_interactive(account["provider"])
    if why:
        raise channels.SendBlocked(why)
    if isinstance(prov, CloudApi):
        return prov.send_interactive(account, to, spec, ref=ref, conn=conn)
    if isinstance(prov, providers.Dialog360):
        return prov._send(
            account,
            {"recipient_type": "individual", "to": to.lstrip("+"), "type": "interactive",
             "interactive": cloud_interactive(spec)},
        )  # fmt: skip
    if isinstance(prov, providers.Simulated):
        return prov._record(conn, account, to, render_interactive(spec), f"interactive:{spec['kind']}")
    raise channels.SendBlocked(f"{prov.label} can't send interactive messages.")


# ---- the Cloud API provider ---------------------------------------------------------------


def _sim_ref(conn: Any, account: dict, ref: str) -> str | None:
    row = conn.execute(
        "SELECT provider_ref FROM sim_channel_outbox WHERE account_id = %s AND headers->>'ref' = %s",
        (account["id"], ref),
    ).fetchone()
    return row["provider_ref"] if row else None


class CloudApi(providers.Provider):
    """Meta's WhatsApp Cloud API. NOT live (see the module notes)."""

    name = "meta-cloud"
    label = "Meta Cloud API"
    # Not offered through the phase 2 "add a number" form: accounts come from
    # Embedded Signup (or the simulated sign-up) on their own endpoint.
    channels = ()

    # -- configuration
    def app_secret(self, account: dict) -> str:
        return os.environ.get("EXA_META_APP_SECRET", "")

    @staticmethod
    def signup_env_missing() -> list[str]:
        return [n for n in ("EXA_META_APP_ID", "EXA_META_APP_SECRET", "EXA_META_ES_CONFIG_ID") if not os.environ.get(n)]

    def missing(self, account: dict) -> list[str]:
        out = [] if self.app_secret(account) else ["EXA_META_APP_SECRET"]
        s = account.get("settings") or {}
        if not s.get("token_ref"):
            out.append("the business token (from Embedded Signup)")
        if not s.get("phone_number_id"):
            out.append("the phone number id (from Embedded Signup)")
        return out

    def _token(self, conn: Any, account: dict) -> str:
        return social.token_for(conn, account)

    def _phone(self, account: dict) -> str:
        pid = (account.get("settings") or {}).get("phone_number_id", "")
        if not pid:
            raise ProviderError("This WhatsApp account has no phone number id yet.")
        return str(pid)

    # -- HTTP
    def _graph(  # pragma: no cover - network
        self, method: str, path: str, token: str, payload: dict | None = None, raw: bytes | None = None, ctype=""
    ) -> dict:
        url = path if path.startswith("https://") else f"{GRAPH}{path}"
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", ctype or "application/json")
        try:
            with urllib.request.urlopen(req, timeout=providers.TIMEOUT_S) as r:
                body = r.read()
                if r.headers.get_content_type() == "application/json" or body[:1] in (b"{", b"["):
                    return json.loads(body or b"{}")
                return {"_bytes": body, "_type": r.headers.get_content_type()}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}").get("error") or {}
            except ValueError:
                err = {}
            raise ProviderError(f"Meta answered {e.code}: {str(err.get('message', ''))[:200]}") from e
        except OSError as e:
            raise ProviderError(f"Meta could not be reached: {type(e).__name__}") from e

    def _send(self, conn: Any, account: dict, payload: dict) -> str:
        if self.missing(account):
            raise ProviderError("The Cloud API is not ready: " + ", ".join(self.missing(account)) + ".")
        out = self._graph(
            "POST",
            f"/{self._phone(account)}/messages",
            self._token(conn, account),
            {"messaging_product": "whatsapp", "recipient_type": "individual", **payload},
        )
        msgs = out.get("messages") or []
        if not msgs or not msgs[0].get("id"):
            raise ProviderError("Meta did not return a message id.")
        return str(msgs[0]["id"])

    # -- sending
    def send_text(self, account: dict, to: str, body: str, *, ref: str, conn: Any = None) -> str:
        return self._send(conn, account, {"to": to.lstrip("+"), "type": "text", "text": {"body": body}})

    def send_template(
        self, account: dict, to: str, template: dict, params: list[str], *, ref: str, conn: Any = None
    ) -> str:
        tpl: dict[str, Any] = {"name": template["name"], "language": {"code": template["language"]}}
        if params:
            tpl["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._send(conn, account, {"to": to.lstrip("+"), "type": "template", "template": tpl})

    def send_interactive(self, account: dict, to: str, spec: dict, *, ref: str, conn: Any = None) -> str:
        return self._send(
            conn, account, {"to": to.lstrip("+"), "type": "interactive", "interactive": cloud_interactive(spec)}
        )

    # -- media
    def upload_media(self, conn: Any, account: dict, data: bytes, content_type: str, filename: str) -> str:
        boundary = "exa" + secrets.token_hex(12)
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\n{content_type}\r\n'.encode(),
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + data
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        out = self._graph(
            "POST", f"/{self._phone(account)}/media", self._token(conn, account), raw=b"".join(parts),
            ctype=f"multipart/form-data; boundary={boundary}",
        )  # fmt: skip
        if not out.get("id"):
            raise ProviderError("Meta did not return a media id.")
        return str(out["id"])

    def download_media(self, conn: Any, account: dict, media_id: str) -> tuple[bytes, str]:
        token = self._token(conn, account)
        meta = self._graph("GET", f"/{urllib.parse.quote(media_id)}", token)
        url = str(meta.get("url", ""))
        if not url.startswith("https://"):
            raise ProviderError("Meta did not return a download address for that media.")
        got = self._graph("GET", url, token)
        return got.get("_bytes", b""), str(meta.get("mime_type") or got.get("_type", "application/octet-stream"))

    # -- templates
    def list_templates(self, conn: Any, account: dict) -> list[dict]:
        waba = (account.get("settings") or {}).get("waba_id", "")
        out = self._graph(
            "GET", f"/{waba}/message_templates?fields=id,name,language,status,category,rejected_reason&limit=200",
            self._token(conn, account),
        )  # fmt: skip
        return list(out.get("data") or [])

    def create_template(self, conn: Any, account: dict, tpl: dict) -> str:
        waba = (account.get("settings") or {}).get("waba_id", "")
        body: dict[str, Any] = {"type": "BODY", "text": tpl["body"]}
        n = providers.placeholders(tpl["body"])
        if n:
            body["example"] = {"body_text": [[f"example {i + 1}" for i in range(n)]]}
        out = self._graph(
            "POST",
            f"/{waba}/message_templates",
            self._token(conn, account),
            {
                "name": tpl["name"],
                "language": tpl["language"],
                "category": tpl["category"].upper(),
                "components": [body],
            },
        )
        if not out.get("id"):
            raise ProviderError("Meta did not return a template id.")
        return str(out["id"])

    # -- signup
    def signup(self, code: str, waba_id: str, phone_number_id: str, pin: str) -> dict:
        """Embedded Signup's server half: exchange the code for a business token,
        subscribe the app to the WABA, register the number, read its display number."""
        missing = self.signup_env_missing()
        if missing:
            raise ProviderError("Embedded Signup is not configured: set " + ", ".join(missing) + ".")
        q = urllib.parse.urlencode(
            {
                "client_id": os.environ["EXA_META_APP_ID"],
                "client_secret": os.environ["EXA_META_APP_SECRET"],
                "code": code,
            }
        )
        tok = self._graph("GET", f"/oauth/access_token?{q}", "")
        token = str(tok.get("access_token", ""))
        if not token:
            raise ProviderError("Meta did not return a business token.")
        self._graph("POST", f"/{waba_id}/subscribed_apps", token, {})
        if pin:
            self._graph("POST", f"/{phone_number_id}/register", token, {"messaging_product": "whatsapp", "pin": pin})
        info = self._graph("GET", f"/{phone_number_id}?fields=display_phone_number,verified_name", token)
        return {"token": token, "number": providers.e164(str(info.get("display_phone_number", ""))),
                "verified_name": str(info.get("verified_name", ""))}  # fmt: skip

    # -- webhooks
    def verify(self, account: dict, req: InboundRequest) -> bool:
        secret = self.app_secret(account)
        got = req.headers.get("x-hub-signature-256", "")
        if not secret or not got:
            return False
        return hmac.compare_digest(got, social.meta_sign(secret, req.body))

    def parse(self, account: dict, req: InboundRequest) -> list:
        raise ValueError("Cloud API webhooks have their own address.")


class CloudApiSimulated(CloudApi):
    name = "meta-cloud-simulated"
    label = "Simulated Cloud API"
    simulated = True

    def app_secret(self, account: dict) -> str:
        return account["hook_secret"]

    def missing(self, account: dict) -> list[str]:
        return []

    def _record(self, conn: Any, account: dict, to: str, body: str, template: str, ref: str) -> str:
        seen = _sim_ref(conn, account, ref)
        if seen:
            return seen
        if (account.get("settings") or {}).get("fail_sends"):
            raise ProviderError("Simulated failure: this account is set to fail sends.")
        pref = "wamid.SIM" + secrets.token_hex(12)
        conn.execute(
            """INSERT INTO sim_channel_outbox (customer_id, account_id, channel, to_address, body, template, headers,
                                               provider_ref) VALUES (%s, %s, 'whatsapp', %s, %s, %s, %s, %s)""",
            (account["customer_id"], account["id"], to, body, template, Jsonb({"ref": ref}), pref),
        )
        return pref

    def send_text(self, account: dict, to: str, body: str, *, ref: str, conn: Any = None) -> str:
        return self._record(conn, account, to, body, "", ref)

    def send_template(
        self, account: dict, to: str, template: dict, params: list[str], *, ref: str, conn: Any = None
    ) -> str:
        if not template.get("provider_template_id"):
            raise ProviderError("This template has not been submitted to Meta.")
        return self._record(
            conn,
            account,
            to,
            providers.render(template["body"], params),
            f"{template['name']}:{template['language']}",
            ref,
        )

    def send_interactive(self, account: dict, to: str, spec: dict, *, ref: str, conn: Any = None) -> str:
        return self._record(conn, account, to, render_interactive(spec), f"interactive:{spec['kind']}", ref)

    def upload_media(self, conn: Any, account: dict, data: bytes, content_type: str, filename: str) -> str:
        row = conn.execute(
            """INSERT INTO channel_files (customer_id, owner, name, content_type, size, data)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (account["customer_id"], f"wa-cloud-sim:{account['id']}", filename, content_type, len(data), data),
        ).fetchone()
        return f"sim-media-{row['id']}"

    def download_media(self, conn: Any, account: dict, media_id: str) -> tuple[bytes, str]:
        fid = media_id.removeprefix("sim-media-")
        row = conn.execute(
            "SELECT data, content_type FROM channel_files WHERE id::text = %s AND customer_id = %s AND owner = %s",
            (fid, account["customer_id"], f"wa-cloud-sim:{account['id']}"),
        ).fetchone()
        if row is None:
            raise ProviderError("No such media.")
        return bytes(row["data"]), row["content_type"]

    def list_templates(self, conn: Any, account: dict) -> list[dict]:
        # Meta's review, simulated: anything still pending is approved now.
        conn.execute(
            "UPDATE sim_wa_templates SET status = 'APPROVED' WHERE account_id = %s AND status = 'PENDING'",
            (account["id"],),
        )
        return conn.execute(
            "SELECT id, name, language, status, category, rejected_reason FROM sim_wa_templates WHERE account_id = %s",
            (account["id"],),
        ).fetchall()

    def create_template(self, conn: Any, account: dict, tpl: dict) -> str:
        tid = str(10**15 + secrets.randbelow(10**15))
        conn.execute(
            """INSERT INTO sim_wa_templates (id, account_id, name, language, category, status)
               VALUES (%s, %s, %s, %s, %s, 'PENDING')""",
            (tid, account["id"], tpl["name"], tpl["language"], tpl["category"].upper()),
        )
        return tid

    def signup(self, code: str, waba_id: str, phone_number_id: str, pin: str) -> dict:
        raise ProviderError("The simulated sign-up does not exchange codes.")


PROVIDERS = {p.name: p for p in (CloudApi(), CloudApiSimulated())}
providers._providers.update(PROVIDERS)


# ---- the WhatsApp channel, extended -----------------------------------------------------------


class WhatsAppPlus(messaging.WhatsApp):
    """Phase 2's WhatsApp rules, plus the Cloud API's go-live gate and interactive messages."""

    def check_send(self, conn: psycopg.Connection, conversation: dict, body: str, template: str) -> None:
        acct = messaging.account_for(conn, conversation)
        if acct and acct["provider"] in CLOUD and not golive.enabled(conn, "channel", KEY, conversation["customer_id"]):
            raise channels.SendBlocked(
                "WhatsApp through Meta's Cloud API is not switched on for this business yet. ExaCarib switches it "
                "on once its go-live checks pass."
            )
        super().check_send(conn, conversation, body, template)

    def rules(self, conn, conversation: dict, identity: dict, body: str, template: str) -> None:
        if template == "interactive":
            acct = self.account(conn, conversation)
            why = supports_interactive(acct["provider"])
            if why:
                raise channels.SendBlocked(why)
            try:
                super().rules(conn, conversation, identity, body, "")  # the 24-hour window
            except channels.SendBlocked as e:
                raise channels.SendBlocked(f"Buttons and lists can only be sent inside the window. {e}") from e
            return
        super().rules(conn, conversation, identity, body, template)

    def deliver(self, conn: psycopg.Connection, conversation: dict, message: dict) -> dict:
        if message["template"] != "interactive":
            return super().deliver(conn, conversation, message)
        self.check_send(conn, conversation, message["body"], message["template"])
        acct = self.account(conn, conversation)
        ident = messaging.identity_of(conn, conversation)
        row = conn.execute("SELECT spec FROM wa_interactive WHERE message_id = %s", (message["id"],)).fetchone()
        if row is None:
            raise channels.SendBlocked("The buttons or list for this message are missing.")
        try:
            ref = send_interactive(conn, acct, ident["address"], row["spec"], str(message["id"]))
        except channels.SendBlocked:
            raise
        except Exception as e:
            conn.execute(
                "UPDATE channel_accounts SET last_error = %s, last_error_at = now() WHERE id = %s",
                (f"{type(e).__name__}: {e}"[:500], acct["id"]),
            )
            raise
        from .. import usage

        conn.execute("UPDATE channel_accounts SET last_sent_at = now() WHERE id = %s", (acct["id"],))
        messaging.remember_thread(conn, conversation["id"], conversation["customer_id"], acct["id"])
        usage.record(conn, conversation["customer_id"], "message_out:whatsapp", 1, ref=str(message["id"]))
        return {"status": "sent", "provider_ref": ref}


WHATSAPP = WhatsAppPlus("whatsapp", "WhatsApp")
channels.register(WHATSAPP)


def send_interactive_reply(
    conn: psycopg.Connection, customer_id: Any, conversation_id: Any, spec: dict, *, author: str, user_id: Any
) -> dict:
    """A person's reply with buttons or a list, through the inbox (so every rule applies)."""
    conv = inbox.get(conn, customer_id, conversation_id)
    if conv["channel"] != "whatsapp":
        raise inbox.InboxError("Buttons and lists are for WhatsApp conversations.", 422)
    msg = inbox.send(
        conn, customer_id, conversation_id, spec["body"], author_kind="user", author=author, user_id=user_id,
        template="interactive", take_over=True,
    )  # fmt: skip
    conn.execute("INSERT INTO wa_interactive (message_id, spec) VALUES (%s, %s)", (msg["id"], Jsonb(spec)))
    return msg


# ---- webhooks --------------------------------------------------------------------------------


def _template_updates(data: dict) -> list[dict]:
    out = []
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") == "message_template_status_update":
                out.append(change.get("value") or {})
    return out


def apply_template_status(conn: psycopg.Connection, account: dict, v: dict) -> int:
    status = TEMPLATE_STATUS.get(str(v.get("event", "")).upper())
    if not status:
        return 0
    cur = conn.execute(
        """UPDATE whatsapp_templates SET status = %s, status_reason = %s, status_by = 'Meta (webhook)',
                  updated_at = now()
           WHERE customer_id = %s AND provider_template_id = %s""",
        (status, str(v.get("reason") or "")[:300], account["customer_id"], str(v.get("message_template_id", ""))),
    )
    return cur.rowcount


def check_on(conn: psycopg.Connection, account: dict) -> None:
    if not golive.enabled(conn, "channel", KEY, account["customer_id"]):
        raise social.ChannelOff("WhatsApp through Meta's Cloud API is not switched on for this business.")


def _apply(conn: psycopg.Connection, account: dict, data: dict) -> dict:
    out = social.apply(conn, account, providers.parse_cloud_api(data))
    out["templates"] = sum(apply_template_status(conn, account, v) for v in _template_updates(data))
    return out


def handle_webhook(conn: psycopg.Connection, account: dict, req: InboundRequest) -> dict:
    prov = PROVIDERS[account["provider"]]
    if not prov.verify(account, req):
        messaging.log_webhook(conn, account, "rejected", "signature did not verify")
        raise PermissionError("The webhook signature did not verify.")
    try:
        check_on(conn, account)
    except social.ChannelOff:
        messaging.log_webhook(conn, account, "refused", "channel not switched on")
        raise
    out = _apply(conn, account, json.loads(req.body or b"{}"))
    messaging.log_webhook(conn, account, "accepted", f"{out}")
    return out


def app_webhook(conn: psycopg.Connection, data: dict) -> dict:
    """The app-wide webhook's WhatsApp part (already verified): each change is
    routed by its phone number id, or for template updates by its WABA id."""
    out = {"received": 0, "duplicates": 0, "receipts": 0, "opt_changes": 0, "templates": 0, "unknown_accounts": 0,
           "refused": 0}  # fmt: skip
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            pid = str((value.get("metadata") or {}).get("phone_number_id", ""))
            if pid:
                a = conn.execute(
                    "SELECT * FROM channel_accounts WHERE provider = 'meta-cloud'"
                    " AND settings->>'phone_number_id' = %s",
                    (pid,),
                ).fetchone()
            else:
                a = conn.execute(
                    "SELECT * FROM channel_accounts WHERE provider = 'meta-cloud'"
                    " AND settings->>'waba_id' = %s LIMIT 1",
                    (str(entry.get("id", "")),),
                ).fetchone()
            if a is None:
                out["unknown_accounts"] += 1
                continue
            try:
                check_on(conn, a)
            except social.ChannelOff:
                messaging.log_webhook(conn, a, "refused", "channel not switched on")
                out["refused"] += 1
                continue
            got = _apply(conn, a, {"entry": [{"id": entry.get("id"), "changes": [change]}]})
            messaging.log_webhook(conn, a, "accepted", f"{got}")
            for k, v in got.items():
                out[k] += v
    return out


# ---- templates sync ---------------------------------------------------------------------------


def submit_template(conn: psycopg.Connection, account: dict, tpl: dict) -> dict:
    prov = PROVIDERS[account["provider"]]
    tid = prov.create_template(conn, account, tpl)
    return conn.execute(
        """UPDATE whatsapp_templates SET provider_template_id = %s, status = 'pending', status_reason = '',
                  status_by = 'submitted to Meta', updated_at = now() WHERE id = %s RETURNING *""",
        (tid, tpl["id"]),
    ).fetchone()


def sync_templates(conn: psycopg.Connection, account: dict) -> dict:
    """Pull every template's status from the WABA into CommAI's template list."""
    prov = PROVIDERS[account["provider"]]
    changed = 0
    remote = prov.list_templates(conn, account)
    for t in remote:
        status = TEMPLATE_STATUS.get(str(t.get("status", "")).upper(), "pending")
        cur = conn.execute(
            """UPDATE whatsapp_templates SET status = %s, status_reason = %s, provider_template_id = %s,
                      status_by = 'Meta (sync)', updated_at = now()
               WHERE customer_id = %s AND name = %s AND language = %s
                 AND (status <> %s OR provider_template_id <> %s)""",
            (status, str(t.get("rejected_reason") or "")[:300] if status == "rejected" else "", str(t["id"]),
             account["customer_id"], t["name"], t["language"], status, str(t["id"])),
        )  # fmt: skip
        changed += cur.rowcount
    return {"remote": len(remote), "updated": changed}


# ---- click to chat ---------------------------------------------------------------------------


def click_to_chat(number: str, text: str = "") -> str:
    digits = re.sub(r"\D", "", number)
    url = f"https://wa.me/{digits}"
    return url + ("?text=" + urllib.parse.quote(text) if text else "")


def qr_svg(url: str) -> str:
    import io

    import segno

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#10213D", xmldecl=False)
    return buf.getvalue().decode()


# ---- simulated traffic -----------------------------------------------------------------------


def sim_payload(
    account: dict, sender: str, text: str, msg_id: str, name: str = "", reply_to: dict | None = None
) -> dict:
    settings = account.get("settings") or {}
    msg: dict[str, Any] = {
        "from": sender.lstrip("+"),
        "id": msg_id,
        "timestamp": str(int(dt.datetime.now(dt.UTC).timestamp())),
    }
    if reply_to:
        msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": reply_to})
    else:
        msg.update(type="text", text={"body": text})
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": settings.get("waba_id", ""),
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": account["address"].lstrip("+"),
                                         "phone_number_id": settings.get("phone_number_id", "")},
                            "contacts": [{"wa_id": sender.lstrip("+"), "profile": {"name": name}}],
                            "messages": [msg],
                        },
                    }
                ],
            }
        ],
    }  # fmt: skip


def sim_status_payload(account: dict, ref: str, status: str, recipient: str) -> dict:
    settings = account.get("settings") or {}
    st: dict[str, Any] = {"id": ref, "status": status, "recipient_id": recipient.lstrip("+"),
                          "timestamp": str(int(dt.datetime.now(dt.UTC).timestamp()))}  # fmt: skip
    if status == "failed":
        st["errors"] = [{"code": 131047, "title": "Re-engagement message"}]
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": settings.get("waba_id", ""), "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": settings.get("phone_number_id", "")},
            "statuses": [st]}}]}],
    }  # fmt: skip


def sim_template_payload(account: dict, tpl: dict, event: str, reason: str = "") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": (account.get("settings") or {}).get("waba_id", ""), "changes": [{
            "field": "message_template_status_update",
            "value": {"event": event, "message_template_id": tpl["provider_template_id"],
                      "message_template_name": tpl["name"], "message_template_language": tpl["language"],
                      "reason": reason}}]}],
    }  # fmt: skip


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
