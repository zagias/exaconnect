"""WhatsApp through Meta's Cloud API, interactive messages and click-to-chat (ADR 0029)."""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets

from cryptography.fernet import Fernet

from exaconnect_controller import db
from exaconnect_controller.commai import golive
from exaconnect_controller.commai.channels import social, whatsapp_cloud

from .commai_helpers import base, business, run_jobs, switch_on

NUMBER = "+18685550300"
CUSTOMER = "+18685550301"


def _signup(client, b, number=NUMBER):
    r = client.post(f"{base(b)}/whatsapp-cloud/signup", json={"number": number}, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    return r.json()


def _path(a):
    return "/api/v1" + a["webhook_url"].split("/api/v1", 1)[1]


def _row(a):
    with db.tx() as conn:
        return conn.execute("SELECT * FROM channel_accounts WHERE id = %s", (a["id"],)).fetchone()


def _in(client, b, a, body="Hello", frm=CUSTOMER, mid=None, button=None):
    r = client.post(
        f"{base(b)}/whatsapp-cloud/{a['id']}/simulate-inbound",
        json={"from": frm, "body": body, "id": mid or "", "button": button},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text
    return r.json()


def _conv(b, address=CUSTOMER):
    with db.tx() as conn:
        return conn.execute(
            """SELECT c.* FROM conversations c JOIN contact_identities ci ON ci.id = c.identity_id
               WHERE c.customer_id = %s AND ci.address = %s AND c.channel = 'whatsapp'
               ORDER BY c.created_at DESC LIMIT 1""",
            (b["id"], address),
        ).fetchone()


def _reply(client, b, conv, body="", template=""):
    return client.post(
        f"{base(b)}/conversations/{conv['id']}/messages",
        json={"body": body, "template": template, "take_over": True},
        headers=b["agent"]["h"],
    )


def _age(conv, days):
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversations SET last_inbound_at = now() - %s WHERE id = %s", (dt.timedelta(days=days), conv["id"])
        )


def _outbox(b):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM sim_channel_outbox WHERE customer_id = %s AND channel = 'whatsapp' ORDER BY id", (b["id"],)
        ).fetchall()


def _interactive(client, b, conv, payload):
    return client.post(f"{base(b)}/conversations/{conv['id']}/interactive", json=payload, headers=b["agent"]["h"])


BUTTONS = {"body": "Is this about your card?", "buttons": [{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}]}


