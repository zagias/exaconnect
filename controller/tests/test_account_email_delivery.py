"""Both account invitation paths send only committed, usable links.

Requires the isolated test database. SMTP is replaced in every test, so these
checks cannot contact a provider or send email.
"""

import re
from dataclasses import replace

import pytest

from exaconnect_controller import db, mailer
from exaconnect_controller.security import token_hash

from .conftest import ADMIN

API = "/api/v1"


@pytest.fixture(autouse=True)
def no_external_mail(monkeypatch):
    for name in ("HOST", "PORT", "USER", "PASSWORD", "STARTTLS", "FROM"):
        monkeypatch.delenv(f"EXA_SMTP_{name}", raising=False)

    def unexpected(msg):
        pytest.fail("Test must replace SMTP before enabling company mail")

    monkeypatch.setattr(mailer, "send", unexpected)


@pytest.mark.parametrize("flow", ["new_organisation", "organisation_member"])
@pytest.mark.parametrize("outcome", ["accepted", "failed", "unavailable"])
@pytest.mark.parametrize("public_origin", ["configured", "forwarded_https"])
def test_invitation_delivery_retains_one_use_link_and_honest_status(
    client, admin_headers, settings, monkeypatch, caplog, flow, outcome, public_origin
):
    client.app.state.settings = replace(
        settings, public_url="https://hub.xakore.com" if public_origin == "configured" else ""
    )
    headers = {**admin_headers, "Host": "hub.xakore.com", "X-Forwarded-Proto": "https"}
    if flow == "organisation_member":
        created = client.post(
            f"{API}/admin/organisations",
            json={"name": "Coral", "products": ["connect"], "owner_email": "owner@coral.example"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
    sent = []

    def send(msg):
        text = msg.get_content()
        url = re.search(r"https://hub\.xakore\.com/invite/[\w-]+", text).group()
        token = url.rsplit("/", 1)[1]
        # A separate transaction must see the invitation before SMTP accepts it.
        # Sending inside the creation transaction would leave an unusable link
        # if commit fails, and would keep its DB locks during a network request.
        with db.tx() as conn:
            row = conn.execute(
                "SELECT customer_id, token_hash FROM org_invites WHERE email = %s", (str(msg["To"]),)
            ).fetchone()
        assert row is not None and row["token_hash"] == token_hash(token)
        sent.append(msg)
        if outcome == "failed":
            raise OSError(f"provider error contained private message: {msg}")

    monkeypatch.setattr(mailer, "send", send)
    if outcome != "unavailable":
        monkeypatch.setenv("EXA_SMTP_HOST", "smtp.example.net")
    if flow == "new_organisation":
        response = client.post(
            f"{API}/admin/organisations",
            json={"name": "Reef", "products": ["connect", "commai"], "owner_email": "new@reef.example"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        invite, cid = body["invite"], body["id"]
    else:
        response = client.post(
            f"{API}/orgs/{cid}/invites",
            json={"email": "new@reef.example", "role": "viewer"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        invite = response.json()
    assert invite["emailed"] is (outcome == "accepted")
    assert invite["email_sender"] == {"accepted": "smtp", "failed": "failed", "unavailable": "unavailable"}[outcome]
    token = invite["url"].rsplit("/", 1)[1]
    public_url = f"https://hub.xakore.com/invite/{token}"
    assert invite["url"] == (public_url if public_origin == "configured" else f"/invite/{token}")
    assert len(sent) == (0 if outcome == "unavailable" else 1)
    if sent:
        msg = sent[0]
        assert str(msg["From"]) == "XaKore <hello@xakore.com>" and msg["To"] == "new@reef.example"
        assert public_url in msg.get_content()
        assert ("as owner" if flow == "new_organisation" else "as viewer") in msg.get_content()
        assert "ExaCarib" not in str(msg)
    with db.tx() as conn:
        records = conn.execute("SELECT token_hash FROM org_invites WHERE customer_id = %s", (cid,)).fetchall()
        logged = conn.execute("SELECT detail::text FROM audit_log WHERE customer_id = %s", (cid,)).fetchall()
        outbox = conn.execute("SELECT body, headers FROM sim_channel_outbox WHERE customer_id = %s", (cid,)).fetchall()
    assert token not in str(records) and token not in str(logged) and token not in str(outbox)
    assert token not in caplog.text
    # SMTP failure does not discard the committed organisation or its invitation.
    assert client.get(f"{API}/invites/{token}").status_code == 200
    client.cookies.clear()
    accepted = client.post(
        f"{API}/invites/{token}/accept", json={"password": "a sufficiently long passphrase", "name": "New Member"}
    )
    assert accepted.status_code == 200, accepted.text
    assert client.get(f"{API}/invites/{token}").status_code == 410


def test_refused_invitation_requests_do_not_send_mail(client, admin_headers, monkeypatch):
    created = client.post(
        f"{API}/admin/organisations",
        json={"name": "Coral", "products": ["connect"], "owner_email": "owner@coral.example"},
        headers=admin_headers,
    ).json()
    sent = []
    monkeypatch.setattr(mailer, "send", lambda msg: sent.append(msg))
    monkeypatch.setenv("EXA_SMTP_HOST", "smtp.example.net")
    client.cookies.clear()
    refused = client.post(f"{API}/orgs/{created['id']}/invites", json={"email": "new@example.net", "role": "viewer"})
    assert refused.status_code == 401
    duplicate = client.post(
        f"{API}/admin/organisations",
        json={"name": "Coral", "products": ["connect"], "owner_email": "another@example.net"},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409 and sent == []


def test_password_reset_uses_external_https_without_changing_passkey_origin_setting(client, settings, monkeypatch):
    from exaconnect_controller.api import password_reset

    client.app.state.settings = replace(settings, public_url="")
    monkeypatch.setenv("EXA_SMTP_HOST", "smtp.example.net")
    sent = []
    monkeypatch.setattr(password_reset, "_send", lambda to, url: sent.append((to, url)))
    response = client.post(
        f"{API}/auth/password-reset",
        json={"email": ADMIN[0]},
        headers={"Host": "hub.xakore.com", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 202 and response.json() == {"emailed": True}
    assert len(sent) == 1 and sent[0][0] == ADMIN[0]
    assert sent[0][1].startswith("https://hub.xakore.com/reset/")
    assert settings.public_url == ""
