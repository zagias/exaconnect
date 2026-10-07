"""Webhooks: CloudEvents 1.0 over HTTP, signed with Standard Webhooks.

The same provider serves portal webhooks, REST hooks made by Zapier, Make
and n8n, and carriers' own endpoints. TMF688 listeners get the TM Forum
event envelope instead (provider ``tmf688``).
"""

from __future__ import annotations

import datetime as dt

from .. import cloudevents
from . import Context, Field, Outcome, Provider, raise_for, register


@register
class Webhook(Provider):
    key = "webhook"
    name = "Webhook (CloudEvents)"
    category = "webhook"
    docs = "https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/bindings/http-protocol-binding.md"
    api = "HTTP POST of a CloudEvent 1.0, structured or binary mode, Standard Webhooks signature"
    live_needs = "Your HTTPS address. Connect makes the signing secret and shows it once."
    fields = (
        Field("url", "Address", secret=True, required=True, kind="url", help="Your HTTPS endpoint."),
        Field(
            "mode",
            "CloudEvents mode",
            default="structured",
            kind="choice",
            choices=("structured", "binary"),
            help="Structured sends the whole event as JSON; binary sends the data with ce-* headers.",
        ),
        Field("signing_secret", "Signing secret", secret=True, help="Made for you if left empty (whsec_...)."),
    )

    def deliver(self, ctx: Context, event: dict) -> Outcome:
        ce = event
        if ctx.config.get("mode") == "binary":
            headers, body = cloudevents.binary(ce)
        else:
            headers, body = cloudevents.structured(ce)
        secret = ctx.secrets.get("signing_secret", "")
        if secret:
            headers = cloudevents.signed(secret, ce["id"], headers, body)
        resp = ctx.http.request("POST", ctx.secrets["url"], headers=headers, body=body)
        raise_for(resp, "The endpoint")
        return Outcome(resp.status)


@register
class Tmf688Listener(Provider):
    """A TMF688 hub subscription: events go to the listener's callback as TMF Event resources."""

    key = "tmf688"
    name = "TM Forum TMF688 listener"
    category = "webhook"
    docs = "https://www.tmforum.org/oda/open-apis/directory/event-management-api-TMF688/v4.0"
    api = "TMF688 Event Management v4: POST of an Event to the hub subscriber's callback"
    live_needs = "The listener's callback address, registered with POST /tmf-api/event/v4/hub."
    fields = (
        Field("url", "Callback", secret=True, required=True, kind="url"),
        Field("query", "Query", help="The hub query the subscriber gave, kept for reference."),
    )

    def deliver(self, ctx: Context, event: dict) -> Outcome:
        body = tmf_event(event)
        resp = ctx.http.request("POST", ctx.secrets["url"], json_body=body)
        raise_for(resp, "The TMF688 listener")
        return Outcome(resp.status)


TMF_PRIORITY = {"critical": "1", "warning": "3", "info": "5"}


def tmf_event(ce: dict) -> dict:
    """A CloudEvent as a TMF688 Event resource."""
    from . import kind_name, summary, title

    now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    return {
        "id": ce["id"],
        "eventId": ce["id"],
        "eventTime": now,
        "eventType": "ExaCaribConnect" + "".join(w.capitalize() for w in kind_name(ce).replace("_", ".").split(".")),
        "correlationId": ce.get("dedupkey", ""),
        "domain": "connectivity",
        "title": title(ce),
        "description": summary(ce),
        "priority": TMF_PRIORITY.get(ce.get("severity", "info"), "5"),
        "timeOcurred": ce["time"],
        "source": {"id": ce["source"], "name": "ExaCarib Connect", "@referredType": "System"},
        "event": ce.get("data") or {},
        "@type": "Event",
    }
