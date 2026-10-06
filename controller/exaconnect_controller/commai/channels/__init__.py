"""Channels: every way a conversation reaches the business (ADR 0016).

A channel turns its provider's format into CommAI messages and back, and
enforces its own rules before anything is sent. Modules register their
channel with `register()`; the inbox only ever talks to this interface.
"""

from __future__ import annotations

from typing import Any

import psycopg


class SendBlocked(Exception):
    """The channel's rules forbid this send (outside a service window, opted out...).
    The message says why, in words a member of staff understands."""


class Channel:
    name = ""
    label = ""
    # True when a reply needs the provider (it can fail and be retried); False
    # when the customer collects it (website chat polling).
    external = False

    def check_send(self, conn: psycopg.Connection, conversation: dict, body: str, template: str) -> None:
        """Raise SendBlocked if this reply may not go out now."""

    def deliver(self, conn: psycopg.Connection, conversation: dict, message: dict) -> dict:
        """Send one message. Return {"status": "sent" | "delivered", "provider_ref": "..."}.
        Called from a job, possibly more than once for the same message: use the
        message id as the provider's idempotency key where it has one."""
        return {"status": "sent", "provider_ref": ""}


class LocalChannel(Channel):
    """Website chat, the API and browser calls: the reply is stored and the
    widget, the API client or the call page collects it."""

    def __init__(self, name: str, label: str):
        self.name = name
        self.label = label


_channels: dict[str, Channel] = {
    "web": LocalChannel("web", "Website chat"),
    "api": LocalChannel("api", "API"),
    "voice": LocalChannel("voice", "Browser call"),
}


def register(channel: Channel) -> None:
    _channels[channel.name] = channel


def get(name: str) -> Channel:
    ch = _channels.get(name)
    if ch is None:
        raise SendBlocked(f"The {name} channel is not available.")
    return ch


def names() -> list[str]:
    return sorted(_channels)


def label(name: str) -> str:
    ch = _channels.get(name)
    return ch.label if ch else name


def any_channel(name: Any) -> bool:
    return name in _channels
