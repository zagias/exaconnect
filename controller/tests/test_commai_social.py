"""Messenger, Instagram and Telegram (ADR 0029): go-live gate, signatures, window
and tag rules, receipts, opt-out, secure token entry, notes and tenant isolation."""

from __future__ import annotations

import datetime as dt
import json
import secrets

from cryptography.fernet import Fernet

from exaconnect_controller import db
from exaconnect_controller.commai import golive, inbox
from exaconnect_controller.commai.channels import social

from .commai_helpers import base, business, run_jobs, switch_on

PAGE = "1029384756"
IG = "17841400000000"


def _create(client, b, channel, address, simulated=True, who="agent"):
    return client.post(
        f"{base(b)}/social-accounts",
        json={"channel": channel, "address": address, "simulated": simulated, "name": channel},
        headers=b[who]["h"],
    )


def _account(client, b, channel, address, simulated=True):
    r = _create(client, b, channel, address, simulated)
    assert r.status_code == 201, r.text
    return r.json()


def _row(acct_id):
    with db.tx() as conn:
        return conn.execute("SELECT * FROM channel_accounts WHERE id = %s", (acct_id,)).fetchone()


def _path(acct):
    return "/api/v1" + acct["webhook_url"].split("/api/v1", 1)[1]


def _post_meta(client, acct, payload, secret=None, sig=True):
    raw = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    if sig:
        h["X-Hub-Signature-256"] = social.meta_sign(secret or acct["secret"], raw)
    return client.post(_path(acct), content=raw, headers=h)


def _post_tg(client, acct, update, secret=None, header=True):
    h = {"Content-Type": "application/json"}
    if header:
        h["X-Telegram-Bot-Api-Secret-Token"] = secret or acct["secret"]
    return client.post(_path(acct), content=json.dumps(update).encode(), headers=h)


def _in(client, b, acct, sender, text, mid=None):
    r = client.post(
        f"{base(b)}/social-accounts/{acct['id']}/simulate-inbound",
        json={"from": sender, "body": text, "name": "Ana Lee", "id": mid or ""},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text
    return r.json()


def _conv(b, channel):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM conversations WHERE customer_id = %s AND channel = %s ORDER BY created_at DESC LIMIT 1",
            (b["id"], channel),
        ).fetchone()


def _age(conv_id, days):
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversations SET last_inbound_at = now() - %s WHERE id = %s", (dt.timedelta(days=days), conv_id)
        )


def _reply(client, b, conv, body, template=""):
    return client.post(
        f"{base(b)}/conversations/{conv['id']}/messages",
        json={"body": body, "template": template, "take_over": True},
        headers=b["agent"]["h"],
    )


def _outbox(b, channel):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM sim_channel_outbox WHERE customer_id = %s AND channel = %s ORDER BY id", (b["id"], channel)
        ).fetchall()


def _tg_update(chat, text, mid, kind="private"):
    return {
        "update_id": mid,
        "message": {
            "message_id": mid,
            "from": {"id": chat, "first_name": "Ana"},
            "chat": {"id": chat, "type": kind},
            "date": 1,
            "text": text,
        },
    }


# ---- go-live gate ------------------------------------------------------------------------------


def test_channels_refuse_to_connect_until_switched_on_for_the_business(client):
    b = business(client)
    other = business(client, "Other Bank", ("agent",))
    for ch, addr in (("messenger", PAGE), ("instagram", IG), ("telegram", "@ExampleBankBot")):
        r = _create(client, b, ch, addr)
        assert r.status_code == 409 and "not switched on" in r.json()["detail"], r.text
    listing = client.get(f"{base(b)}/social-accounts", headers=b["agent"]["h"]).json()
    assert listing["channels"]["telegram"]["available"] is False
    assert any("BotFather" in n for n in listing["channels"]["telegram"]["needs"])

    switch_on("channel", "telegram", b["id"])  # a pilot for b only
    tg = _account(client, b, "telegram", "@ExampleBankBot")
    assert tg["status"] == "live" and tg["simulated"]
    assert _create(client, other, "telegram", "@OtherBankBot").status_code == 409

    _in(client, b, tg, "5551001", "Hello")
    # Switched off again (a criterion stops holding): webhooks and sends are refused.
    with db.tx() as conn:
        golive.check(conn, "channel", "telegram", "webhook-secret", False, "", "test")
    r = _post_tg(client, tg, _tg_update(5551001, "Again", 2))
    assert r.status_code == 403 and "not switched on" in r.json()["detail"]
    r = _reply(client, b, _conv(b, "telegram"), "Hi")
    assert r.status_code == 422 and "not switched on" in r.json()["detail"]


