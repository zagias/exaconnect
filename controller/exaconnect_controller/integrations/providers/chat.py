"""Chat: Slack and Microsoft Teams."""

from __future__ import annotations

from ..transport import Http, sim_ok
from . import Context, Field, Outcome, Provider, ProviderError, facts, link, raise_for, register, summary, title

ICON = {"critical": ":red_circle:", "warning": ":large_orange_circle:", "info": ":large_blue_circle:"}


def slack_blocks(ev: dict) -> list[dict]:
    sev = ev.get("severity", "info")
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title(ev)[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{ICON.get(sev, '')} {summary(ev)}"[:3000]}},
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"[:2000]} for k, v in facts(ev)[2:12]],
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"ExaCarib Connect · {sev} · {ev['id']}"}]},
    ]
    if not blocks[2]["fields"]:
        blocks.pop(2)
    url = link(ev)
    if url:
        blocks.append(
            {
                "type": "actions",
                "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open in Connect"}, "url": url}],
            }
        )
    return blocks


@register
class Slack(Provider):
    key = "slack"
    name = "Slack"
    category = "chat"
    docs = "https://api.slack.com/messaging/webhooks"
    api = "Incoming webhook, or chat.postMessage with a bot token (Block Kit)"
    live_needs = "An incoming webhook URL from a Slack app, or a bot token (chat:write) and a channel ID."
    fields = (
        Field(
            "mode",
            "How",
            default="webhook",
            kind="choice",
            choices=("webhook", "api"),
            help="Incoming webhook, or the Web API with a bot token.",
        ),
        Field("webhook_url", "Incoming webhook URL", secret=True, kind="url"),
        Field("bot_token", "Bot token (xoxb-...)", secret=True),
        Field("channel", "Channel ID", help="For the Web API, e.g. C0123456789."),
        Field("base_url", "Web API address", default="https://slack.com/api", kind="url"),
    )

    def credentials_present(self, config: dict, secrets: dict) -> bool:
        if config.get("mode") == "api":
            return bool(secrets.get("bot_token") and config.get("channel"))
        return bool(secrets.get("webhook_url"))

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        text = f"{title(ev)}: {summary(ev)}"
        blocks = slack_blocks(ev)
        if ctx.config.get("mode") == "api":
            resp = ctx.http.request(
                "POST",
                f"{ctx.config.get('base_url', 'https://slack.com/api')}/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {ctx.secrets.get('bot_token', '')}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json_body={"channel": ctx.config.get("channel", ""), "text": text, "blocks": blocks},
            )
            raise_for(resp, "Slack")
            body = resp.json()
            if not body.get("ok"):
                err = str(body.get("error") or "unknown error")
                raise ProviderError(f"Slack said {err}.", resp.status, retry=err in ("ratelimited", "internal_error"))
            return Outcome(resp.status, {"ts": body.get("ts", "")})
        resp = ctx.http.request("POST", ctx.secrets.get("webhook_url", ""), json_body={"text": text, "blocks": blocks})
        raise_for(resp, "Slack")
        return Outcome(resp.status)

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        if url.endswith("/chat.postMessage"):
            return sim_ok(200, {"ok": True, "channel": "C0SIMULATED", "ts": "1700000000.000100"})
        return Http(200, "ok")


COLOUR = {"critical": "attention", "warning": "warning", "info": "accent"}


def adaptive_card(ev: dict) -> dict:
    sev = ev.get("severity", "info")
    card: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": title(ev),
                "weight": "Bolder",
                "size": "Medium",
                "color": COLOUR.get(sev, "default"),
                "wrap": True,
            },
            {"type": "TextBlock", "text": summary(ev), "wrap": True},
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts(ev)]},
        ],
    }
    url = link(ev)
    if url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in Connect", "url": url}]
    return card


@register
class Teams(Provider):
    key = "teams"
    name = "Microsoft Teams"
    category = "chat"
    docs = "https://learn.microsoft.com/en-us/connectors/teams/?tabs=text1#microsoft-teams-webhook"
    api = "Workflows (Power Automate) webhook: 'When a Teams webhook request is received', with an Adaptive Card"
    live_needs = "The HTTP POST URL of a Teams workflow made from the 'Post to a channel when a webhook request is "
    "received' template."
    fields = (Field("workflow_url", "Workflow webhook URL", secret=True, required=True, kind="url"),)

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        body = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": adaptive_card(ev),
                }
            ],
        }
        resp = ctx.http.request("POST", ctx.secrets["workflow_url"], json_body=body)
        raise_for(resp, "Teams")
        return Outcome(resp.status)
