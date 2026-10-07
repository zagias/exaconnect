"""A business's own mailbox over IMAP and SMTP (ADR 0034).

This extends the email channel (ADR 0018); it does not duplicate it. An email
channel account with provider "mailbox" keeps all of the channel's rules
(threading by Message-ID, unsubscribe, the 24-hour reply window). Only the
transport changes:

- Inbound: the mailbox is read over IMAP (RFC 9051 / 3501), by polling every
  poll_seconds (the `mailbox.poll` job) or with IDLE (RFC 2177) from
  `idle_loop`. New mail is found by UID: the mailbox's UIDVALIDITY and the
  last UID seen are kept, so nothing is read twice; if UIDVALIDITY changes the
  server renumbered the folder and we start again from its newest message.
  Each email goes through email.receive_email, which stores it once per
  Message-ID, so even a re-read never makes a duplicate.
- Outbound: SMTP submission (RFC 6409) with STARTTLS on 587 or TLS on 465.

The password is held in the vault (EXA_SECRETS_KEY), never shown again.

NOT LIVE until ExaCarib switches on the "integration-mailbox" feature after
its go-live checks. Until then (or before a mailbox is set up) replies go to
the simulated outbox and nothing is read from the server. Tested only with
fake IMAP and SMTP servers; `imap_factory` and `smtp_factory` are the seams.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
import ssl
import time
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import psycopg

from .. import golive, jobs
from ..automation import vault
from . import email as email_ch

FEATURE = "integration-mailbox"
SECURITY = ("ssl", "starttls")

golive.declare(
    "feature",
    FEATURE,
    "Business mailbox (IMAP and SMTP)",
    {
        "live-test": "A real mailbox (IMAP and SMTP) received, threaded and replied, with no duplicate on a re-read.",
        "providers": "Tested against Gmail (app password), Microsoft 365 and one hosting mailbox; quirks written down.",
        "tls": "Certificates are verified on both IMAP and SMTP; plain-text logins are refused.",
    },
    {"category": "email", "env": ["EXA_SECRETS_KEY"]},
)


class MailboxError(Exception):
    def __init__(self, message: str, cause: str = "provider"):
        super().__init__(message)
        self.cause = cause


def _imap(host: str, port: int, security: str) -> imaplib.IMAP4:  # pragma: no cover - network
    ctx = ssl.create_default_context()
    if security == "ssl":
        return imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=20)
    m = imaplib.IMAP4(host, port, timeout=20)
    m.starttls(ssl_context=ctx)
    return m


def _smtp(host: str, port: int, security: str) -> smtplib.SMTP:  # pragma: no cover - network
    ctx = ssl.create_default_context()
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
    s = smtplib.SMTP(host, port, timeout=20)
    s.starttls(context=ctx)
    return s


# The seams the tests replace.
imap_factory: Callable[[str, int, str], Any] = _imap
smtp_factory: Callable[[str, int, str], Any] = _smtp


# ---- configuration ---------------------------------------------------------------------


def get(conn: psycopg.Connection, customer_id: Any, account_id: Any) -> dict | None:
    return conn.execute(
        "SELECT * FROM commai_mailboxes WHERE account_id = %s AND customer_id = %s", (account_id, customer_id)
    ).fetchone()


def out(row: dict | None) -> dict | None:
    if row is None:
        return None
    keep = (
        "account_id imap_host imap_port imap_security smtp_host smtp_port smtp_security username folder mode"
        " poll_seconds last_uid last_poll_at last_ok_at last_error"
    ).split()
    return {k: row[k] for k in keep} | {"password_set": bool(row["secret_ref"])}


HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")


def _check_host(host: str) -> str:
    from .. import webhooks

    host = host.strip().lower()
    if not HOST.match(host):
        raise MailboxError(f"{host or 'That'} is not a mail server name.", "input")
    try:
        webhooks.check_url(f"https://{host}/")  # public addresses only, as for webhooks
    except webhooks.UnsafeURL as e:
        raise MailboxError(str(e), "input") from None
    return host


def configure(conn: psycopg.Connection, account: dict, cfg: dict, password: str | None, actor: str) -> dict:
    """Set (or change) the mailbox behind an email account whose provider is "mailbox"."""
    if account["channel"] != "email" or account["provider"] != "mailbox":
        raise MailboxError("Only an email account set up with the mailbox provider has a mailbox.", "input")
    for k in ("imap_security", "smtp_security"):
        if cfg.get(k, "ssl" if k == "imap_security" else "starttls") not in SECURITY:
            raise MailboxError(f"{k} must be ssl or starttls.", "input")
    if cfg.get("mode", "poll") not in ("poll", "idle"):
        raise MailboxError("mode must be poll or idle.", "input")
    row = get(conn, account["customer_id"], account["id"])
    ref = row["secret_ref"] if row else ""
    if password:
        ref = vault.put(conn, account["customer_id"], f"mailbox:{account['id']}", {"password": password}, ref)
    if not ref:
        raise MailboxError("Enter the mailbox password (or app password).", "input")
    vals = {
        "imap_host": _check_host(cfg["imap_host"]),
        "imap_port": int(cfg.get("imap_port") or 993),
        "imap_security": cfg.get("imap_security", "ssl"),
        "smtp_host": _check_host(cfg["smtp_host"]),
        "smtp_port": int(cfg.get("smtp_port") or 587),
        "smtp_security": cfg.get("smtp_security", "starttls"),
        "username": str(cfg["username"]).strip(),
        "folder": str(cfg.get("folder") or "INBOX"),
        "mode": cfg.get("mode", "poll"),
        "poll_seconds": max(30, min(int(cfg.get("poll_seconds") or 60), 3600)),
    }
    if not vals["username"]:
        raise MailboxError("Give the mailbox user name.", "input")
    cols = list(vals)
    row = conn.execute(
        f"""INSERT INTO commai_mailboxes (account_id, customer_id, secret_ref, created_by, {", ".join(cols)})
            VALUES (%s, %s, %s, %s, {", ".join(["%s"] * len(cols))})
            ON CONFLICT (account_id) DO UPDATE SET secret_ref = EXCLUDED.secret_ref, updated_at = now(),
              {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)},
              uidvalidity = CASE WHEN commai_mailboxes.imap_host = EXCLUDED.imap_host
                                  AND commai_mailboxes.folder = EXCLUDED.folder
                                 THEN commai_mailboxes.uidvalidity END
            RETURNING *""",
        (account["id"], account["customer_id"], ref, actor, *vals.values()),
    ).fetchone()
    schedule(conn, row)
    return row


def _password(conn, row: dict) -> str:
    secret = vault.get(conn, row["customer_id"], row["secret_ref"]) or {}
    if not secret.get("password"):
        raise MailboxError("The mailbox password is missing; enter it again.", "expired_signin")
    return secret["password"]


def live(conn: psycopg.Connection, customer_id: Any) -> bool:
    return golive.enabled(conn, "feature", FEATURE, customer_id)


# ---- sending ----------------------------------------------------------------------------


class MailboxSender(email_ch.Sender):
    """SMTP through the business's own mailbox; the simulated outbox until it is live."""

    name = "mailbox"

    def send(self, conn, account, msg: EmailMessage) -> None:
        row = get(conn, account["customer_id"], account["id"])
        if row is None or not live(conn, account["customer_id"]):
            email_ch.SENDERS["simulated"].send(conn, account, msg)
            return
        password = _password(conn, row)
        try:
            with smtp_factory(row["smtp_host"], row["smtp_port"], row["smtp_security"]) as s:
                s.login(row["username"], password)
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise MailboxError("The mailbox refused the sign-in (SMTP).", "expired_signin") from None
        except smtplib.SMTPRecipientsRefused:
            raise MailboxError("The mail server refused the address.", "input") from None