# ---- signatures ------------------------------------------------------------------------------------


def test_meta_signatures_good_bad_missing_and_verify_handshake(client):
    b = business(client)
    switch_on("channel", "messenger")
    switch_on("channel", "instagram")
    ms = _account(client, b, "messenger", PAGE)
    ig = _account(client, b, "instagram", IG)
    for acct in (ms, ig):
        payload = social.sim_payload(_row(acct["id"]), "PSID-1", "Hello there", f"m_{acct['channel']}_1")
        assert _post_meta(client, acct, payload, sig=False).status_code == 403
        assert _post_meta(client, acct, payload, secret="wrong-secret").status_code == 403
        tampered = json.loads(json.dumps(payload))
        r = client.post(
            _path(acct),
            content=json.dumps(tampered).encode() + b" ",
            headers={"X-Hub-Signature-256": social.meta_sign(acct["secret"], json.dumps(payload).encode())},
        )
        assert r.status_code == 403
        r = _post_meta(client, acct, payload)
        assert r.status_code == 200 and r.json()["received"] == 1, r.text
        r = _post_meta(client, acct, payload)
        assert r.json()["duplicates"] == 1 and r.json()["received"] == 0
    with db.tx() as conn:
        rejected = conn.execute(
            "SELECT count(*) AS n FROM channel_webhook_log WHERE account_id = %s AND outcome = 'rejected'", (ms["id"],)
        ).fetchone()["n"]
    assert rejected == 3

    # Meta's subscription handshake.
    q = {"hub.mode": "subscribe", "hub.challenge": "1158201444", "hub.verify_token": ms["secret"]}
    r = client.get(_path(ms), params=q)
    assert r.status_code == 200 and r.text == "1158201444"
    assert client.get(_path(ms), params={**q, "hub.verify_token": "nope"}).status_code == 403
    # The generic phase 2 webhook path refuses these accounts' traffic.
    raw = json.dumps(social.sim_payload(_row(ms["id"]), "PSID-1", "x", "m_x")).encode()
    r = client.post(
        f"/api/v1/commai/channels/hooks/{_row(ms['id'])['hook_token']}",
        content=raw,
        headers={"X-Hub-Signature-256": social.meta_sign(ms["secret"], raw)},
    )
    assert r.status_code == 400


def test_meta_app_wide_webhook_routes_by_page_and_fails_closed(client, admin_headers, monkeypatch):
    b = business(client)
    other = business(client, "Other Bank", ("agent",))
    switch_on("channel", "messenger")
    real = _account(client, b, "messenger", PAGE, simulated=False)
    assert real["status"] == "setup" and real["webhook_url"].endswith("/channels/meta/webhook")
    # A real Page connects to one business only.
    assert _create(client, other, "messenger", PAGE, simulated=False).status_code == 409

    payload = {
        "object": "page",
        "entry": [
            {"id": PAGE, "messaging": [{"sender": {"id": "P1"}, "message": {"mid": "m_real_1", "text": "Hi"}}]},
            {"id": "999999999", "messaging": [{"sender": {"id": "P2"}, "message": {"mid": "m_x", "text": "?"}}]},
        ],
    }
    raw = json.dumps(payload).encode()
    url = "/api/v1/commai/channels/meta/webhook"
    monkeypatch.delenv("EXA_META_APP_SECRET", raising=False)
    assert client.post(url, content=raw, headers={"X-Hub-Signature-256": social.meta_sign("x", raw)}).status_code == 403
    app_secret = secrets.token_hex(16)
    monkeypatch.setenv("EXA_META_APP_SECRET", app_secret)
    assert client.post(url, content=raw).status_code == 403
    assert client.post(url, content=raw, headers={"X-Hub-Signature-256": social.meta_sign("x", raw)}).status_code == 403
    r = client.post(url, content=raw, headers={"X-Hub-Signature-256": social.meta_sign(app_secret, raw)})
    assert r.status_code == 200 and r.json()["received"] == 1 and r.json()["unknown_accounts"] == 1, r.text
    assert _conv(b, "messenger") is not None and _conv(other, "messenger") is None

    verify = os_token = secrets.token_hex(8)
    monkeypatch.setenv("EXA_META_VERIFY_TOKEN", os_token)
    r = client.get(url, params={"hub.mode": "subscribe", "hub.challenge": "42", "hub.verify_token": verify})
    assert r.status_code == 200 and r.text == "42"
    assert client.get(url, params={"hub.mode": "subscribe", "hub.challenge": "42"}).status_code == 403


