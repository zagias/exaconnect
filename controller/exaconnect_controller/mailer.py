"""Platform email transport, shared by account mail and SMTP channel accounts.

Credentials stay in the server environment. A successful send means the SMTP
server accepted the message; it does not prove delivery to the recipient's inbox.
Tenant channel accounts still choose their own From address.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from starlette.requests import Request

log = logging.getLogger("exaconnect.mail")


def missing() -> list[str]:
    return [name for name in ("EXA_SMTP_HOST",) if not os.environ.get(name)]


def send(msg: EmailMessage) -> None:
    """Send without persisting message bodies, reset links or invite tokens."""
    if missing():
        raise RuntimeError("Email is not configured: set EXA_SMTP_HOST.")
    host = os.environ["EXA_SMTP_HOST"]
    port = int(os.environ.get("EXA_SMTP_PORT", "587"))
    user = os.environ.get("EXA_SMTP_USER", "")
    starttls = os.environ.get("EXA_SMTP_STARTTLS", "true").lower() != "false"
    if user and not starttls:
        raise RuntimeError("Authenticated SMTP requires STARTTLS.")
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if starttls:
            smtp.starttls(context=ssl.create_default_context())
        if user:
            smtp.login(user, os.environ.get("EXA_SMTP_PASSWORD", ""))
        refused = smtp.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def message(to: str, subject: str, body: str) -> EmailMessage:
    """Build company account mail; Jibsy replies use the tenant account instead."""
    msg = EmailMessage()
    msg["From"] = os.environ.get("EXA_SMTP_FROM") or "XaKore <hello@xakore.com>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="xakore.com")
    msg.set_content(body)
    return msg


def public_base(request: Request) -> str:
    """Prefer the configured Hub address; keep HTTPS behind the private proxy.

    Caddy overwrites X-Forwarded-Proto while the controller stays on its private
    network. It can reach Uvicorn from a Docker peer rather than loopback, so
    request.base_url alone may still describe that internal HTTP connection.
    Do not accept a forwarded host or downgrade an already secure request.
    """
    configured = request.app.state.settings.public_url
    if configured:
        return configured.rstrip("/")
    base = request.base_url
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        base = base.replace(scheme="https")
    return str(base).rstrip("/")


def send_invitation(*, to: str, organisation: str, by: str, role: str, url: str) -> dict:
    """The invite already exists. A mail failure leaves its copyable link valid."""
    if missing():
        return {"emailed": False, "email_sender": "unavailable"}
    try:
        msg = message(
            to,
            f"You're invited to {organisation} on XaKore",
            f"{by} has invited you to join {organisation} on XaKore as {role}.\n\n"
            f"Accept your invitation here:\n{url}\n\n"
            "This link works once and expires in 7 days. If you weren't expecting "
            "this invitation, you can ignore this email.\n",
        )
        send(msg)
    except Exception as e:
        # Provider errors can include addresses, credentials or the message itself.
        log.warning("invitation email failed: %s", type(e).__name__)
        return {"emailed": False, "email_sender": "failed"}
    return {"emailed": True, "email_sender": "smtp"}
