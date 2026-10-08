"""A business's own mailbox over IMAP and SMTP (ADR 0034), with fake servers.

Off until ExaCarib switches integration-mailbox on (replies go to the simulated
outbox); then mail is read by UID once, threaded through the email channel,
and replies leave by SMTP with the channel's headers."""

from __future__ import annotations

import imaplib
import smtplib
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet

from exaconnect_controller import db
from exaconnect_controller.commai import golive
from exaconnect_controller.commai.channels import mailbox

from .commai_helpers import base, business, run_jobs


class FakeIMAP:
    """Enough of an IMAP4rev1 server: one folder, UIDs, UIDVALIDITY."""

    def __init__(self, password: str):
        self.password = password
        self.messages: dict[int, bytes] = {}
        self.uidvalidity = 7001
        self.logins = 0
        self.fetched: list[int] = []

    def add(self, uid: int, sender: str, subject: str, text: str, msgid: str, **headers) -> None:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, "help@examplebank.tt", subject, msgid
        for k, v in headers.items():
            m[k.replace("_", "-")] = v
        m.set_content(text)
        self.messages[uid] = m.as_bytes()

    def __call__(self, host, port, security):
        assert host == "imap.mail.example" and port == 993 and security == "ssl"
        return self

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.logins += 1
        return "OK", [b"Logged in"]

    def select(self, folder, readonly=False):
        assert folder == '"INBOX"' and readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        assert code == "UIDVALIDITY"
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            crit = args[1]
            uids = sorted(self.messages)
            if crit.startswith("UID "):
                lo = int(crit[4:].split(":")[0])
                # RFC 3501: "n:*" always includes the highest UID, even below n.
                uids = [u for u in uids if u >= lo] or uids[-1:]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            self.fetched.append(uid)
            return "OK", [(f"{uid} (UID {uid} BODY[] {{n}}".encode(), self.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        return "BYE", []


class FakeSMTP:
    def __init__(self, password: str):
        self.password = password
        self.sent: list[EmailMessage] = []

    def __call__(self, host, port, security):
        assert host == "smtp.mail.example" and port == 587 and security == "starttls"
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if password != self.password:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    def send_message(self, msg):
        self.sent.append(msg)

    def noop(self):
        return 250, b"OK"


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setenv("EXA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    pw = "app-pw-" + "z" * 10
    imap, smtp = FakeIMAP(pw), FakeSMTP(pw)
    monkeypatch.setattr(mailbox, "imap_factory", imap)
    monkeypatch.setattr(mailbox, "smtp_factory", smtp)
    return imap, smtp, pw


def _switch_on(customer_id: str) -> None:
    with db.tx() as conn:
        golive.sync(conn)
        for r in conn.execute(
            "SELECT criterion FROM commai_capability_criteria WHERE kind = 'feature' AND key = %s", (mailbox.FEATURE,)
        ).fetchall():
            golive.check(conn, "feature", mailbox.FEATURE, r["criterion"], True, "test run", "user:test")
        golive.set_status(conn, "feature", mailbox.FEATURE, "pilot", "user:test", [customer_id])


def _setup(client, b, pw):
    u, h = base(b), b["agent"]["h"]
    r = client.post(
        f"{u}/channel-accounts",
        json={"channel": "email", "provider": "mailbox", "address": "help@examplebank.tt", "name": "Help"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    acct = r.json()
    assert acct["status"] == "setup"
    cfg = {"imap_host": "imap.mail.example", "smtp_host": "smtp.mail.example", "username": "help@examplebank.tt"}
    r = client.put(f"{u}/channel-accounts/{acct['id']}/mailbox", json={**cfg, "password": pw}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["mailbox"]["password_set"] and "password" not in r.json()["mailbox"]
    assert not r.json()["live"]
    client.patch(f"{u}/channel-accounts/{acct['id']}", json={"status": "live"}, headers=h)
    return acct, cfg


def test_mailbox_reads_once_threads_and_replies_by_smtp(client, servers):
    imap, smtp, pw = servers
    b = business(client, "Mailbox Bank", people=("agent",))
    other = business(client, "Mailbox Other", people=("agent",))
    u, h = base(b), b["agent"]["h"]
    acct, cfg = _setup(client, b, pw)
    imap.add(1, "Old <old@example.org>", "History", "old mail", "<old@example.org>")

    # Off: nothing is read, and a poll by hand is refused.
    assert client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/poll", headers=h).status_code == 409
    run_jobs()
    assert imap.logins == 0

    _switch_on(b["id"])
    # The first read starts from now: history is not imported.
    r = client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/poll", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"new": 0, "stored": 0, "skipped": 0}

    imap.add(2, "Ana <ana@example.org>", "Lost card", "I lost my card", "<m2@example.org>")
    imap.add(
        3, "Mailer <mailer@example.org>", "Out of office", "Away", "<m3@example.org>", Auto_Submitted="auto-replied"
    )
    assert client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/poll", headers=h).json() == {
        "new": 2,
        "stored": 1,
        "skipped": 1,
    }
    # A re-read finds nothing new; the server's "n:*" quirk doesn't re-fetch.
    assert client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/poll", headers=h).json()["new"] == 0
    assert imap.fetched == [2, 3]
    # UIDVALIDITY changed: the folder was renumbered; start again from its newest message,
    # and even a re-delivered Message-ID is stored once.
    imap.uidvalidity += 1
    client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/poll", headers=h)
    imap.add(9, "Ana <ana@example.org>", "Lost card", "I lost my card", "<m2@example.org>")
    assert client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/poll", headers=h).json()["stored"] == 0

    with db.tx() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE customer_id = %s AND channel = 'email'", (b["id"],)
        ).fetchall()
        assert len(conv) == 1 and conv[0]["subject"] == "Lost card"
        assert (
            conn.execute("SELECT count(*) AS n FROM conversations WHERE customer_id = %s", (other["id"],)).fetchone()[
                "n"
            ]
            == 0
        )

    # The reply leaves by SMTP, threaded with the email channel's headers.
    client.post(f"{u}/conversations/{conv[0]['id']}/messages", json={"body": "Blocked it."}, headers=h)
    run_jobs()
    assert len(smtp.sent) == 1
    sent = smtp.sent[0]
    assert sent["In-Reply-To"] == "<m2@example.org>" and sent["Subject"] == "Re: Lost card"
    assert sent["To"] == "ana@example.org" and "List-Unsubscribe" in sent

    # The check signs in to both and reports; another business can't see the mailbox.
    assert client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/check", headers=h).json()["ok"]
    r = client.get(f"{base(other)}/channel-accounts/{acct['id']}/mailbox", headers=other["agent"]["h"])
    assert r.status_code == 404


def test_mailbox_off_sends_to_outbox_and_names_a_refused_sign_in(client, servers):
    imap, smtp, pw = servers
    b = business(client, "Mailbox Repair", people=("agent",))
    u, h = base(b), b["agent"]["h"]
    acct, cfg = _setup(client, b, pw)
    imap.add(1, "Ben <ben@example.org>", "Hello", "Hi", "<b1@example.org>")

    # Not live yet: the email channel's simulated outbox, nothing over SMTP.
    with db.tx() as conn:
        a = conn.execute("SELECT * FROM channel_accounts WHERE id = %s", (acct["id"],)).fetchone()
        from exaconnect_controller.commai.channels import email as email_ch

        email_ch.receive_email(conn, a, mailbox.parse(imap.messages[1]))
        conv = conn.execute("SELECT * FROM conversations WHERE customer_id = %s", (b["id"],)).fetchone()
    client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Hello Ben."}, headers=h)
    run_jobs()
    assert smtp.sent == []
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM sim_channel_outbox WHERE account_id = %s", (acct["id"],)
            ).fetchone()["n"]
            == 1
        )

    # Live, with the password changed at the provider: the check names an expired sign-in.
    _switch_on(b["id"])
    imap.password = smtp.password = "changed-" + "q" * 8
    r = client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/check", headers=h).json()
    assert not r["ok"] and r["cause"] == "expired_signin"
    r = client.post(f"{u}/channel-accounts/{acct['id']}/mailbox/poll", headers=h)
    assert r.status_code == 409 and "expired_signin" in r.text

    # Private or malformed servers are refused.
    bad_host = client.put(
        f"{u}/channel-accounts/{acct['id']}/mailbox", json={**cfg, "imap_host": "not a host"}, headers=h
    )
    assert bad_host.status_code == 422
    # A simulated email account has no mailbox.
    sim = client.post(
        f"{u}/channel-accounts",
        json={"channel": "email", "provider": "simulated", "address": "sim@examplebank.tt", "name": "Sim"},
        headers=h,
    ).json()
    assert (
        client.put(f"{u}/channel-accounts/{sim['id']}/mailbox", json={**cfg, "password": pw}, headers=h).status_code
        == 422
    )
