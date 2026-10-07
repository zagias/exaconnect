"""CommAI channels (ADR 0018): website chat, WhatsApp, SMS and email.

Acceptance tests covered here: 1 (a website or WhatsApp enquiry reaches the
inbox, is assigned and gets a reply on its original channel), 2 (notes are
unreachable through the widget and channel sends), 5 (repeated webhooks and
retries create no duplicates) and 9 (free-form WhatsApp outside the 24-hour
window is blocked; an approved template is allowed).
"""

import base64
import datetime as dt
import json
import time

from exaconnect_controller import db
from exaconnect_controller.commai import inbox
from exaconnect_controller.commai.channels import messaging, providers, widget

from .commai_helpers import api_key, base, business, run_jobs, switch_on

SITE = "https://www.examplebank.tt"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ---- helpers ------------------------------------------------------------------------------


def _team_rule(client, b, channel):
    u = base(b)
    team = client.post(
        f"{u}/teams", json={"name": f"Front desk {channel}", "members": [b["agent"]["id"]]}, headers=b["agent"]["h"]
    )
    assert team.status_code == 201, team.text
    r = client.post(
        f"{u}/routing-rules",
        json={"name": f"All {channel}", "match": {"channel": channel}, "team_id": team.json()["id"]},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    return team.json()["id"]


def _widget_key(client, b, origins=(SITE,), settings=None):
    r = client.post(
        f"{base(b)}/widget-keys",
        json={"name": "Main site", "allowed_origins": list(origins), "settings": settings or {}},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


class Visitor:
    """A browser on the business's website running the widget."""

    def __init__(self, client, key, origin=SITE, user_token=""):
        self.c, self.key, self.origin = client, key["public_key"], origin
        self.u = f"/api/v1/commai/widget/{self.key}"
        r = client.post(
            f"{self.u}/session", json={"user_token": user_token} if user_token else {}, headers={"Origin": origin}
        )
        self.start = r
        self.token = r.json().get("token", "") if r.status_code == 200 else ""
        self.conv = None

    def h(self):
        return {"Origin": self.origin, "X-Widget-Session": self.token}

    def say(self, body, client_id=None, attachments=()):
        payload = {
            "body": body,
            "client_id": client_id or f"cid{time.monotonic_ns()}",
            "attachments": list(attachments),
        }
        if self.conv:
            payload["conversation_id"] = self.conv
        r = self.c.post(f"{self.u}/messages", json=payload, headers=self.h())
        if r.status_code == 201:
            self.conv = r.json()["conversation_id"]
        return r

    def read(self):
        r = self.c.get(f"{self.u}/messages", params={"conversation_id": self.conv}, headers=self.h())
        assert r.status_code == 200, r.text
        return r


def _account(client, b, channel, address, provider="simulated", settings=None):
    r = client.post(
        f"{base(b)}/channel-accounts",
        json={
            "channel": channel,
            "provider": provider,
            "address": address,
            "name": f"{channel} line",
            "settings": settings or {},
        },
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _hook(client, acct, payload, *, sign=True, secret=None):
    raw = json.dumps(payload).encode()
    sig = providers.Simulated.sign(secret or acct["secret"], raw) if sign else ""
    path = acct["webhook_url"].split("/api/v1", 1)[1]
    return client.post(
        f"/api/v1{path}", content=raw, headers={"Content-Type": "application/json", "X-Exa-Signature": sig}
    )


def _wa_inbound(client, acct, body, msg_id, frm="+18685550101", name="Ana"):
    return _hook(client, acct, {"messages": [{"id": msg_id, "from": frm, "name": name, "body": body}]})


def _outbox(client, b):
    return client.get(f"{base(b)}/channels/outbox", headers=b["agent"]["h"]).json()


def _conv_for(b, channel):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM conversations WHERE customer_id = %s AND channel = %s ORDER BY created_at DESC LIMIT 1",
            (b["id"], channel),
        ).fetchone()


# ---- acceptance test 1 ----------------------------------------------------------------------


def test_website_enquiry_reaches_inbox_is_assigned_and_answered_in_the_widget(client):
    b = business(client)
    team = _team_rule(client, b, "web")
    key = _widget_key(client, b)
    assert key["secret"].startswith("wsk_")
    v = Visitor(client, key)
    assert v.start.status_code == 200, v.start.text
    assert v.start.headers["access-control-allow-origin"] == SITE
    r = v.say("Hello, my card was declined")
    assert r.status_code == 201, r.text

    conv = _conv_for(b, "web")
    assert str(conv["team_id"]) == team and str(conv["assignee_id"]) == b["agent"]["id"]
    r = client.post(
        f"{base(b)}/conversations/{conv['id']}/messages",
        json={"body": "Hi, let me check that card."},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text

    items = v.read().json()["items"]
    assert [(m["from"], m["body"]) for m in items] == [
        ("you", "Hello, my card was declined"),
        ("team", "Hi, let me check that card."),
    ]
    assert "agent@" not in v.read().text  # staff emails never reach the visitor
    # Polling with `after` returns only what's new.
    after = items[0]["at"]
    r = client.get(f"{v.u}/messages", params={"conversation_id": v.conv, "after": after}, headers=v.h())
    assert [m["body"] for m in r.json()["items"]] == ["Hi, let me check that card."]


def test_whatsapp_enquiry_reaches_inbox_is_assigned_and_answered_on_whatsapp(client):
    b = business(client)
    team = _team_rule(client, b, "whatsapp")
    acct = _account(client, b, "whatsapp", "+1 868 555 0100")
    assert acct["status"] == "live" and acct["simulated"] and acct["address"] == "+18685550100"
    r = _wa_inbound(client, acct, "Do you open on Saturday?", "wamid.1")
    assert r.status_code == 200, r.text and r.json()["received"] == 1

    conv = _conv_for(b, "whatsapp")
    assert str(conv["team_id"]) == team and str(conv["assignee_id"]) == b["agent"]["id"]
    r = client.post(
        f"{base(b)}/conversations/{conv['id']}/messages",
        json={"body": "Yes, 9 to 1 on Saturdays."},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "queued"
    run_jobs()
    sent = _outbox(client, b)
    assert [(o["channel"], o["to_address"], o["body"]) for o in sent] == [
        ("whatsapp", "+18685550101", "Yes, 9 to 1 on Saturdays.")
    ]
    detail = client.get(f"{base(b)}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()
    out = [m for m in detail["messages"] if m["direction"] == "out"][0]
    assert out["status"] == "sent" and out["provider_ref"] == sent[0]["provider_ref"]

    # Delivery and read receipts update the message; a late "delivered" never goes backwards.
    for status in ("delivered", "read", "delivered"):
        r = _hook(client, acct, {"statuses": [{"ref": out["provider_ref"], "status": status}]})
        assert r.status_code == 200, r.text
    with db.tx() as conn:
        assert conn.execute("SELECT status FROM messages WHERE id = %s", (out["id"],)).fetchone()["status"] == "read"


# ---- acceptance test 2 --------------------------------------------------------------------------


def test_notes_never_reach_the_widget_or_channel_sends(client):
    b = business(client)
    u = base(b)
    key = _widget_key(client, b)
    v = Visitor(client, key)
    v.say("I need help")
    wa = _account(client, b, "whatsapp", "+18685550100")
    em = _account(client, b, "email", "help@examplebank.tt")
    _wa_inbound(client, wa, "Hi there", "wamid.n1")
    client.post(
        f"{u}/channel-accounts/{em['id']}/simulate-inbound",
        json={"from": "ana@example.org", "subject": "Card", "body": "Hello"},
        headers=b["agent"]["h"],
    )
    for ch in ("web", "whatsapp", "email"):
        conv = _conv_for(b, ch)
        r = client.post(
            f"{u}/conversations/{conv['id']}/notes", json={"body": f"SECRET note on {ch}"}, headers=b["internal"]["h"]
        )
        assert r.status_code == 201, r.text
        r = client.post(
            f"{u}/conversations/{conv['id']}/messages", json={"body": f"Reply on {ch}"}, headers=b["agent"]["h"]
        )
        assert r.status_code == 201, r.text
    run_jobs()

    # Every widget path the visitor can reach.
    seen = [
        v.read().text,
        client.get(f"{v.u}/conversations", headers=v.h()).text,
        client.post(f"{v.u}/session", json={}, headers=v.h()).text,
        client.get(f"{v.u}/config", headers={"Origin": SITE}).text,
    ]
    assert all("SECRET" not in s for s in seen)
    assert "Reply on web" in seen[0]
    # Everything the channels sent out.
    sent = _outbox(client, b)
    assert {o["channel"] for o in sent} == {"whatsapp", "email"}
    with db.tx() as conn:
        rows = conn.execute("SELECT * FROM sim_channel_outbox WHERE customer_id = %s", (b["id"],)).fetchall()
    assert rows and all("SECRET" not in json.dumps(r, default=str) for r in rows)


# ---- acceptance test 5 ------------------------------------------------------------------------------


def test_repeated_webhooks_and_retries_create_no_duplicates(client):
    b = business(client)
    u = base(b)
    wa = _account(client, b, "whatsapp", "+18685550100")
    for _ in range(3):
        r = _wa_inbound(client, wa, "Same message", "wamid.dup")
        assert r.status_code == 200
    assert r.json() == {"received": 0, "duplicates": 1, "receipts": 0}

    key = _widget_key(client, b)
    v = Visitor(client, key)
    first = v.say("Retry me", client_id="client-msg-0001")
    again = v.say("Retry me", client_id="client-msg-0001")
    assert first.status_code == again.status_code == 201
    assert again.json()["duplicate"] and again.json()["message"]["id"] == first.json()["message"]["id"]

    em = _account(client, b, "email", "help@examplebank.tt")
    mail = {"from": "Ana <ana@example.org>", "subject": "Hello", "text": "Once only", "message_id": "<m1@example.org>"}
    path = em["webhook_url"].split("/api/v1", 1)[1]
    for _ in range(2):
        r = client.post(f"/api/v1{path}", json=mail, headers={"X-Exa-Email-Secret": em["secret"]})
        assert r.status_code == 200, r.text

    # A retried reply (same Idempotency-Key) and a retried send job send once.
    conv = _conv_for(b, "whatsapp")
    h = {**b["agent"]["h"], "Idempotency-Key": "reply-1"}
    for _ in range(2):
        assert (
            client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Once"}, headers=h).status_code == 201
        )
    run_jobs()
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'queued' WHERE kind = 'message.send'")  # the worker crashed and retried
    run_jobs()

    with db.tx() as conn:
        counts = {
            r["channel"]: r["n"]
            for r in conn.execute(
                """SELECT c.channel, count(*) AS n FROM messages m JOIN conversations c ON c.id = m.conversation_id
                   WHERE m.customer_id = %s AND m.direction = 'in' GROUP BY c.channel""",
                (b["id"],),
            ).fetchall()
        }
        outs = conn.execute(
            "SELECT count(*) AS n FROM sim_channel_outbox WHERE customer_id = %s", (b["id"],)
        ).fetchone()
    assert counts == {"whatsapp": 1, "web": 1, "email": 1}
    assert outs["n"] == 1


# ---- acceptance test 9 ----------------------------------------------------------------------------------


def test_whatsapp_free_form_outside_the_window_is_blocked_and_a_template_is_allowed(client):
    b = business(client)
    u = base(b)
    wa = _account(client, b, "whatsapp", "+18685550100")
    _wa_inbound(client, wa, "Hi", "wamid.w1")
    conv = _conv_for(b, "whatsapp")
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversations SET last_inbound_at = now() - interval '25 hours' WHERE id = %s", (conv["id"],)
        )

    # A person, an API key and the AI are all refused free-form text.
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Following up"}, headers=b["agent"]["h"])
    assert r.status_code == 422 and "24-hour" in r.json()["detail"]
    key = api_key(client, b["agent"]["h"], ["commai:read", "commai:write"])
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "From our app"}, headers=key)
    assert r.status_code == 422
    with db.tx() as conn:
        conn.execute("UPDATE conversations SET handler = 'ai', handler_user_id = NULL WHERE id = %s", (conv["id"],))
    try:
        with db.tx() as conn:
            inbox.send(conn, b["id"], conv["id"], "AI follow-up", author_kind="ai", author="ai")
        raise AssertionError("the AI's free-form send went through")
    except inbox.InboxError as e:
        assert e.code == 422 and "24-hour" in str(e)

    # A template that isn't approved yet is refused too.
    t = client.post(
        f"{u}/whatsapp-templates",
        json={
            "name": "appointment_reminder",
            "category": "utility",
            "body": "Hello {{1}}, your appointment is on {{2}}.",
        },
        headers=b["agent"]["h"],
    )
    assert t.status_code == 201 and t.json()["status"] == "pending"
    send = {"template_id": t.json()["id"], "params": ["Ana", "Friday at 10:00"], "take_over": True}
    r = client.post(f"{u}/conversations/{conv['id']}/template", json=send, headers=b["agent"]["h"])
    assert r.status_code == 422 and "not approved" in r.json()["detail"]

    # The simulated provider approves it; now it goes out, rendered.
    r = client.post(
        f"{u}/whatsapp-templates/{t.json()['id']}/review", json={"status": "approved"}, headers=b["agent"]["h"]
    )
    assert r.status_code == 200 and r.json()["status_by"] == "simulated provider"
    r = client.post(f"{u}/conversations/{conv['id']}/template", json=send, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    # The template label can't smuggle other text out.
    r = client.post(
        f"{u}/conversations/{conv['id']}/messages",
        json={"body": "Anything I like", "template": "appointment_reminder"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 422
    run_jobs()
    sent = _outbox(client, b)
    assert [(o["body"], o["template"]) for o in sent] == [
        ("Hello Ana, your appointment is on Friday at 10:00.", "appointment_reminder:en")
    ]


def test_queued_whatsapp_reply_is_rechecked_when_sent(client):
    b = business(client)
    wa = _account(client, b, "whatsapp", "+18685550100")
    _wa_inbound(client, wa, "Hi", "wamid.q1")
    conv = _conv_for(b, "whatsapp")
    r = client.post(
        f"{base(b)}/conversations/{conv['id']}/messages", json={"body": "Inside the window"}, headers=b["agent"]["h"]
    )
    assert r.status_code == 201
    with db.tx() as conn:  # the job waited past the window
        conn.execute(
            "UPDATE conversations SET last_inbound_at = now() - interval '2 days' WHERE id = %s", (conv["id"],)
        )
    run_jobs()
    with db.tx() as conn:
        m = conn.execute("SELECT status, error FROM messages WHERE id = %s", (r.json()["id"],)).fetchone()
    assert m["status"] == "blocked" and "24-hour" in m["error"]
    assert _outbox(client, b) == []


# ---- website chat ------------------------------------------------------------------------------------


def test_widget_origins_preflight_and_installation_checker(client):
    b = business(client)
    key = _widget_key(client, b, origins=(SITE, "https://*.examplebank.tt"))
    u = f"/api/v1/commai/widget/{key['public_key']}"
    script = client.get("/api/v1/commai/widget/v1.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert b"ExaCaribChat" in script.content

    r = client.options(f"{u}/messages", headers={"Origin": SITE, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == SITE
    assert "X-Widget-Session" in r.headers["access-control-allow-headers"]
    assert client.options(f"{u}/messages", headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.get(f"{u}/config", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    assert client.get(f"{u}/config").status_code == 403  # no Origin at all
    r = client.get(f"{u}/config", headers={"Origin": "https://shop.examplebank.tt"})  # wildcard subdomain
    assert r.status_code == 200 and r.json()["mode"] == "human_first"

    # A session for one key is no good on another.
    other = _widget_key(client, b)
    v = Visitor(client, key)
    r = client.get(f"/api/v1/commai/widget/{other['public_key']}/conversations", headers=v.h())
    assert r.status_code == 401

    # Heartbeats (text/plain, so no preflight) show where the widget runs, allowed or not.
    for origin in (SITE, "https://staging.example.net"):
        r = client.post(
            f"{u}/heartbeat",
            content=json.dumps({"page": f"{origin}/contact"}),
            headers={"Origin": origin, "Content-Type": "text/plain"},
        )
        assert r.status_code == 204
    keys = client.get(f"{base(b)}/widget-keys", headers=b["agent"]["h"]).json()
    mine = next(k for k in keys if k["id"] == key["id"])
    assert "secret" not in mine and mine["secret_hint"].startswith("…")
    assert {(i["origin"], i["allowed"]) for i in mine["installs"]} == {
        (SITE, True),
        ("https://staging.example.net", False),
    }
    diag = client.get(f"{base(b)}/channels/diagnostics", headers=b["agent"]["h"]).json()
    web = [d for d in diag if d["area"] == "website_chat" and d["status"] == "problem"]
    assert any("isn't on the allowed list" in d["summary"] for d in web)

    # Origins are validated; a test conversation lands in the inbox.
    r = client.patch(
        f"{base(b)}/widget-keys/{key['id']}", json={"allowed_origins": ["not a url"]}, headers=b["agent"]["h"]
    )
    assert r.status_code == 422
    r = client.post(f"{base(b)}/widget-keys/{key['id']}/test-conversation", headers=b["agent"]["h"])
    assert r.status_code == 201
    conv = client.get(f"{base(b)}/conversations/{r.json()['conversation_id']}", headers=b["agent"]["h"]).json()
    assert conv["tags"] == ["test"] and conv["channel"] == "web"


def test_signed_in_customers_see_history_and_typed_emails_see_nothing(client):
    b = business(client)
    key = _widget_key(client, b)
    exp = int(time.time()) + 600
    token = widget.sign_user_token(
        key["secret"], {"sub": "cust-42", "email": "ana@example.org", "name": "Ana", "exp": exp}
    )
    ana = Visitor(client, key, user_token=token)
    assert ana.start.status_code == 200, ana.start.text
    assert ana.start.json()["signed_in"] and ana.start.json()["conversations"] == []
    ana.say("My first question")

    # Ana comes back another day, signed in: she sees her past conversation.
    again = Visitor(client, key, user_token=token)
    assert [c["id"] for c in again.start.json()["conversations"]] == [ana.conv]

    # Someone who only types Ana's email sees nothing of hers.
    stranger = Visitor(client, key)
    assert not stranger.start.json()["signed_in"]
    r = client.post(f"{stranger.u}/contact", json={"name": "Ana", "email": "ana@example.org"}, headers=stranger.h())
    assert r.status_code == 200, r.text
    assert client.get(f"{stranger.u}/conversations", headers=stranger.h()).json()["items"] == []
    r = client.get(f"{stranger.u}/messages", params={"conversation_id": ana.conv}, headers=stranger.h())
    assert r.status_code == 404
    with db.tx() as conn:  # the typed email linked nothing
        idents = conn.execute(
            "SELECT address, verified FROM contact_identities WHERE customer_id = %s ORDER BY verified", (b["id"],)
        ).fetchall()
    assert [i["verified"] for i in idents] == [False, True]

    # Tokens not signed with this widget's secret, expired or unsigned are refused.
    bad = [
        widget.sign_user_token("wsk_wrong", {"sub": "cust-42", "exp": exp}),
        widget.sign_user_token(key["secret"], {"sub": "cust-42", "exp": int(time.time()) - 5}),
        widget.sign_user_token(key["secret"], {"sub": "cust-42", "exp": int(time.time()) + 7 * 86400}),
        token.rsplit(".", 1)[0] + ".",
    ]
    none_alg = widget.b64(b'{"alg":"none"}') + "." + token.split(".")[1] + "."
    for t in [*bad, none_alg]:
        assert Visitor(client, key, user_token=t).start.status_code == 401
    # A rotated secret ends old sessions.
    client.post(f"{base(b)}/widget-keys/{key['id']}/rotate", headers=b["agent"]["h"])
    assert client.get(f"{ana.u}/conversations", headers=ana.h()).status_code == 401


def test_widget_attachments_are_small_typed_and_private(client):
    b = business(client)
    key = _widget_key(client, b)
    v = Visitor(client, key)

    def up(name, ctype, data):
        return client.post(
            f"{v.u}/files", json={"name": name, "type": ctype, "data": base64.b64encode(data).decode()}, headers=v.h()
        )

    assert up("x.png", "image/png", b"not a png").status_code == 415
    assert up("x.exe", "application/x-msdownload", b"MZ").status_code == 415
    assert up("big.png", "image/png", PNG + b"\x00" * (2 * 1024 * 1024)).status_code == 413
    f = up("receipt.png", "image/png", PNG)
    assert f.status_code == 201, f.text
    r = v.say("Here's the receipt", attachments=[f.json()["id"]])
    assert r.status_code == 201, r.text
    assert r.json()["message"]["attachments"][0]["name"] == "receipt.png"

    got = client.get(f"{v.u}/files/{f.json()['id']}", params={"s": v.token})
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["x-content-type-options"] == "nosniff"
    other = Visitor(client, key)
    assert client.get(f"{other.u}/files/{f.json()['id']}", params={"s": other.token}).status_code == 404
    assert other.say("stealing", attachments=[f.json()["id"]]).status_code == 404
    staff = client.get(f"{base(b)}/files/{f.json()['id']}", headers=b["agent"]["h"])
    assert staff.status_code == 200 and staff.content == PNG


def test_offline_form_callbacks_hours_and_mode(client):
    b = business(client)
    closed = {d: None for d in widget.DAYS}
    key = _widget_key(client, b, settings={"hours": closed})
    cfg = client.get(f"/api/v1/commai/widget/{key['public_key']}/config", headers={"Origin": SITE}).json()
    assert cfg["online"] is False and "closed" in cfg["hours"]
    r = client.patch(f"{base(b)}/settings", json={"mode": "human_only"}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    cfg = client.get(f"/api/v1/commai/widget/{key['public_key']}/config", headers={"Origin": SITE}).json()
    assert cfg["mode"] == "human_only" and cfg["ai"] is False

    v = Visitor(client, key)
    r = client.post(
        f"{v.u}/offline",
        json={"name": "Ana", "email": "Ana@Example.org", "message": "Call me", "client_id": "offline-0001"},
        headers=v.h(),
    )
    assert r.status_code == 201, r.text
    conv = _conv_for(b, "email")
    assert conv["subject"].startswith("Message from your website")
    r = client.post(
        f"{v.u}/callback",
        json={"name": "Ana", "phone": "+1 (868) 555-0199", "when": "after 4pm", "client_id": "callback-0001"},
        headers=v.h(),
    )
    assert r.status_code == 201, r.text
    cb = client.get(f"{base(b)}/conversations/{r.json()['conversation_id']}", headers=b["agent"]["h"]).json()
    assert cb["tags"] == ["callback"] and "+18685550199" in cb["messages"][0]["body"]
    assert cb["contact_phone"] == "+18685550199"

    assert widget.open_now(
        {"settings": {"hours": {"mon": ["09:00", "17:00"]}}}, "UTC", dt.datetime(2026, 10, 5, 10, 0, tzinfo=dt.UTC)
    )
    assert not widget.open_now(
        {"settings": {"hours": {"mon": ["09:00", "17:00"]}}}, "UTC", dt.datetime(2026, 10, 5, 17, 0, tzinfo=dt.UTC)
    )


# ---- SMS -------------------------------------------------------------------------------------------------


def test_sms_opt_out_words_and_daily_country_limits(client):
    b = business(client)
    u = base(b)
    switch_on("country", "TT", b["id"])  # countries start off (ADR 0029)
    sms = _account(client, b, "sms", "+18685550100", settings={"daily_limits": {"TT": 1, "*": 5}})
    _wa_inbound(client, sms, "What's my balance?", "SM1")
    conv = _conv_for(b, "sms")
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "One"}, headers=b["agent"]["h"])
    assert r.status_code == 201
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Two"}, headers=b["agent"]["h"])
    assert r.status_code == 422 and "limit for TT" in r.json()["detail"]

    _wa_inbound(client, sms, "stop", "SM2")
    with db.tx() as conn:
        ident = conn.execute(
            "SELECT opted_out FROM contact_identities WHERE id = %s", (conv["identity_id"],)
        ).fetchone()
    assert ident["opted_out"]
    client.patch(
        f"{u}/channel-accounts/{sms['id']}", json={"settings": {"daily_limits": {"*": 100}}}, headers=b["agent"]["h"]
    )
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Three"}, headers=b["agent"]["h"])
    assert r.status_code == 422 and "opted out" in r.json()["detail"]
    _wa_inbound(client, sms, "START", "SM3")
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Welcome back"}, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text

    assert messaging.country_of("+18685550100") == "TT"
    assert messaging.country_of("+18765550100") == "JM"
    assert messaging.country_of("+12125550100") == "US/CA"
    assert messaging.country_of("+5926000000") == "GY"
    assert messaging.country_of("+447700900000") == "GB"


# ---- email ---------------------------------------------------------------------------------------------------


def test_email_threading_secret_formats_and_unsubscribe(client, monkeypatch):
    monkeypatch.setenv("EXA_PUBLIC_URL", "https://connect.example")
    b = business(client)
    u = base(b)
    em = _account(client, b, "email", "help@examplebank.tt", settings={"from_name": "Example Bank"})
    path = "/api/v1" + em["webhook_url"].split("/api/v1", 1)[1]
    mail = {
        "from": "Ana <ana@example.org>",
        "subject": "Lost card",
        "text": "I lost my card",
        "message_id": "<first@example.org>",
    }
    assert client.post(path, json=mail, headers={"X-Exa-Email-Secret": "wrong"}).status_code == 403
    assert client.post(path, json=mail).status_code == 403
    assert client.post(path, json=mail, headers={"X-Exa-Email-Secret": em["secret"]}).status_code == 200
    conv = _conv_for(b, "email")
    assert conv["subject"] == "Lost card"

    client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "We've blocked it."}, headers=b["agent"]["h"])
    run_jobs()
    with db.tx() as conn:
        out = conn.execute("SELECT * FROM sim_channel_outbox WHERE channel = 'email'").fetchone()
    hdr = out["headers"]
    assert hdr["Subject"] == "Re: Lost card" and hdr["In-Reply-To"] == "<first@example.org>"
    assert "Example Bank" in hdr["From"] and hdr["Message-ID"].endswith("@examplebank.tt>")
    assert "https://connect.example/api/v1/commai/channels/email/" in hdr["List-Unsubscribe"]

    # Ana's reply (Mailgun's form format, secret in the URL) threads into the same conversation.
    form = {
        "sender": "ana@example.org",
        "from": "Ana <ana@example.org>",
        "subject": "Re: Lost card",
        "body-plain": "Thanks!\n> quoted",
        "stripped-text": "Thanks!",
        "Message-Id": "<second@example.org>",
        "In-Reply-To": hdr["Message-ID"],
        "References": f"<first@example.org> {hdr['Message-ID']}",
    }
    r = client.post(f"{path}?secret={em['secret']}", data=form)
    assert r.status_code == 200, r.text
    with db.tx() as conn:
        msgs = conn.execute(
            "SELECT body FROM messages WHERE conversation_id = %s AND direction = 'in' ORDER BY created_at",
            (conv["id"],),
        ).fetchall()
    assert [m["body"] for m in msgs] == ["I lost my card", "Thanks!"]

    # The List-Unsubscribe link opts the address out; after 24 hours of silence, replies stop.
    link = hdr["List-Unsubscribe"].split("<", 1)[1].split(">", 1)[0].replace("https://connect.example", "")
    assert client.post(link).status_code == 200
    assert client.get(link.rsplit(".", 1)[0] + ".AAAA").status_code == 404
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Within a day"}, headers=b["agent"]["h"])
    assert r.status_code == 201
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversations SET last_inbound_at = now() - interval '2 days' WHERE id = %s", (conv["id"],)
        )
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Later"}, headers=b["agent"]["h"])
    assert r.status_code == 422 and "unsubscribed" in r.json()["detail"]


# ---- real provider adapters (offline: signatures and parsing only) --------------------------------------------


def test_twilio_webhook_signature_and_parsing(client, monkeypatch):
    monkeypatch.setenv("EXA_PUBLIC_URL", "https://connect.example")
    monkeypatch.setenv("EXA_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("EXA_TWILIO_AUTH_TOKEN", "twilio-test-token")
    b = business(client)
    acct = _account(client, b, "whatsapp", "+18685550100", provider="twilio")
    assert acct["status"] == "setup" and acct["missing_env"] == []
    path = "/api/v1" + acct["webhook_url"].split("/api/v1", 1)[1]
    form = {
        "MessageSid": "SM123",
        "From": "whatsapp:+18685550101",
        "To": "whatsapp:+18685550100",
        "Body": "Hello from Twilio",
        "ProfileName": "Ana",
        "NumMedia": "0",
    }
    sig = providers.Twilio.signature("twilio-test-token", f"https://connect.example{path}", form)
    assert client.post(path, data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    for _ in range(2):
        r = client.post(path, data=form, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200 and r.text == "<Response/>"
    conv = _conv_for(b, "whatsapp")
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM messages WHERE conversation_id = %s", (conv["id"],)).fetchone()
        log = conn.execute("SELECT outcome FROM channel_webhook_log ORDER BY id").fetchall()
    assert n["n"] == 1 and [x["outcome"] for x in log] == ["rejected", "accepted", "accepted"]
    # Still in setup: replies are refused with a clear reason, not sent.
    r = client.post(f"{base(b)}/conversations/{conv['id']}/messages", json={"body": "Hi"}, headers=b["agent"]["h"])
    assert r.status_code == 422 and "still being set up" in r.json()["detail"]

    receipt = providers.Twilio().parse(
        acct,
        providers.InboundRequest(
            "", {}, b"", {"MessageSid": "SM9", "MessageStatus": "undelivered", "ErrorCode": "63016"}
        ),
    )
    assert receipt == [providers.Receipt("SM9", "failed", "Twilio error 63016")]
    # credentials_env can't point the adapter at another service's secret.
    r = client.patch(
        f"{base(b)}/channel-accounts/{acct['id']}",
        json={"settings": {"credentials_env": "EXA_LLM"}},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 422


def test_360dialog_signature_and_cloud_api_parsing(monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setenv("EXA_360DIALOG_WEBHOOK_SECRET", "meta-secret")
    acct = {"settings": {}, "channel": "whatsapp"}
    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"wa_id": "18685550101", "profile": {"name": "Ana"}}],
                                "messages": [
                                    {"from": "18685550101", "id": "wamid.X", "type": "text", "text": {"body": "Hi"}}
                                ],
                                "statuses": [
                                    {"id": "wamid.OUT", "status": "read"},
                                    {
                                        "id": "wamid.F",
                                        "status": "failed",
                                        "errors": [{"code": 131047, "title": "Re-engagement message"}],
                                    },
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    p = providers.Dialog360()
    good = "sha256=" + hmac.new(b"meta-secret", body, hashlib.sha256).hexdigest()
    assert p.verify(acct, providers.InboundRequest("", {"x-hub-signature-256": good}, body))
    assert not p.verify(acct, providers.InboundRequest("", {"x-hub-signature-256": "sha256=00"}, body))
    items = p.parse(acct, providers.InboundRequest("", {}, body))
    assert items[0] == providers.Inbound("+18685550101", "Hi", "wamid.X", "Ana", [])
    assert items[1] == providers.Receipt("wamid.OUT", "read", "")
    assert items[2].status == "failed" and "131047" in items[2].error
    assert p.missing(acct) == ["EXA_360DIALOG_API_KEY"]
    assert providers.match_params("Hi {{1}}, see you {{2}}.", "Hi Ana, see you at 10.") == ["Ana", "at 10"]
    assert providers.match_params("Hi {{1}}.", "Hello Ana.") is None


# ---- diagnostics ---------------------------------------------------------------------------------------------


def test_diagnostics_explain_why_whatsapp_stopped_sending(client):
    b = business(client)
    u = base(b)
    diag = client.get(f"{u}/channels/diagnostics", headers=b["agent"]["h"]).json()
    assert any(d["area"] == "whatsapp" and "No WhatsApp account" in d["summary"] for d in diag)

    wa = _account(client, b, "whatsapp", "+18685550100", settings={"fail_sends": True})
    _wa_inbound(client, wa, "Hi", "wamid.d1")
    _hook(client, wa, {"messages": []}, secret="wrong")  # a misconfigured webhook
    conv = _conv_for(b, "whatsapp")
    client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Hello"}, headers=b["agent"]["h"])
    run_jobs()
    client.patch(f"{u}/channel-accounts/{wa['id']}", json={"status": "paused"}, headers=b["agent"]["h"])

    diag = [
        d for d in client.get(f"{u}/channels/diagnostics", headers=b["agent"]["h"]).json() if d["area"] == "whatsapp"
    ]
    text = json.dumps(diag)
    assert any(d["status"] == "problem" and "paused" in d["summary"] for d in diag)
    assert "Simulated failure" in text  # the provider's last error, with its words
    assert "refused in the last 24 hours" in text
    assert any("no approved WhatsApp template" in d["summary"] for d in diag)
    fix = next(d["fix"] for d in diag if "paused" in d["summary"])
    assert fix["id"] == "set_account_status" and fix["params"]["status"] == "live"
