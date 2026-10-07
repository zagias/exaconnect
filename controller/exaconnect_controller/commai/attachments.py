"""Files staff attach to replies, on every channel (ADR 0038).

Staff upload a file for a conversation (POST .../conversations/{id}/files),
then name it on the reply (`attachments: [file id]`). The file is checked
twice: its contents against its type (the same magic-number check as website
chat uploads) and against what the conversation's channel can carry:

    web, api   images (PNG, JPEG, GIF, WebP), PDF, plain text; 2 MB
    email      the same; 2 MB
    whatsapp   PNG, JPEG, PDF, plain text; 2 MB (WhatsApp sends GIF and WebP
               differently, so they are refused rather than sent wrongly)
    sms        PNG, JPEG, GIF; 500 KB (MMS; most carriers outside North
               America deliver a link instead, so keep them small)
    voice      none

Delivery: website chat and the API show the file in the conversation (the
widget fetches it with the visitor's session); email attaches it; WhatsApp and
SMS send it through the provider as media. The provider fetches it from a
link signed with the channel account's secret that expires after a day
(public_media_url); the simulated provider records it in its outbox.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import time
from typing import Any

import psycopg

from .channels import widget

DEFAULT_TYPES = tuple(widget.FILE_TYPES)
RULES: dict[str, tuple[tuple[str, ...], int]] = {
    "web": (DEFAULT_TYPES, widget.MAX_FILE_BYTES),
    "api": (DEFAULT_TYPES, widget.MAX_FILE_BYTES),
    "email": (DEFAULT_TYPES, widget.MAX_FILE_BYTES),
    "whatsapp": (("image/png", "image/jpeg", "application/pdf", "text/plain"), widget.MAX_FILE_BYTES),
    "sms": (("image/png", "image/jpeg", "image/gif"), 500 * 1024),
    "voice": ((), 0),
}
MAX_PER_MESSAGE = 5
MEDIA_TTL_S = 24 * 3600
LABELS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WebP",
    "application/pdf": "PDF",
    "text/plain": "plain text",
}


class AttachmentError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


def rules_for(channel: str) -> tuple[tuple[str, ...], int]:
    """What a channel can carry. Channels added later (Messenger, Telegram...)
    get the WhatsApp rules until they say otherwise."""
    return RULES.get(channel, RULES["whatsapp"])


def check_for_channel(channel: str, content_type: str, size: int) -> None:
    types, max_bytes = rules_for(channel)
    if not types:
        raise AttachmentError("Files can't be sent on this channel.")
    if content_type not in types:
        allowed = ", ".join(LABELS.get(t, t) for t in types)
        raise AttachmentError(
            f"This channel can carry {allowed} files, not {LABELS.get(content_type, content_type)}.", 415
        )
    if size > max_bytes:
        kb = max_bytes // 1024
        limit = f"{kb // 1024} MB" if kb >= 1024 else f"{kb} KB"
        raise AttachmentError(f"Files on this channel can be up to {limit}.", 413)


def upload(conn: psycopg.Connection, conv: dict, user_id: Any, name: str, content_type: str, data_b64: str) -> dict:
    """Store a file a member of staff will attach to a reply on this conversation."""
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise AttachmentError("The file couldn't be read.", 400) from e
    try:
        widget.check_file(name, content_type, data)
    except widget.WidgetError as e:
        raise AttachmentError(str(e), e.code) from e
    check_for_channel(conv["channel"], content_type, len(data))
    pending = conn.execute(
        "SELECT count(*) AS n FROM channel_files WHERE customer_id = %s AND owner = %s AND conversation_id IS NULL",
        (conv["customer_id"], owner(user_id)),
    ).fetchone()["n"]
    if pending >= 20:
        raise AttachmentError("Send the files you've added before adding more.", 429)
    return conn.execute(
        """INSERT INTO channel_files (customer_id, owner, name, content_type, size, data)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, name, content_type AS type, size, created_at""",
        (conv["customer_id"], owner(user_id), name, content_type, len(data), data),
    ).fetchone()


def owner(user_id: Any) -> str:
    return f"staff:{user_id}"


def claim(conn: psycopg.Connection, conv: dict, user_id: Any, file_ids: list[str]) -> list[dict]:
    """The staff member's pending files, checked for this conversation's channel.
    Returns the message's attachment list ({"id", "name", "type", "size"})."""
    if not file_ids:
        return []
    if len(set(file_ids)) > MAX_PER_MESSAGE:
        raise AttachmentError(f"Attach up to {MAX_PER_MESSAGE} files to one reply.")
    files = conn.execute(
        """SELECT id, name, content_type, size FROM channel_files WHERE id = ANY(%s::uuid[])
           AND customer_id = %s AND owner = %s AND conversation_id IS NULL""",
        (list(set(file_ids)), conv["customer_id"], owner(user_id)),
    ).fetchall()
    if len(files) != len(set(file_ids)):
        raise AttachmentError("One of the files wasn't found. Upload it again.", 404)
    for f in files:
        check_for_channel(conv["channel"], f["content_type"], f["size"])
    return [{"id": str(f["id"]), "name": f["name"], "type": f["content_type"], "size": f["size"]} for f in files]


def attach(conn: psycopg.Connection, conv: dict, attachments: list[dict]) -> None:
    """Link the sent files to the conversation, so its people (and the visitor) can open them."""
    if attachments:
        conn.execute(
            "UPDATE channel_files SET conversation_id = %s WHERE id = ANY(%s::uuid[]) AND customer_id = %s",
            (conv["id"], [a["id"] for a in attachments], conv["customer_id"]),
        )


def files_of(conn: psycopg.Connection, message: dict) -> list[dict]:
    """The stored files of a message's attachments (with their bytes)."""
    ids = [a["id"] for a in (message.get("attachments") or []) if isinstance(a, dict) and a.get("id")]
    if not ids:
        return []
    return conn.execute(
        "SELECT * FROM channel_files WHERE id = ANY(%s::uuid[]) AND customer_id = %s ORDER BY created_at",
        (ids, message["customer_id"]),
    ).fetchall()


# ---- signed links for providers ---------------------------------------------------------


def _sig(account: dict, file_id: str, exp: int) -> str:
    mac = hmac.new(account["hook_secret"].encode(), f"media:{file_id}:{exp}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def media_path(account: dict, file_id: Any, now: float | None = None) -> str:
    exp = int((now or time.time()) + MEDIA_TTL_S)
    return f"/api/v1/commai/channels/media/{account['hook_token']}/{file_id}/{exp}/{_sig(account, str(file_id), exp)}"


def public_media_url(account: dict, file_id: Any) -> str:
    base = os.environ.get("EXA_PUBLIC_URL", "").rstrip("/")
    if not base:
        from .channels.providers import ProviderError

        raise ProviderError("Set EXA_PUBLIC_URL so the provider can fetch the file.")
    return base + media_path(account, file_id)


def check_media_link(account: dict, file_id: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(sig, _sig(account, file_id, exp))