def test_telegram_secret_header_opt_out_and_private_chats_only(client):
    b = business(client)
    switch_on("channel", "telegram")
    tg = _account(client, b, "telegram", "@ExampleBankBot")
    assert _post_tg(client, tg, _tg_update(42, "Hello", 1), header=False).status_code == 403
    assert _post_tg(client, tg, _tg_update(42, "Hello", 1), secret="chs_wrong").status_code == 403
    r = _post_tg(client, tg, _tg_update(42, "Hello", 1))
    assert r.status_code == 200 and r.json()["received"] == 1
    assert _post_tg(client, tg, _tg_update(42, "Hello", 1)).json()["duplicates"] == 1  # Telegram retries
    assert _post_tg(client, tg, _tg_update(-100, "group", 2, kind="group")).json()["received"] == 0

    conv = _conv(b, "telegram")
    assert _reply(client, b, conv, "Hello Ana").status_code == 201
    assert _reply(client, b, conv, "x", template="tag:HUMAN_AGENT").status_code == 422  # no tags on Telegram

    _post_tg(client, tg, _tg_update(42, "/stop", 3))
    r = _reply(client, b, conv, "Still there?")
    assert r.status_code == 422 and "opted out" in r.json()["detail"]
    _post_tg(client, tg, _tg_update(42, "/start", 4))
    assert _reply(client, b, conv, "Welcome back").status_code == 201
    # Blocking the bot opts out too.
    blocked = {
        "update_id": 9,
        "my_chat_member": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "first_name": "Ana"},
            "new_chat_member": {"status": "kicked"},
        },
    }
    assert _post_tg(client, tg, blocked).json()["opt_changes"] == 1
    assert _reply(client, b, conv, "Hello?").status_code == 422
    # No window: a month-old conversation can still be answered once they come back.
    _post_tg(client, tg, _tg_update(42, "/start", 5))
    _age(conv["id"], 30)
    assert _reply(client, b, conv, "Any time").status_code == 201
    run_jobs()
    sent = [r["body"] for r in _outbox(b, "telegram")]
    assert sent == ["Hello Ana", "Welcome back", "Any time"]


# ---- window, tags, receipts -------------------------------------------------------------------------


def test_meta_window_and_message_tags(client):
    b = business(client)
    switch_on("channel", "messenger")
    switch_on("channel", "instagram")
    ms = _account(client, b, "messenger", PAGE)
    ig = _account(client, b, "instagram", IG)
    _in(client, b, ms, "PSID-9", "Where is my card?")
    conv = _conv(b, "messenger")
    assert _reply(client, b, conv, "On its way").status_code == 201
    run_jobs()  # a queued reply is checked again when it goes out, so send each one now

    _age(conv["id"], 3)
    r = _reply(client, b, conv, "Following up")
    assert r.status_code == 422 and "24-hour" in r.json()["detail"] and "HUMAN_AGENT" in r.json()["detail"]
    assert _reply(client, b, conv, "Following up", template="welcome_v1").status_code == 422
    assert _reply(client, b, conv, "x", template="tag:MARKETING").status_code == 422
    assert _reply(client, b, conv, "Your card shipped", template="tag:HUMAN_AGENT").status_code == 201
    run_jobs()
    _age(conv["id"], 8)
    r = _reply(client, b, conv, "Late", template="tag:HUMAN_AGENT")
    assert r.status_code == 422 and "7 days" in r.json()["detail"]
    assert _reply(client, b, conv, "Statement ready", template="tag:ACCOUNT_UPDATE").status_code == 201
    run_jobs()
    out = _outbox(b, "messenger")
    assert [(o["body"], o["template"]) for o in out] == [
        ("On its way", ""),
        ("Your card shipped", "HUMAN_AGENT"),
        ("Statement ready", "ACCOUNT_UPDATE"),
    ]

    # Instagram: only the human agent tag.
    _in(client, b, ig, "IGSID-1", "Hi")
    iconv = _conv(b, "instagram")
    _age(iconv["id"], 2)
    assert _reply(client, b, iconv, "x", template="tag:ACCOUNT_UPDATE").status_code == 422
    assert _reply(client, b, iconv, "Sorry for the wait", template="tag:HUMAN_AGENT").status_code == 201

    # The AI may never use a tag: refused when the queued message is delivered.
    with db.tx() as conn:
        conn.execute("UPDATE conversations SET handler = 'ai' WHERE id = %s", (iconv["id"],))
        msg = inbox.send(
            conn, b["id"], iconv["id"], "Automated nudge", author_kind="ai", author="ai", template="tag:HUMAN_AGENT"
        )
    run_jobs()
    with db.tx() as conn:
        m = conn.execute("SELECT status, error FROM messages WHERE id = %s", (msg["id"],)).fetchone()
    assert m["status"] == "blocked" and "not the AI" in m["error"]
    assert [o["body"] for o in _outbox(b, "instagram")] == ["Sorry for the wait"]


