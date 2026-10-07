"""Slack connector (ADR 0029): staff notifications, handover alerts and
approval requests with Approve and Reject buttons.

Written from Slack's Web API (oauth.v2.access, auth.test,
conversations.list, conversations.history, chat.postMessage) and its
interactivity payloads. ExaCarib registers one Slack app (EXA_SLACK_CLIENT_ID,
EXA_SLACK_CLIENT_SECRET, EXA_SLACK_SIGNING_SECRET) with the bot scopes
chat:write, channels:read, groups:read, channels:history and
groups:history (to check a message was not already posted), the redirect URI
{EXA_PUBLIC_URL}/api/v1/commai/oauth/slack/callback and the interactivity URL
{EXA_PUBLIC_URL}/api/v1/commai/slack/interactions.

- Customer data: messages say no more than the business's ``share`` setting
  allows (none, names or summary); email addresses, phone numbers and card
  numbers are always masked in free text.
- Buttons: a press is checked with Slack's request signing (v0, HMAC-SHA256
  of "v0:{timestamp}:{body}", five-minute window), the Slack workspace must be
  the business's own, and the Slack user must be linked to a CommAI person
  with a reply seat. Approval then goes through the action service, so every
  rule (a second person approves, never the AI) still holds.
- Idempotency: Slack has no idempotency key, so each message carries
  metadata with a reference from the idempotency key; before posting, the
  channel's recent messages are checked for it.
- Errors: Slack answers 200 with ok=false; the error names the repair cause.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from ..automation import oauth
from . import ActionSpec, ConnectorError, Field, kit
from .more_common import (
    SHARE_LEVELS,
    MoreConnector,
    approval_brief,
    conversation_brief,
    luhn_card,
    state_get,
    state_put,
)

oauth.PROVIDERS["slack"] = oauth.Provider(
    "slack",
    "Slack",
    "https://slack.com/oauth/v2/authorize",
    "https://slack.com/api/oauth.v2.access",
    ("chat:write", "channels:read", "groups:read", "channels:history", "groups:history"),
    "EXA_SLACK",
    scope_sep=",",
    expiring=False,
)

SIGNING_ENV = "EXA_SLACK_SIGNING_SECRET"
EXPIRED = {"invalid_auth", "token_revoked", "token_expired", "account_inactive", "not_authed"}
PERMISSION = {"missing_scope", "restricted_action", "not_allowed_token_type", "ekm_access_denied"}
MAPPING = {"channel_not_found", "is_archived", "not_in_channel"}
CHANNEL = r"[CGD][A-Z0-9]{6,20}"


def mask(text: str) -> str:
    """Free text sent to team chat never carries contact details or card numbers."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", text or "")
    if luhn_card(text):
        text = re.sub(r"(?:\d[ -]?){13,19}", "[card]", text)
    return re.sub(r"\+?\d[\d ()-]{7,}\d", "[phone]", text)


