"""Microsoft Teams connector (ADR 0035): staff notifications, handover alerts
and approval requests.

Two Microsoft pieces, both from their public documentation:

- **Incoming webhook** (a Teams Workflows "post to a channel when a webhook
  request is received" flow, or a classic connector URL). The business pastes
  the URL through secure entry; it carries its own key, so it is stored
  encrypted and never shown again. Messages are Adaptive Cards.
- **Bot** (Azure Bot / Bot Framework, ExaCarib's app: EXA_TEAMS_BOT_ID and
  EXA_TEAMS_BOT_PASSWORD) for Approve and Reject buttons. A business links a
  channel by sending the bot "link <code>" (a one-time code from Jibsy).
  Every request to the bot endpoint carries a Bot Framework JWT, checked
  here (RS256 against Microsoft's published keys, issuer, audience = our bot
  id, expiry, and the serviceUrl claim). A press is accepted only from the
  linked tenant, from a Teams user linked to a Jibsy person with a reply
  seat, and is decided by the action service.

Without the bot, approval requests go through the webhook with an "Open in
Jibsy" button, and the decision is made in Jibsy.

Customer data: as for Slack, messages say no more than the ``share`` setting
allows, and contact details and card numbers are masked.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..automation import oauth
from . import ActionSpec, ConnectorError, Field, kit
from .more_common import (
    SHARE_LEVELS,
    MoreConnector,
    approval_brief,
    conversation_brief,
    state_get,
    state_put,
)
from .slack import mask

BOT_ID_ENV = "EXA_TEAMS_BOT_ID"
BOT_PASSWORD_ENV = "EXA_TEAMS_BOT_PASSWORD"
OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
ISSUER = "https://api.botframework.com"
TOKEN_URL = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
WEBHOOK = (
    r"https://([a-z0-9-]+\.)+(webhook\.office\.com|logic\.azure\.com|environment\.api\.powerplatform\.com)"
    r"(:443)?/[A-Za-z0-9/@?=&%._~:+-]{10,600}"
)
SERVICE_URL = r"https://(smba\.trafficmanager\.net|[a-z0-9.-]+\.botframework\.com)(/[A-Za-z0-9/._-]*)?"
SIM_WEBHOOK = "https://example.webhook.office.com/webhookb2/simulated-channel"
LINK_TTL_S = 1800

_keys: dict[str, Any] = {"at": 0.0, "keys": {}}


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _signing_keys(refresh: bool = False) -> dict:
    """Microsoft's Bot Framework signing keys, cached for an hour."""
    from ..automation import http

    if not refresh and _keys["keys"] and time.time() - _keys["at"] < 3600:
        return _keys["keys"]
    try:
        cfg = http.request("GET", OPENID)
        jwks = http.request("GET", cfg.body["jwks_uri"])
    except (http.NetworkError, KeyError, TypeError):
        return _keys["keys"]
    keys = {}
    for k in (jwks.body or {}).get("keys") or []:
        if k.get("kty") == "RSA" and k.get("kid"):
            n = int.from_bytes(_b64url(k["n"]), "big")
            e = int.from_bytes(_b64url(k["e"]), "big")
            keys[k["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
    _keys.update(at=time.time(), keys=keys)
    return keys


def verify_bot_token(authorization: str, service_url: str, now: float | None = None) -> dict | None:
    """The claims of a valid Bot Framework token for our bot, or None."""
    bot_id = os.environ.get(BOT_ID_ENV, "")
    if not bot_id or not authorization.startswith("Bearer "):
        return None
    parts = authorization[7:].strip().split(".")
    if len(parts) != 3:
        return None
    try:
        head, claims, sig = json.loads(_b64url(parts[0])), json.loads(_b64url(parts[1])), _b64url(parts[2])
    except (ValueError, TypeError):
        return None
    if head.get("alg") != "RS256":
        return None
    key = _signing_keys().get(head.get("kid", "")) or _signing_keys(refresh=True).get(head.get("kid", ""))
    if key is None:
        return None
    try:
        key.verify(sig, f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return None
    t = now or time.time()
    if claims.get("iss") != ISSUER or claims.get("aud") != bot_id:
        return None
    if not (claims.get("exp", 0) + 300 > t >= claims.get("nbf", 0) - 300):
        return None
    if service_url and claims.get("serviceurl", claims.get("serviceUrl")) not in (None, service_url):
        return None
    return claims


def link_code(conn, customer_id: Any) -> str:
    """A one-time code a business sends the bot ("link <code>") to link a channel."""
    code = secrets.token_hex(4).upper()
    state_put(
        conn,
        customer_id,
        "teams",
        "link_code",
        {"id": hashlib.sha256(code.encode()).hexdigest(), "expires": time.time() + LINK_TTL_S},
    )
    return code


def _card(title: str, lines: list[str], actions: list[dict]) -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True}]
    body += [{"type": "TextBlock", "text": x, "wrap": True, "spacing": "Small"} for x in lines]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                    "actions": actions,
                },
            }
        ],
    }