def test_receipts_and_stop_words(client):
    b = business(client)
    switch_on("channel", "messenger")
    switch_on("channel", "instagram")
    switch_on("channel", "telegram")
    ms = _account(client, b, "messenger", PAGE)
    ig = _account(client, b, "instagram", IG)
    tg = _account(client, b, "telegram", "@ExampleBankBot")
    u = base(b)

    _in(client, b, ms, "PSID-5", "Hello")
    conv = _conv(b, "messenger")
    one = _reply(client, b, conv, "First").json()
    two = _reply(client, b, conv, "Second").json()
    run_jobs()
    r = client.post(
        f"{u}/social-accounts/{ms['id']}/simulate-receipt",
        json={"message_id": one["id"], "status": "delivered"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200 and r.json()["receipts"] >= 1
    # Messenger's read watermark: everything sent before it is read.
    client.post(
        f"{u}/social-accounts/{ms['id']}/simulate-receipt",
        json={"message_id": two["id"], "status": "read"},
        headers=b["agent"]["h"],
    )
    with db.tx() as conn:
        st = {r["body"]: r["status"] for r in conn.execute(
            "SELECT body, status FROM messages WHERE conversation_id = %s AND direction = 'out'", (conv["id"],)
        ).fetchall()}  # fmt: skip
    assert st == {"First": "read", "Second": "read"}

    _in(client, b, ig, "IGSID-5", "Hi")
    im = _reply(client, b, _conv(b, "instagram"), "Hello from us").json()
    run_jobs()
    r = client.post(
        f"{u}/social-accounts/{ig['id']}/simulate-receipt",
        json={"message_id": im["id"], "status": "delivered"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 409  # Instagram has read receipts only
    r = client.post(
        f"{u}/social-accounts/{ig['id']}/simulate-receipt",
        json={"message_id": im["id"], "status": "read"},
        headers=b["agent"]["h"],
    )
    assert r.json()["receipts"] == 1
    r = client.post(
        f"{u}/social-accounts/{tg['id']}/simulate-receipt",
        json={"message_id": im["id"], "status": "read"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 409 and "no delivery or read receipts" in r.json()["detail"]

    # STOP on Messenger opts out.
    _in(client, b, ms, "PSID-5", "STOP")
    r = _reply(client, b, conv, "Bye?")
    assert r.status_code == 422 and "opted out" in r.json()["detail"]


def test_internal_notes_never_go_out_on_the_new_channels(client):
    b = business(client)
    for ch in ("messenger", "instagram", "telegram"):
        switch_on("channel", ch)
    accts = {
        "messenger": _account(client, b, "messenger", PAGE),
        "instagram": _account(client, b, "instagram", IG),
        "telegram": _account(client, b, "telegram", "@ExampleBankBot"),
    }
    secret_note = "INTERNAL: customer flagged for fraud review"
    for ch, acct in accts.items():
        _in(client, b, acct, "77001" if ch == "telegram" else "SID-77", "Hello")
        conv = _conv(b, ch)
        r = client.post(
            f"{base(b)}/conversations/{conv['id']}/notes", json={"body": secret_note}, headers=b["agent"]["h"]
        )
        assert r.status_code == 201, r.text
        assert _reply(client, b, conv, f"Reply on {ch}").status_code == 201
    run_jobs()
    with db.tx() as conn:
        rows = conn.execute("SELECT * FROM sim_channel_outbox WHERE customer_id = %s", (b["id"],)).fetchall()
    assert len(rows) == 3
    assert all(secret_note not in json.dumps(r, default=str) for r in rows)


# ---- secure token entry -------------------------------------------------------------------------------


def test_bot_token_secure_entry_is_never_echoed(client, monkeypatch):
    b = business(client)
    switch_on("channel", "telegram")
    tg = _account(client, b, "telegram", "@RealBankBot", simulated=False)
    assert tg["status"] == "setup" and "secret" not in tg and tg["token_saved"] is False
    u = f"{base(b)}/social-accounts/{tg['id']}"
    token = f"{secrets.randbelow(10**9) + 10**8}:" + secrets.token_urlsafe(30)[:35]

    monkeypatch.delenv("EXA_SECRETS_KEY", raising=False)
    r = client.put(f"{u}/token", json={"token": token}, headers=b["agent"]["h"])
    assert r.status_code == 409 and "EXA_SECRETS_KEY" in r.json()["detail"]
    monkeypatch.setenv("EXA_SECRETS_KEY", Fernet.generate_key().decode())
    assert client.put(f"{u}/token", json={"token": "not-a-token-at-all"}, headers=b["agent"]["h"]).status_code == 422
    assert client.put(f"{u}/token", json={"token": token}, headers=b["internal"]["h"]).status_code == 403
    r = client.put(f"{u}/token", json={"token": token}, headers=b["agent"]["h"])
    assert r.status_code == 204 and r.text == ""

    listing = client.get(f"{base(b)}/social-accounts", headers=b["agent"]["h"])
    assert token not in listing.text and listing.json()["accounts"][0]["token_saved"] is True
    shared = client.get(f"{base(b)}/channel-accounts", headers=b["agent"]["h"])
    assert token not in shared.text
    with db.tx() as conn:
        audit_rows = conn.execute("SELECT * FROM audit_log").fetchall()
        secrets_rows = conn.execute("SELECT ciphertext FROM commai_secrets").fetchall()
        acct = conn.execute("SELECT * FROM channel_accounts WHERE id = %s", (tg["id"],)).fetchone()
        assert social.token_for(conn, acct) == token
    assert token not in json.dumps(audit_rows, default=str)
    assert all(token not in r["ciphertext"] for r in secrets_rows)

    # Live needs a connection; connecting calls Telegram (stubbed: no network in tests).
    r = client.patch(u, json={"status": "live"}, headers=b["agent"]["h"])
    assert r.status_code == 409 and "Connect" in r.json()["detail"]
    calls = []

    def fake_call(self, tok, method, payload):
        calls.append((tok == token, method, payload.get("secret_token") == _row(tg["id"])["hook_secret"]))
        return {"username": "RealBankBot"} if method == "getMe" else True

    monkeypatch.setattr(social.Telegram, "_call", fake_call)
    r = client.post(f"{u}/connect", headers=b["agent"]["h"])
    assert r.status_code == 200 and token not in r.text, r.text
    assert calls == [(True, "getMe", False), (True, "setWebhook", True)]
    assert client.patch(u, json={"status": "live"}, headers=b["agent"]["h"]).json()["status"] == "live"

    # Removing the account removes the stored token.
    assert client.delete(u, headers=b["agent"]["h"]).status_code == 204
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM commai_secrets").fetchone()["n"] == 0


# ---- tenant isolation ------------------------------------------------------------------------------------


def test_new_endpoints_are_tenant_isolated(client, admin_headers):
    a = business(client, "Bank A", ("agent",))
    z = business(client, "Bank Z", ("agent",))
    switch_on("channel", "telegram")
    tg = _account(client, a, "telegram", "@BankABot")
    _in(client, a, tg, "123", "Hello")
    conv = _conv(a, "telegram")
    msg = _reply(client, a, conv, "Hi").json()

    zh = z["agent"]["h"]
    assert client.get(f"{base(a)}/social-accounts", headers=zh).status_code == 403
    assert client.get(f"{base(a)}/countries", headers=zh).status_code == 403
    assert client.get(f"{base(a)}/sms-senders", headers=zh).status_code == 403
    assert client.get(f"{base(a)}/sms-route-attempts", headers=zh).status_code == 403
    # Z's own path with A's ids finds nothing.
    zu = f"{base(z)}/social-accounts/{tg['id']}"
    assert client.patch(zu, json={"status": "paused"}, headers=zh).status_code == 404
    assert client.put(f"{zu}/token", json={"token": "1234567:" + "a" * 35}, headers=zh).status_code == 404
    assert client.post(f"{zu}/simulate-inbound", json={"from": "1", "body": "x"}, headers=zh).status_code == 404
    r = client.post(f"{zu}/simulate-receipt", json={"message_id": msg["id"], "status": "read"}, headers=zh)
    assert r.status_code == 404
    assert client.delete(zu, headers=zh).status_code == 404
    assert client.get(f"{base(z)}/social-accounts", headers=zh).json()["accounts"] == []
    # ExaCarib admin endpoints are for admins only.
    for path in ("/sms-routes", "/sms-carriers", "/sms-senders"):
        assert client.get(f"/api/v1/commai{path}", headers=zh).status_code == 403
        assert client.get(f"/api/v1/commai{path}", headers=admin_headers).status_code == 200
    r = client.put("/api/v1/commai/countries/TT/sms-rules", json={"rate_per_minute": 5}, headers=zh)
    assert r.status_code == 403