email_ch.SENDERS["mailbox"] = MailboxSender()


# ---- reading ----------------------------------------------------------------------------


def _text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", text))
        text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def parse(raw: bytes) -> dict:
    """One RFC 5322 message as the email channel's inbound shape."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    return {
        "from": addr,
        "name": name,
        "subject": str(msg.get("Subject", "")),
        "text": _text(msg),
        "message_id": str(msg.get("Message-ID", "")),
        "in_reply_to": str(msg.get("In-Reply-To", "")),
        "references": str(msg.get("References", "")),
        "auto": str(msg.get("Auto-Submitted", "no")).lower() != "no",
    }


def _ok(typ: str, what: str) -> None:
    if typ != "OK":
        raise MailboxError(f"The mail server refused {what}.")


def _uids(data: list) -> list[int]:
    return [int(x) for x in (data[0] or b"").split()] if data else []


def _raw(data: list) -> bytes:
    for item in data or []:
        if isinstance(item, tuple) and len(item) > 1:
            return item[1]
    return b""


def poll(conn: psycopg.Connection, row: dict, max_messages: int = 200) -> dict:
    """Read new mail once. Returns {"new", "stored", "skipped"}."""
    account = conn.execute("SELECT * FROM channel_accounts WHERE id = %s", (row["account_id"],)).fetchone()
    password = _password(conn, row)
    stored = skipped = 0
    try:
        m = imap_factory(row["imap_host"], row["imap_port"], row["imap_security"])
    except (OSError, imaplib.IMAP4.error) as e:
        raise MailboxError(f"Could not reach {row['imap_host']}: {e}") from None
    try:
        try:
            m.login(row["username"], password)
        except imaplib.IMAP4.error:
            raise MailboxError("The mailbox refused the sign-in (IMAP).", "expired_signin") from None
        typ, _ = m.select(f'"{row["folder"]}"', readonly=True)
        _ok(typ, f"the folder {row['folder']}")
        uv = int((m.response("UIDVALIDITY")[1] or [b"0"])[0] or 0)
        last = row["last_uid"] if row["uidvalidity"] == uv else None
        typ, data = m.uid("SEARCH", None, "ALL" if last is None else f"UID {last + 1}:*")
        _ok(typ, "the search")
        uids = _uids(data)
        if last is None:
            # First read, or the folder was renumbered: start from now, not from history.
            last = max(uids, default=0)
            new = []
        else:
            new = sorted(u for u in uids if u > last)[:max_messages]
        for uid in new:
            typ, data = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
            _ok(typ, "a message")
            mail = parse(_raw(data))
            if mail["auto"] or not mail["from"]:
                skipped += 1  # out-of-office replies and bounces never start conversations
            else:
                got = email_ch.receive_email(conn, account, mail)
                stored += 0 if got["duplicate"] else 1
            last = uid
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001 - the server may already have gone
            pass
    conn.execute(
        """UPDATE commai_mailboxes SET uidvalidity = %s, last_uid = %s, last_poll_at = now(), last_ok_at = now(),
                  last_error = '', updated_at = now() WHERE account_id = %s""",
        (uv, last, row["account_id"]),
    )
    return {"new": len(new), "stored": stored, "skipped": skipped}


def check(conn: psycopg.Connection, row: dict) -> dict:
    """Sign in to IMAP and SMTP without reading or sending anything."""
    out_ = {"imap": "", "smtp": "", "ok": False, "cause": ""}
    password = _password(conn, row)
    try:
        m = imap_factory(row["imap_host"], row["imap_port"], row["imap_security"])
        m.login(row["username"], password)
        typ, _ = m.select(f'"{row["folder"]}"', readonly=True)
        out_["imap"] = "ok" if typ == "OK" else f"folder {row['folder']} not found"
        m.logout()
    except imaplib.IMAP4.error:
        out_["imap"], out_["cause"] = "sign-in refused", "expired_signin"
    except OSError as e:
        out_["imap"], out_["cause"] = f"unreachable: {e}", "provider"
    try:
        with smtp_factory(row["smtp_host"], row["smtp_port"], row["smtp_security"]) as s:
            s.login(row["username"], password)
            s.noop()
        out_["smtp"] = "ok"
    except smtplib.SMTPAuthenticationError:
        out_["smtp"], out_["cause"] = "sign-in refused", out_["cause"] or "expired_signin"
    except (OSError, smtplib.SMTPException) as e:
        out_["smtp"], out_["cause"] = f"unreachable: {e}", out_["cause"] or "provider"
    out_["ok"] = out_["imap"] == "ok" and out_["smtp"] == "ok"
    return out_


# ---- the polling job and the IDLE loop ---------------------------------------------------


def schedule(conn: psycopg.Connection, row: dict, delay_s: float = 0) -> None:
    slot = int((time.time() + delay_s) // max(row["poll_seconds"], 1))
    jobs.enqueue(
        conn,
        "mailbox.poll",
        {"account_id": str(row["account_id"])},
        customer_id=row["customer_id"],
        dedupe_key=f"mailbox.poll:{row['account_id']}:{slot}",
        delay_s=delay_s,
        max_attempts=1,
    )


@jobs.handler("mailbox.poll")
def _poll_job(conn: psycopg.Connection, job: dict) -> None:
    row = conn.execute(
        "SELECT * FROM commai_mailboxes WHERE account_id = %s", (job["payload"]["account_id"],)
    ).fetchone()
    if row is None:
        return  # the account was removed
    acct = conn.execute("SELECT status FROM channel_accounts WHERE id = %s", (row["account_id"],)).fetchone()
    if acct and acct["status"] == "live" and live(conn, row["customer_id"]) and row["mode"] == "poll":
        try:
            poll(conn, row)
        except MailboxError as e:
            conn.execute(
                "UPDATE commai_mailboxes SET last_error = %s, last_poll_at = now() WHERE account_id = %s",
                (f"{e} ({e.cause})", row["account_id"]),
            )
    schedule(conn, row, row["poll_seconds"])


def idle_loop(account_id: str, stop: Callable[[], bool] = lambda: False, wait_s: int = 25 * 60) -> None:
    """Run with `python -m exaconnect_controller.commai.channels.mailbox <account_id>` for
    a mailbox in idle mode: wait in IMAP IDLE, read new mail as soon as it lands, and
    renew IDLE before the 29-minute limit. Falls back to polling if IDLE is missing."""
    from ... import db

    while not stop():
        with db.tx() as conn:
            row = conn.execute("SELECT * FROM commai_mailboxes WHERE account_id = %s", (account_id,)).fetchone()
            if row is None or not live(conn, row["customer_id"]):
                return
            poll(conn, row)
            password = _password(conn, row)
        m = imap_factory(row["imap_host"], row["imap_port"], row["imap_security"])
        try:
            m.login(row["username"], password)
            m.select(f'"{row["folder"]}"', readonly=True)
            if hasattr(m, "idle") and "IDLE" in [str(c).upper() for c in getattr(m, "capabilities", ())]:
                with m.idle(duration=wait_s) as responses:  # Python 3.14+
                    for _ in responses:
                        break
            else:
                time.sleep(row["poll_seconds"])
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":  # pragma: no cover
    import sys

    idle_loop(sys.argv[1])