def verify_request(secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    """Slack request signing, version 0."""
    if not secret or not kit.fresh(timestamp, 300, now):
        return False
    return kit.same(signature, "v0=" + kit.hmac_hex(secret, f"v0:{timestamp}:".encode() + body))


class SlackSim(kit.Simulator):
    """Answers like Slack's Web API (HTTP 200 with ok true or false)."""

    app = "slack"

    def error_body(self, status: int, message: str) -> Any:
        return {"ok": False, "error": {401: "invalid_auth", 403: "missing_scope"}.get(status, "internal_error")}

    @kit.Simulator.route("POST", r"/api/auth\.test$")
    def auth_test(self, conn, c, req, m):
        return 200, {"ok": True, "team": "Example workspace", "team_id": "T0SIM", "user_id": "U0BOT", "bot_id": "B0"}

    @kit.Simulator.route("GET", r"/api/conversations\.list$")
    def channels(self, conn, c, req, m):
        chans = [
            {"id": "C0SUPPORT1", "name": "support", "is_private": False},
            {"id": "G0MANAGERS", "name": "managers", "is_private": True},
        ]
        cursor = req.query.get("cursor", "")
        page = chans[1:] if cursor else chans[:1]
        return 200, {"ok": True, "channels": page, "response_metadata": {"next_cursor": "" if cursor else "dGVhbTpD"}}

    @kit.Simulator.route("POST", r"/api/chat\.postMessage$")
    def post(self, conn, c, req, m):
        b = req.body or {}
        if not re.fullmatch(CHANNEL, b.get("channel", "")):
            return 200, {"ok": False, "error": "channel_not_found"}
        ts = f"{int(time.time())}.{self.new_id(conn, c, 'message', digits=True)}"
        self.put(conn, c, "message", ts, {**b, "ts": ts})
        return 200, {"ok": True, "channel": b["channel"], "ts": ts, "message": {"text": b.get("text", "")}}

    @kit.Simulator.route("GET", r"/api/conversations\.history$")
    def history(self, conn, c, req, m):
        ch = req.query.get("channel", "")
        msgs = [x for x in self.all(conn, c, "message") if x.get("channel") == ch]
        return 200, {"ok": True, "messages": list(reversed(msgs))[:20], "has_more": False}


class Slack(MoreConnector):
    app = "slack"
    label = "Slack"
    category = "chat"
    description = "Tell staff in Slack about handovers and ask them to approve actions with a button."
    auth = "oauth"
    settings_fields = (
        kit.Setting("channel", "Channel for staff messages (channel ID)", CHANNEL),
        kit.Setting("approvals_channel", "Channel for approval requests (channel ID)", CHANNEL),
        kit.Setting(
            "share", "What staff messages may say about customers: none, names or summary", "|".join(SHARE_LEVELS)
        ),
    )
    simulator = SlackSim()
    needs_from_exacarib = (
        "One Slack app (api.slack.com/apps), distributed: EXA_SLACK_CLIENT_ID, EXA_SLACK_CLIENT_SECRET and "
        "EXA_SLACK_SIGNING_SECRET; bot scopes chat:write, channels:read, groups:read, "
        "channels:history, groups:history; interactivity on."
    )
    webhooks = "Button presses arrive at /api/v1/commai/slack/interactions, checked with Slack request signing."
    docs_url = "https://api.slack.com/web"
    actions = {
        "list_channels": ActionSpec("list_channels", "List channels", "read"),
        "notify_staff": ActionSpec(
            "notify_staff",
            "Send staff a message",
            "create",
            fields=(Field("text", "Message", "text"), Field("channel", "Channel", required=False)),
        ),
        "handover_alert": ActionSpec(
            "handover_alert",
            "Alert staff to a handover",
            "create",
            fields=(Field("conversation_id", "Conversation"), Field("channel", "Channel", required=False)),
        ),
        "request_approval": ActionSpec(
            "request_approval",
            "Ask staff to approve an action",
            "create",
            fields=(Field("run_id", "Action waiting for approval"), Field("channel", "Channel", required=False)),
        ),
    }

    def env_names(self) -> list[str]:
        return [*super().env_names(), SIGNING_ENV]

    def real_ready(self, conn, customer_id: Any) -> tuple[bool, list[str]]:
        ok, why = super().real_ready(conn, customer_id)
        if not os.environ.get(SIGNING_ENV):
            why = [*why, f"The controller needs {SIGNING_ENV} to check button presses."]
        return not why, why

    def base_url(self, conn, connection: dict) -> str:
        return "https://slack.com"

    def validate(self, action: str, inputs: dict) -> dict:
        out = super().validate(action, inputs)
        if out.get("channel") and not re.fullmatch(CHANNEL, str(out["channel"])):
            raise ValueError("Channel must be a Slack channel ID, like C0123ABCD.")
        if action == "notify_staff" and len(out["text"]) > 3000:
            raise ValueError("Keep staff messages under 3,000 characters.")
        return out

    # ---- calls ------------------------------------------------------------------------

    def api(self, conn, connection: dict, method: str, name: str, **kw) -> dict:
        r = self.call(conn, connection, method, f"/api/{name}", **kw)
        body = r.body if isinstance(r.body, dict) else {}
        if body.get("ok"):
            return body
        err = body.get("error") if isinstance(body.get("error"), str) else "unknown_error"
        cause = (
            "expired_signin"
            if err in EXPIRED
            else "permission"
            if err in PERMISSION
            else "mapping"
            if err in MAPPING
            else "provider"
            if err in ("ratelimited", "internal_error", "fatal_error")
            else "input"
        )
        raise ConnectorError(f"Slack refused {name}: {err}.", cause)

    def team(self, conn, connection: dict) -> str:
        """The business's Slack workspace (learnt once from auth.test)."""
        known = state_get(conn, connection["customer_id"], self.app, "team").get("id", "")
        if known:
            return known
        t = self.api(conn, connection, "POST", "auth.test")
        state_put(conn, connection["customer_id"], self.app, "team", {"id": t["team_id"], "name": t.get("team", "")})
        return t["team_id"]

    def health(self, conn, connection: dict) -> dict:
        try:
            t = self.api(conn, connection, "POST", "auth.test")
            state_put(conn, connection["customer_id"], self.app, "team", {"id": t["team_id"], "name": t.get("team")})
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        mode = "the stand-in" if self.simulated(connection) else "Slack"
        return {"ok": True, "cause": "", "detail": f"Slack workspace {t.get('team', '')!r} answering ({mode})."}

    def _post(self, conn, connection: dict, key: str, channel: str, text: str, blocks: list | None = None) -> dict:
        known = self.known(conn, connection, key)
        if known:
            return {"message_ts": known["object_id"], "channel": channel, "replayed": True}
        ref = kit.ref(key)
        payload: dict = {
            "channel": channel,
            "text": text,
            "unfurl_links": False,
            "metadata": {"event_type": "commai_message", "event_payload": {"ref": ref}},
        }
        if blocks:
            payload["blocks"] = blocks
        if self.dry(connection):
            return {"dry_run": True, "would_send": payload}
        hist = self.api(
            conn,
            connection,
            "GET",
            "conversations.history",
            params={"channel": channel, "limit": 20, "include_all_metadata": "true"},
        )
        for msg in hist.get("messages") or []:
            if ((msg.get("metadata") or {}).get("event_payload") or {}).get("ref") == ref:
                self.remember(conn, connection, key, "message", msg["ts"])
                return {"message_ts": msg["ts"], "channel": channel, "existing": True}
        out = self.api(conn, connection, "POST", "chat.postMessage", json_body=payload)
        self.remember(conn, connection, key, "message", out["ts"])
        return {"message_ts": out["ts"], "channel": out.get("channel", channel)}

    def _channel(self, connection: dict, inputs: dict, setting: str = "channel") -> str:
        s = self.settings(connection)
        ch = inputs.get("channel") or s.get(setting) or s.get("channel")
        if not ch:
            raise ConnectorError("Choose a Slack channel in the Slack settings.", "mapping")
        return ch

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        share = self.settings(connection).get("share") or "none"
        if action == "list_channels":
            items = self.paginate(
                conn,
                connection,
                "/api/conversations.list",
                params={"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200},
                items=lambda b: (b or {}).get("channels") or [],
                next_page=lambda r: (
                    {"cursor": c} if (c := ((r.body or {}).get("response_metadata") or {}).get("next_cursor")) else None
                ),
            )
            return {
                "channels": [
                    {"id": c["id"], "name": c.get("name", ""), "private": c.get("is_private", False)} for c in items
                ]
            }
        if action == "notify_staff":
            text = inputs["text"] if share == "summary" else mask(inputs["text"])
            if luhn_card(text):
                text = mask(text)
            return self._post(conn, connection, key, self._channel(connection, inputs), text)
        if action == "handover_alert":
            try:
                brief = conversation_brief(conn, connection["customer_id"], inputs["conversation_id"], share)
            except ValueError as e:
                raise ConnectorError(str(e), "input") from None
            head = "A conversation needs a person" + (" (urgent)" if brief["priority"] == "urgent" else "") + "."
            lines = [mask(x) for x in brief["lines"]]
            text = "\n".join([head, *lines, f"Open it in CommAI: {brief['link']}"])
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join([f"*{head}*", *lines])}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Open in CommAI"},
                            "url": brief["link"],
                            "action_id": "commai_open",
                        }
                    ],
                },
            ]
            return self._post(conn, connection, key, self._channel(connection, inputs), text, blocks)
        if action == "request_approval":
            try:
                brief = approval_brief(conn, connection["customer_id"], inputs["run_id"], share)
            except ValueError as e:
                raise ConnectorError(str(e), "input") from None
            self.team(conn, connection)  # learnt now, so the button press can be checked
            value = json.dumps({"c": str(connection["customer_id"]), "r": brief["run_id"]})
            head = f"Approval needed: {brief['what']}"
            lines = [mask(x) for x in brief["lines"]]
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join([f"*{head}*", *lines])}},
                {
                    "type": "actions",
                    "block_id": "commai_approval",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "action_id": "commai_approve",
                            "value": value,
                            "text": {"type": "plain_text", "text": "Approve"},
                        },
                        {
                            "type": "button",
                            "style": "danger",
                            "action_id": "commai_reject",
                            "value": value,
                            "text": {"type": "plain_text", "text": "Reject"},
                        },
                        {
                            "type": "button",
                            "action_id": "commai_open",
                            "url": brief["link"],
                            "text": {"type": "plain_text", "text": "Open in CommAI"},
                        },
                    ],
                },
            ]
            text = "\n".join([head, *lines, brief["link"]])
            return self._post(
                conn, connection, key, self._channel(connection, inputs, "approvals_channel"), text, blocks
            )
        raise ConnectorError(f"Unknown action {action}.", "input")


kit.register(Slack())
