"""Channel gap fixes (ADR 0032): staff attachments on every channel, the
hard spend limit before WhatsApp, SMS and email sends, and email sending limits."""

import base64

from exaconnect_controller import db

from .commai_helpers import base, business, run_jobs
from .test_commai_channels import PNG, Visitor, _account, _conv_for, _outbox, _wa_inbound, _widget_key

PDF = b"%PDF-1.4\n" + b"0" * 64
GIF = b"GIF89a" + b"\x00" * 32


def _upload(client, b, conv_id, name, type_, data):
    return client.post(
        f"{base(b)}/conversations/{conv_id}/files",
        json={"name": name, "type": type_, "data": base64.b64encode(data).decode()},
        headers=b["agent"]["h"],
    )


def _reply(client, b, conv_id, body="", files=()):
    return client.post(
        f"{base(b)}/conversations/{conv_id}/messages",
        json={"body": body, "attachments": list(files)},
        headers=b["agent"]["h"],
    )


def _events(b, type_):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM commai_events WHERE customer_id = %s AND type = %s ORDER BY seq", (b["id"], type_)
        ).fetchall()


def test_staff_attachment_on_website_chat(client):
    b = business(client)
    key = _widget_key(client, b)
    v = Visitor(client, key)
    v.say("Can you send me the form?")
    conv = _conv_for(b, "web")
    f = _upload(client, b, conv["id"], "form.pdf", "application/pdf", PDF)
    assert f.status_code == 201, f.text
    # The contents must match the type.
    bad = _upload(client, b, conv["id"], "fake.pdf", "application/pdf", PNG)
    assert bad.status_code == 415 and bad.json()["code"] == "unsupported_type"
    # An internal seat can't upload to send.
    r = client.post(
        f"{base(b)}/conversations/{conv['id']}/files",
        json={"name": "x.pdf", "type": "application/pdf", "data": base64.b64encode(PDF).decode()},
        headers=b["internal"]["h"],
    )
    assert r.status_code == 403

    r = _reply(client, b, conv["id"], "", [f.json()["id"]])  # a file with no text is a reply
    assert r.status_code == 201, r.text
    assert r.json()["attachments"][0]["name"] == "form.pdf"
    # The visitor sees it and can open it with their session.
    msgs = v.read().json()["items"]
    att = msgs[-1]["attachments"][0]
    assert att["name"] == "form.pdf" and att["type"] == "application/pdf"
    got = client.get(f"{v.u}/files/{att['id']}", params={"s": v.token}, headers={"Origin": v.origin})
    assert got.status_code == 200 and got.content == PDF
    # Staff can open it from the inbox. A file can't be sent twice.
    assert client.get(f"{base(b)}/files/{att['id']}", headers=b["agent"]["h"]).content == PDF
    assert _reply(client, b, conv["id"], "again", [att["id"]]).status_code == 404
    # Another person's pending upload can't be sent by someone else.
    f2 = _upload(client, b, conv["id"], "mine.png", "image/png", PNG).json()
    r = client.post(
        f"{base(b)}/conversations/{conv['id']}/messages",
        json={"body": "x", "attachments": [f2["id"]], "take_over": True},
        headers=b["agent2"]["h"],
    )
    assert r.status_code == 404


