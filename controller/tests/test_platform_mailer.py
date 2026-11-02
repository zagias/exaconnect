"""Real SMTP transport boundaries, with an in-process transport double only."""

import smtplib
import ssl
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from exaconnect_controller import mailer
from exaconnect_controller.api import password_reset
from exaconnect_controller.commai.channels.email import SmtpSender


@pytest.fixture(autouse=True)
def clean_mail_environment(monkeypatch):
    for name in ("HOST", "PORT", "USER", "PASSWORD", "STARTTLS", "FROM"):
        monkeypatch.delenv(f"EXA_SMTP_{name}", raising=False)


def smtp(monkeypatch):
    calls = []

    class Transport:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append(("close",))

        def starttls(self, *, context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            calls.append(("verified_tls",))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, msg):
            calls.append(("send", msg))
            return {}

    monkeypatch.setattr(mailer.smtplib, "SMTP", Transport)
    monkeypatch.setenv("EXA_SMTP_HOST", "smtp.example.net")
    monkeypatch.setenv("EXA_SMTP_USER", "hello@xakore.com")
    monkeypatch.setenv("EXA_SMTP_PASSWORD", "test-mail-password")
    return calls, Transport


def test_password_reset_uses_verified_tls_and_company_sender(monkeypatch):
    calls, _ = smtp(monkeypatch)
    password_reset._send("member@example.net", "https://hub.xakore.com/reset/private-token")
    assert [c[0] for c in calls] == ["connect", "verified_tls", "login", "send", "close"]
    assert calls[0] == ("connect", "smtp.example.net", 587, 15)
    msg = calls[3][1]
    assert str(msg["From"]) == "XaKore <hello@xakore.com>"
    assert msg["To"] == "member@example.net" and msg["Subject"] == "Reset your XaKore password"
    assert msg["Message-ID"].endswith("@xakore.com>")
    assert "https://hub.xakore.com/reset/private-token" in msg.get_content()
    assert "ExaCarib" not in str(msg)


def test_platform_from_override_does_not_replace_tenant_channel_sender(monkeypatch):
    calls, _ = smtp(monkeypatch)
    monkeypatch.setenv("EXA_SMTP_FROM", "XaKore Accounts <hello@xakore.com>")
    assert str(mailer.message("one@example.net", "Account", "Body")["From"]) == ("XaKore Accounts <hello@xakore.com>")
    msg = mailer.message("one@example.net", "Reply", "Body")
    msg.replace_header("From", "Customer Support <support@customer.example>")
    SmtpSender().send(None, {}, msg)
    assert str(next(c[1] for c in calls if c[0] == "send")["From"]) == ("Customer Support <support@customer.example>")


def test_missing_transport_does_not_send_or_claim_invitation_delivery(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("No SMTP connection is allowed without configuration")

    monkeypatch.setattr(mailer.smtplib, "SMTP", unexpected)
    assert SmtpSender.missing() == ["EXA_SMTP_HOST"]
    result = mailer.send_invitation(
        to="member@example.net",
        organisation="Example",
        by="owner@example.net",
        role="member",
        url="https://hub/invite/t",
    )
    assert result == {"emailed": False, "email_sender": "unavailable"}


def test_authenticated_smtp_cannot_downgrade_to_plaintext(monkeypatch):
    calls, _ = smtp(monkeypatch)
    monkeypatch.setenv("EXA_SMTP_STARTTLS", "false")
    with pytest.raises(RuntimeError, match="requires STARTTLS"):
        mailer.send(mailer.message("one@example.net", "Account", "Body"))
    assert calls == []


def test_certificate_failure_stops_before_login_or_send(monkeypatch):
    calls, transport = smtp(monkeypatch)

    def refuse(self, *, context):
        raise ssl.SSLCertVerificationError("certificate rejected")

    monkeypatch.setattr(transport, "starttls", refuse)
    with pytest.raises(ssl.SSLCertVerificationError):
        mailer.send(mailer.message("one@example.net", "Account", "Body"))
    assert [c[0] for c in calls] == ["connect", "close"]


@pytest.mark.parametrize("failure", [smtplib.SMTPAuthenticationError(535, b"refused"), OSError("timeout")])
def test_invitation_failure_keeps_link_fallback_and_logs_no_message(monkeypatch, caplog, failure):
    smtp(monkeypatch)

    def refused(msg):
        raise failure

    monkeypatch.setattr(mailer, "send", refused)
    url = "https://hub.xakore.com/invite/private-invitation-token"
    result = mailer.send_invitation(
        to="member@example.net", organisation="Example", by="owner@example.net", role="viewer", url=url
    )
    assert result == {"emailed": False, "email_sender": "failed"}
    assert url not in caplog.text and "member@example.net" not in caplog.text


def test_recipient_refusal_cannot_report_sent(monkeypatch):
    calls, transport = smtp(monkeypatch)
    monkeypatch.setattr(transport, "send_message", lambda self, msg: {"member@example.net": (550, b"refused")})
    result = mailer.send_invitation(
        to="member@example.net",
        organisation="Example",
        by="owner@example.net",
        role="viewer",
        url="https://hub/invite/t",
    )
    assert result == {"emailed": False, "email_sender": "failed"}
    assert [c[0] for c in calls] == ["connect", "verified_tls", "login", "close"]


@pytest.mark.parametrize(
    "configured,scheme,forwarded,expected",
    [
        ("", "http", "https", "https://hub.xakore.com"),
        ("", "https", "http", "https://hub.xakore.com"),
        ("", "http", "", "http://hub.xakore.com"),
        ("https://configured.example/", "http", "https", "https://configured.example"),
    ],
)
def test_account_links_keep_external_https_behind_private_proxy(configured, scheme, forwarded, expected):
    request = Request(
        {
            "type": "http",
            "scheme": scheme,
            "method": "POST",
            "path": "/api/v1/auth/password-reset",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"hub.xakore.com"), (b"x-forwarded-proto", forwarded.encode())],
            "server": ("controller-api", 8000),
            "app": SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(public_url=configured))),
        }
    )
    assert mailer.public_base(request) == expected