class TeamsSim(kit.Simulator):
    app = "teams"

    @kit.Simulator.route("POST", r"/webhookb2/")
    def webhook(self, conn, c, req, m):
        mid = self.new_id(conn, c, "message", digits=True)
        self.put(conn, c, "message", mid, {"via": "webhook", **(req.body or {})})
        return 200, "1"

    @kit.Simulator.route("POST", r"/botframework\.com/oauth2/v2\.0/token$")
    def token(self, conn, c, req, m):
        return 200, {"token_type": "Bearer", "expires_in": 3599, "access_token": "sim-bot-token"}

    @kit.Simulator.route("POST", r"/v3/conversations/([^/]+)/activities$")
    def activity(self, conn, c, req, m):
        mid = self.new_id(conn, c, "message", digits=True)
        self.put(conn, c, "message", mid, {"via": "bot", "conversation": m.group(1), **(req.body or {})})
        return 201, {"id": mid}


class Teams(MoreConnector):
    app = "teams"
    label = "Microsoft Teams"
    category = "chat"
    description = "Tell staff in Teams about handovers and ask them to approve actions."
    auth = "credentials"
    credentials = (kit.Credential("webhook_url", "Incoming webhook URL", pattern=WEBHOOK),)
    settings_fields = (
        kit.Setting(
            "share", "What staff messages may say about customers: none, names or summary", "|".join(SHARE_LEVELS)
        ),
    )
    simulator = TeamsSim()
    needs_from_exacarib = (
        "For Approve and Reject buttons: an Azure Bot (multi-tenant) with the Teams channel and messaging endpoint "
        "{EXA_PUBLIC_URL}/api/v1/commai/teams/messages: EXA_TEAMS_BOT_ID and EXA_TEAMS_BOT_PASSWORD, plus a Teams "
        "app package. Notifications alone need only each business's incoming webhook URL."
    )
    webhooks = "Button presses reach the bot endpoint, checked with the Bot Framework token."
    docs_url = "https://learn.microsoft.com/microsoftteams/platform/"
    actions = {
        "notify_staff": ActionSpec(
            "notify_staff", "Send staff a message", "create", fields=(Field("text", "Message", "text"),)
        ),
        "handover_alert": ActionSpec(
            "handover_alert", "Alert staff to a handover", "create", fields=(Field("conversation_id", "Conversation"),)
        ),
        "request_approval": ActionSpec(
            "request_approval",
            "Ask staff to approve an action",
            "create",
            fields=(Field("run_id", "Action waiting for approval"),),
        ),
    }

    def env_names(self) -> list[str]:
        return [BOT_ID_ENV, BOT_PASSWORD_ENV]

    def base_url(self, conn, connection: dict) -> str:
        return "https://example.webhook.office.com"

    def live_auth_headers(self, conn, connection: dict) -> dict:
        return {}  # the webhook URL carries its own key; bot calls add their own token

    def bot_ready(self) -> bool:
        return bool(os.environ.get(BOT_ID_ENV) and os.environ.get(BOT_PASSWORD_ENV))

    def _webhook_url(self, conn, connection: dict) -> str:
        if self.simulated(connection):
            return SIM_WEBHOOK
        return oauth.credentials(conn, connection)["webhook_url"]

    def health(self, conn, connection: dict) -> dict:
        if not self.simulated(connection):
            try:
                url = self._webhook_url(conn, connection)
            except ConnectorError as e:
                return {"ok": False, "cause": e.cause, "detail": str(e)}
            if not re.fullmatch(WEBHOOK, url):
                return {"ok": False, "cause": "mapping", "detail": "The incoming webhook URL is not a Teams one."}
            # Posting is the only check a Teams webhook offers, so health does not send.
            return {"ok": True, "cause": "", "detail": "Teams webhook stored."}
        return {"ok": True, "cause": "", "detail": "Microsoft Teams answering (the stand-in)."}

    def _send(self, conn, connection: dict, key: str, message: dict, via_bot: dict | None = None) -> dict:
        known = self.known(conn, connection, key)
        if known:
            return {"message_id": known["object_id"], "replayed": True}
        if self.dry(connection):
            return {"dry_run": True, "would_send": message}
        if via_bot:
            tok = self.call(
                conn,
                connection,
                "POST",
                TOKEN_URL,
                form={
                    "grant_type": "client_credentials",
                    "client_id": os.environ.get(BOT_ID_ENV, ""),
                    "client_secret": os.environ.get(BOT_PASSWORD_ENV, ""),
                    "scope": "https://api.botframework.com/.default",
                },
            )
            url = f"{via_bot['service_url'].rstrip('/')}/v3/conversations/{via_bot['conversation_id']}/activities"
            r = self.call(
                conn,
                connection,
                "POST",
                url,
                json_body=message,
                headers={"Authorization": f"Bearer {tok.body['access_token']}"},
            )
            mid = str((r.body or {}).get("id", "")) if isinstance(r.body, dict) else ""
        else:
            self.call(conn, connection, "POST", self._webhook_url(conn, connection), json_body=message)
            mid = kit.ref(key)
        self.remember(conn, connection, key, "message", mid)
        return {"message_id": mid, "via": "bot" if via_bot else "webhook"}

    def cause(self, status: int, body: Any) -> str:
        if status in (401, 403, 404, 410):  # a deleted or disabled webhook or flow
            return "expired_signin" if status in (401, 410) else ("permission" if status == 403 else "mapping")
        return super().cause(status, body)

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        share = self.settings(connection).get("share") or "none"
        if action == "notify_staff":
            text = inputs["text"] if share == "summary" else mask(inputs["text"])
            return self._send(conn, connection, key, _card("Message from Jibsy", [text], []))
        if action == "handover_alert":
            try:
                brief = conversation_brief(conn, connection["customer_id"], inputs["conversation_id"], share)
            except ValueError as e:
                raise ConnectorError(str(e), "input") from None
            head = "A conversation needs a person" + (" (urgent)" if brief["priority"] == "urgent" else "") + "."
            card = _card(
                head,
                [mask(x) for x in brief["lines"]],
                [{"type": "Action.OpenUrl", "title": "Open in Jibsy", "url": brief["link"]}],
            )
            return self._send(conn, connection, key, card)
        if action == "request_approval":
            try:
                brief = approval_brief(conn, connection["customer_id"], inputs["run_id"], share)
            except ValueError as e:
                raise ConnectorError(str(e), "input") from None
            linked = state_get(conn, connection["customer_id"], self.app, "bot_conversation")
            head = f"Approval needed: {brief['what']}"
            lines = [mask(x) for x in brief["lines"]]
            if self.bot_ready() and linked.get("conversation_id"):
                data = {"commai": "", "c": str(connection["customer_id"]), "r": brief["run_id"]}
                card = _card(
                    head,
                    lines,
                    [
                        {
                            "type": "Action.Submit",
                            "title": "Approve",
                            "style": "positive",
                            "data": {**data, "commai": "approve"},
                        },
                        {
                            "type": "Action.Submit",
                            "title": "Reject",
                            "style": "destructive",
                            "data": {**data, "commai": "reject"},
                        },
                        {"type": "Action.OpenUrl", "title": "Open in Jibsy", "url": brief["link"]},
                    ],
                )
                return self._send(conn, connection, key, card, via_bot=linked)
            card = _card(
                head,
                [*lines, "Decide in Jibsy."],
                [{"type": "Action.OpenUrl", "title": "Open in Jibsy", "url": brief["link"]}],
            )
            return self._send(conn, connection, key, card)
        raise ConnectorError(f"Unknown action {action}.", "input")

    def bot_reply(self, conn, customer_id: Any, text: str) -> None:
        """A short answer in the linked channel (best effort)."""
        from . import connection as get_connection

        row = get_connection(conn, customer_id, self.app)
        linked = state_get(conn, customer_id, self.app, "bot_conversation")
        if row is None or not linked.get("conversation_id") or not self.bot_ready():
            return
        try:
            self._send(
                conn,
                {**row, "test": False},
                f"reply:{secrets.token_hex(6)}",
                {"type": "message", "text": text},
                via_bot=linked,
            )
        except ConnectorError:
            pass


kit.register(Teams())