def test_staff_attachment_through_whatsapp_sms_and_email(client, monkeypatch):
    b = business(client)
    wa = _account(client, b, "whatsapp", "+18685550100")
    _wa_inbound(client, wa, "Hi, send the statement please", "wamid.a1")
    conv = _conv_for(b, "whatsapp")
    # WhatsApp can't take a GIF; it can take a PDF.
    gif = _upload(client, b, conv["id"], "x.gif", "image/gif", GIF)
    assert gif.status_code == 415 and "PNG, JPEG, PDF" in gif.json()["detail"]
    pdf = _upload(client, b, conv["id"], "statement.pdf", "application/pdf", PDF).json()
    assert _reply(client, b, conv["id"], "Here it is", [pdf["id"]]).status_code == 201
    run_jobs()
    out = [o for o in _outbox(client, b) if o["channel"] == "whatsapp"]
    assert out[0]["body"] == "Here it is" and out[0]["attachments"][0]["name"] == "statement.pdf"
    link = out[0]["attachments"][0]["link"]
    # The provider fetches the file from the signed link; a changed link gets nothing.
    got = client.get(link)
    assert got.status_code == 200 and got.content == PDF
    assert client.get(link[:-4] + "0000").status_code == 404

    # SMS carries small images only.
    sms = _account(client, b, "sms", "+18685550200")
    client.post(
        f"{base(b)}/channel-accounts/{sms['id']}/simulate-inbound",
        json={"from": "+18685550101", "body": "Hello"},
        headers=b["agent"]["h"],
    )
    sconv = _conv_for(b, "sms")
    assert _upload(client, b, sconv["id"], "x.pdf", "application/pdf", PDF).status_code == 415
    assert _upload(client, b, sconv["id"], "x.png", "image/png", PNG).status_code == 201

    # Email attaches the file.
    em = _account(client, b, "email", "help@examplebank.tt")
    client.post(
        f"{base(b)}/channel-accounts/{em['id']}/simulate-inbound",
        json={"from": "ana@example.org", "subject": "Statement", "body": "Please send it"},
        headers=b["agent"]["h"],
    )
    econv = _conv_for(b, "email")
    f = _upload(client, b, econv["id"], "statement.pdf", "application/pdf", PDF).json()
    assert _reply(client, b, econv["id"], "Attached.", [f["id"]]).status_code == 201
    run_jobs()
    mail = [o for o in _outbox(client, b) if o["channel"] == "email"][0]
    assert mail["body"].strip() == "Attached."
    assert mail["attachments"] == [{"name": "statement.pdf", "type": "application/pdf", "size": len(PDF)}]


def test_hard_spend_limit_blocks_channel_sends(client):
    b = business(client)
    wa = _account(client, b, "whatsapp", "+18685550100")
    _wa_inbound(client, wa, "Hello", "wamid.s1")
    conv = _conv_for(b, "whatsapp")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO usage_limits (customer_id, meter, monthly_hard) VALUES (%s, 'message_out:whatsapp', 1)",
            (b["id"],),
        )
    assert _reply(client, b, conv["id"], "First").status_code == 201
    run_jobs()
    r = _reply(client, b, conv["id"], "Second")
    assert r.status_code == 422 and "limit is used up" in r.json()["detail"]
    assert _events(b, "message.blocked")[-1]["data"]["reason"].startswith("This month's WhatsApp limit")

    # Email is checked too.
    em = _account(client, b, "email", "help@examplebank.tt")
    client.post(
        f"{base(b)}/channel-accounts/{em['id']}/simulate-inbound",
        json={"from": "ana@example.org", "subject": "Hi", "body": "Hello"},
        headers=b["agent"]["h"],
    )
    econv = _conv_for(b, "email")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO usage_limits (customer_id, meter, monthly_hard) VALUES (%s, 'message_out:email', 0)",
            (b["id"],),
        )
    r = _reply(client, b, econv["id"], "Hi")
    assert r.status_code == 422 and "Email limit" in r.json()["detail"]


def test_email_sending_limits(client):
    b = business(client)
    em = _account(client, b, "email", "help@examplebank.tt")
    bad = client.patch(
        f"{base(b)}/channel-accounts/{em['id']}",
        json={"settings": {"email_limits": {"daily": 0}}},
        headers=b["agent"]["h"],
    )
    assert bad.status_code == 422
    r = client.patch(
        f"{base(b)}/channel-accounts/{em['id']}",
        json={"settings": {"email_limits": {"daily": 5, "per_domain_daily": 1}}},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text
    for who in ("ana@example.org", "ben@example.org", "cy@other.org"):
        client.post(
            f"{base(b)}/channel-accounts/{em['id']}/simulate-inbound",
            json={"from": who, "subject": "Hi", "body": "Hello"},
            headers=b["agent"]["h"],
        )
    with db.tx() as conn:
        convs = {
            r["address"]: r["id"]
            for r in conn.execute(
                """SELECT c.id, ci.address FROM conversations c JOIN contact_identities ci ON ci.id = c.identity_id
                   WHERE c.customer_id = %s AND c.channel = 'email'""",
                (b["id"],),
            ).fetchall()
        }
    assert _reply(client, b, convs["ana@example.org"], "Hi Ana").status_code == 201
    run_jobs()
    r = _reply(client, b, convs["ben@example.org"], "Hi Ben")  # same domain, limit 1 a day
    assert r.status_code == 422 and "example.org" in r.json()["detail"]
    assert _reply(client, b, convs["cy@other.org"], "Hi Cy").status_code == 201