def test_cloud_api_refused_until_switched_on_and_not_on_the_generic_form(client):
    b = business(client)
    r = client.post(f"{base(b)}/whatsapp-cloud/signup", json={"number": NUMBER}, headers=b["agent"]["h"])
    assert r.status_code == 409 and "not switched on" in r.json()["detail"]
    setup = client.get(f"{base(b)}/whatsapp-cloud", headers=b["agent"]["h"]).json()
    assert setup["available"] is False and setup["embedded_signup"]["ready"] is False
    assert "EXA_META_ES_CONFIG_ID" in setup["embedded_signup"]["missing_env"]
    r = client.post(
        f"{base(b)}/channel-accounts",
        json={"channel": "whatsapp", "provider": "meta-cloud-simulated", "address": NUMBER},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 422  # only through sign-up, where the gate is

    switch_on("channel", "whatsapp-cloud", b["id"])
    a = _signup(client, b)
    assert a["status"] == "live" and a["simulated"] and a["phone_number_id"] and a["secret"]
    _in(client, b, a)
    # Switched off: webhooks and sends are refused.
    with db.tx() as conn:
        golive.check(conn, "channel", "whatsapp-cloud", "templates", False, "", "test")
    r = _reply(client, b, _conv(b), "Hi")
    assert r.status_code == 422 and "Cloud API is not switched on" in r.json()["detail"]
    raw = json.dumps(whatsapp_cloud.sim_payload(_row(a), CUSTOMER, "again", "wamid.X2")).encode()
    r = client.post(_path(a), content=raw, headers={"X-Hub-Signature-256": social.meta_sign(a["secret"], raw)})
    assert r.status_code == 403


def test_cloud_signatures_handshake_and_duplicates(client):
    b = business(client)
    switch_on("channel", "whatsapp-cloud")
    a = _signup(client, b)
    raw = json.dumps(whatsapp_cloud.sim_payload(_row(a), CUSTOMER, "Hello", "wamid.ABC")).encode()
    url = _path(a)
    assert client.post(url, content=raw).status_code == 403
    bad = {"X-Hub-Signature-256": social.meta_sign("not-the-secret", raw)}
    assert client.post(url, content=raw, headers=bad).status_code == 403
    good = {"X-Hub-Signature-256": social.meta_sign(a["secret"], raw)}
    assert client.post(url, content=raw + b" ", headers=good).status_code == 403
    r = client.post(url, content=raw, headers=good)
    assert r.status_code == 200 and r.json()["received"] == 1
    assert client.post(url, content=raw, headers=good).json()["duplicates"] == 1

    q = {"hub.mode": "subscribe", "hub.challenge": "8812", "hub.verify_token": a["secret"]}
    r = client.get(url, params=q)
    assert r.status_code == 200 and r.text == "8812"
    assert client.get(url, params={**q, "hub.verify_token": "x"}).status_code == 403
    # The phase 2 generic path does not take Cloud API traffic.
    r = client.post(f"/api/v1/commai/channels/hooks/{_row(a)['hook_token']}", content=raw, headers=good)
    assert r.status_code == 400


def test_embedded_signup_and_app_wide_webhook(client, monkeypatch):
    b = business(client)
    other = business(client, "Other Bank", ("agent",))
    switch_on("channel", "whatsapp-cloud")
    app_secret, token = secrets.token_hex(16), "EAAG" + secrets.token_urlsafe(40)
    for k, v in (("EXA_META_APP_ID", "1234567890"), ("EXA_META_APP_SECRET", app_secret),
                 ("EXA_META_ES_CONFIG_ID", "998877"), ("EXA_SECRETS_KEY", Fernet.generate_key().decode())):  # fmt: skip
        monkeypatch.setenv(k, v)
    calls = []

    def fake_graph(self, method, path, tok, payload=None, raw=None, ctype=""):
        calls.append((method, path.split("?")[0], tok == token))
        if path.startswith("/oauth/access_token"):
            assert "code=CODE123" in path
            return {"access_token": token}
        if path.startswith("/555000111?fields"):
            return {"display_phone_number": "+1 868-555-0400", "verified_name": "Example Bank"}
        if path.endswith("/messages"):
            return {"messages": [{"id": "wamid.REAL1"}]}
        return {"success": True}

    monkeypatch.setattr(whatsapp_cloud.CloudApi, "_graph", fake_graph)
    setup = client.get(f"{base(b)}/whatsapp-cloud", headers=b["agent"]["h"]).json()
    assert setup["embedded_signup"] == {"ready": True, "missing_env": [], "app_id": "1234567890", "config_id": "998877"}
    body = {"simulated": False, "code": "CODE123", "waba_id": "777000", "phone_number_id": "555000111", "pin": "123456"}
    r = client.post(f"{base(b)}/whatsapp-cloud/signup", json=body, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["address"] == "+18685550400" and a["token_saved"] and token not in r.text and "secret" not in a
    assert [c[:2] for c in calls] == [
        ("GET", "/oauth/access_token"),
        ("POST", "/777000/subscribed_apps"),
        ("POST", "/555000111/register"),
        ("GET", "/555000111"),
    ]
    with db.tx() as conn:
        assert token not in json.dumps(conn.execute("SELECT * FROM audit_log").fetchall(), default=str)
    # The same number can't be connected by another business.
    r = client.post(f"{base(other)}/whatsapp-cloud/signup", json=body, headers=other["agent"]["h"])
    assert r.status_code == 409

    # Meta's app-wide webhook, routed by phone number id.
    payload = whatsapp_cloud.sim_payload(_row(a), CUSTOMER, "Hello bank", "wamid.IN1")
    payload["entry"].append(
        {"id": "1", "changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "1"}, "messages": []}}]}
    )
    raw = json.dumps(payload).encode()
    url = "/api/v1/commai/channels/meta/webhook"
    assert client.post(url, content=raw, headers={"X-Hub-Signature-256": social.meta_sign("x", raw)}).status_code == 403
    r = client.post(url, content=raw, headers={"X-Hub-Signature-256": social.meta_sign(app_secret, raw)})
    assert r.status_code == 200 and r.json()["received"] == 1 and r.json()["unknown_accounts"] == 1, r.text
    # A reply goes out through the Graph API with the stored token.
    assert _reply(client, b, _conv(b), "Hello from the bank").status_code == 201
    run_jobs()
    assert ("POST", "/555000111/messages", True) in calls
    with db.tx() as conn:
        m = conn.execute("SELECT status, provider_ref FROM messages WHERE body = 'Hello from the bank'").fetchone()
    assert m["status"] == "sent" and m["provider_ref"] == "wamid.REAL1"


def test_window_templates_sync_statuses_and_opt_out(client):
    b = business(client)
    switch_on("channel", "whatsapp-cloud")
    a = _signup(client, b)
    u = base(b)
    _in(client, b, a, "I need help")
    conv = _conv(b)
    sent = _reply(client, b, conv, "Happy to help").json()
    run_jobs()
    for st in ("delivered", "read"):
        r = client.post(
            f"{u}/whatsapp-cloud/{a['id']}/simulate-status", json={"message_id": sent["id"], "status": st},
            headers=b["agent"]["h"],
        )  # fmt: skip
        assert r.json()["receipts"] == 1
    with db.tx() as conn:
        assert conn.execute("SELECT status FROM messages WHERE id = %s", (sent["id"],)).fetchone()["status"] == "read"

    _age(conv, 2)
    r = _reply(client, b, conv, "Following up")
    assert r.status_code == 422 and "24-hour" in r.json()["detail"]
    tpl = client.post(
        f"{u}/whatsapp-templates",
        json={
            "name": "card_ready",
            "language": "en",
            "category": "utility",
            "body": "Hello {{1}}, your card is ready.",
        },
        headers=b["agent"]["h"],
    ).json()
    sub = client.post(f"{u}/whatsapp-cloud/{a['id']}/templates/{tpl['id']}/submit", headers=b["agent"]["h"])
    assert sub.status_code == 200 and sub.json()["status"] == "pending" and sub.json()["provider_template_id"]
    assert _reply(client, b, conv, "Hello Ana, your card is ready.", template="card_ready").status_code == 422
    sync = client.post(f"{u}/whatsapp-cloud/{a['id']}/templates/sync", headers=b["agent"]["h"])
    assert sync.json() == {"remote": 1, "updated": 1}
    assert _reply(client, b, conv, "Hello Ana, your card is ready.", template="card_ready").status_code == 201
    run_jobs()
    assert _outbox(b)[-1]["template"] == "card_ready:en"
    # Meta pauses the template: the webhook brings the status in, and sends stop.
    r = client.post(
        f"{u}/whatsapp-cloud/{a['id']}/simulate-template-status",
        json={"template_id": tpl["id"], "event": "PAUSED", "reason": "Low quality"},
        headers=b["agent"]["h"],
    )
    assert r.json()["templates"] == 1
    assert _reply(client, b, conv, "Hello Ana, your card is ready.", template="card_ready").status_code == 422

    _in(client, b, a, "STOP")
    r = _reply(client, b, _conv(b), "Bye")
    assert r.status_code == 422 and "opted out" in r.json()["detail"]


def test_interactive_buttons_and_lists(client):
    b = business(client)
    switch_on("channel", "whatsapp-cloud")
    a = _signup(client, b)
    _in(client, b, a, "Hi")
    conv = _conv(b)
    assert _interactive(client, b, conv, BUTTONS).status_code == 201
    lst = {
        "body": "Choose a topic",
        "list": {"button": "Topics", "sections": [{"title": "Cards", "rows": [
            {"id": "lost", "title": "Lost card", "description": "Block it now"}, {"id": "new", "title": "New card"}]}]},
    }  # fmt: skip
    assert _interactive(client, b, conv, lst).status_code == 201
    run_jobs()
    out = _outbox(b)
    assert out[0]["template"] == "interactive:button" and "[Yes] | [No]" in out[0]["body"]
    assert out[1]["template"] == "interactive:list" and "[Lost card]" in out[1]["body"]

    # The person taps a button: it arrives as the button's title.
    _in(client, b, a, "", button={"id": "yes", "title": "Yes"})
    with db.tx() as conn:
        last = conn.execute(
            "SELECT body FROM messages WHERE conversation_id = %s AND direction = 'in'"
            " ORDER BY created_at DESC LIMIT 1",
            (conv["id"],),
        ).fetchone()
    assert last["body"] == "Yes"

    four = {"body": "x", "buttons": [{"title": f"B{i}"} for i in range(4)]}
    assert _interactive(client, b, conv, four).status_code == 422
    long = {"body": "x", "buttons": [{"title": "A title far too long for WhatsApp"}]}
    r = _interactive(client, b, conv, long)
    assert r.status_code == 422 and "20 characters" in r.json()["detail"]
    both = {**BUTTONS, "list": lst["list"]}
    assert _interactive(client, b, conv, both).status_code == 422
    eleven = {"body": "x", "list": {"button": "Pick", "sections": [{"rows": [{"title": f"R{i}"} for i in range(11)]}]}}
    assert _interactive(client, b, conv, eleven).status_code == 422

    _age(conv, 2)
    r = _interactive(client, b, conv, BUTTONS)
    assert r.status_code == 422 and "inside the window" in r.json()["detail"]


def test_interactive_on_phase_two_providers(client):
    b = business(client)
    r = client.post(
        f"{base(b)}/channel-accounts",
        json={"channel": "whatsapp", "provider": "simulated", "address": NUMBER},
        headers=b["agent"]["h"],
    )
    acct = r.json()
    client.post(
        f"{base(b)}/channel-accounts/{acct['id']}/simulate-inbound",
        json={"from": CUSTOMER, "body": "Hi"},
        headers=b["agent"]["h"],
    )
    conv = _conv(b)
    assert _interactive(client, b, conv, BUTTONS).status_code == 201
    run_jobs()
    assert _outbox(b)[-1]["template"] == "interactive:button"
    # Twilio sends buttons only as pre-created Content templates: refused with that reason.
    with db.tx() as conn:
        conn.execute("UPDATE channel_accounts SET provider = 'twilio' WHERE id = %s", (acct["id"],))
    r = _interactive(client, b, conv, BUTTONS)
    assert r.status_code == 422 and "Content templates" in r.json()["detail"]
    assert whatsapp_cloud.supports_interactive("360dialog") == ""
    assert whatsapp_cloud.cloud_interactive(whatsapp_cloud.check_interactive(BUTTONS))["action"]["buttons"][0] == {
        "type": "reply", "reply": {"id": "yes", "title": "Yes"},
    }  # fmt: skip


def test_media_links_notes_and_tenant_isolation(client):
    b = business(client)
    z = business(client, "Bank Z", ("agent",))
    switch_on("channel", "whatsapp-cloud")
    a = _signup(client, b)
    u = f"{base(b)}/whatsapp-cloud/{a['id']}"
    pdf = b"%PDF-1.4 test statement"
    r = client.post(
        f"{u}/media",
        json={"filename": "statement.pdf", "content_type": "application/pdf", "data": base64.b64encode(pdf).decode()},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    mid = r.json()["media_id"]
    got = client.get(f"{u}/media/{mid}", headers=b["agent"]["h"])
    assert got.status_code == 200 and got.content == pdf and got.headers["content-type"] == "application/pdf"

    link = client.get(
        f"{base(b)}/whatsapp-link",
        params={"account_id": a["id"], "text": "Hello Example Bank"},
        headers=b["agent"]["h"],
    ).json()
    assert link["url"] == "https://wa.me/18685550300?text=Hello%20Example%20Bank"
    assert link["qr_svg"].startswith("<svg") and "</svg>" in link["qr_svg"]

    # Notes never leave through the Cloud API.
    _in(client, b, a, "Hello")
    conv = _conv(b)
    note = "INTERNAL: check KYC before replying"
    client.post(f"{base(b)}/conversations/{conv['id']}/notes", json={"body": note}, headers=b["agent"]["h"])
    _reply(client, b, conv, "We're on it")
    run_jobs()
    assert all(note not in json.dumps(o, default=str) for o in _outbox(b)) and len(_outbox(b)) == 1

    # Another business can't reach any of it.
    zh = z["agent"]["h"]
    assert client.get(f"{base(b)}/whatsapp-cloud", headers=zh).status_code == 403
    zu = f"{base(z)}/whatsapp-cloud/{a['id']}"
    assert client.get(f"{zu}/media/{mid}", headers=zh).status_code == 404
    assert client.post(f"{zu}/templates/sync", headers=zh).status_code in (404, 409)
    assert client.post(f"{zu}/simulate-inbound", json={"from": CUSTOMER, "body": "x"}, headers=zh).status_code == 404
    r = client.get(f"{base(z)}/whatsapp-link", params={"account_id": a["id"]}, headers=zh)
    assert r.status_code == 404
    r = client.post(f"{base(z)}/conversations/{conv['id']}/interactive", json=BUTTONS, headers=zh)
    assert r.status_code == 404
