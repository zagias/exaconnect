"""Typing and presence in the inbox and the widget, and the website visitor's
AI browser call (ADR 0032)."""

from exaconnect_controller import db

from .commai_helpers import base
from .test_commai_ai import ai_business
from .test_commai_channels import SITE, Visitor, _conv_for, _widget_key


def test_typing_and_presence(client):
    from .commai_helpers import business

    b = business(client)
    key = _widget_key(client, b)
    v = Visitor(client, key)
    v.say("Hello, anyone there?")
    conv = dict(_conv_for(b, "web"))
    conv["id"] = str(conv["id"])
    u = f"{base(b)}/conversations/{conv['id']}"

    # Nobody typing yet.
    r = client.get(f"{v.u}/typing", params={"conversation_id": conv["id"]}, headers=v.h())
    assert r.status_code == 200 and r.json() == {"team_typing": False}

    # Staff start typing: the visitor sees "team typing", never who.
    r = client.post(f"{u}/typing", json={"typing": True}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    assert r.json()["typing"] == [{"who_kind": "user", "name": b["agent"]["email"]}]
    r = client.get(f"{v.u}/typing", params={"conversation_id": conv["id"]}, headers=v.h())
    assert r.json() == {"team_typing": True}
    assert b["agent"]["email"] not in r.text

    # The visitor types: staff see a customer typing, and a second person sees who else is here.
    r = client.post(f"{v.u}/typing", json={"conversation_id": conv["id"], "typing": True}, headers=v.h())
    assert r.status_code == 200 and r.json()["team_typing"] is True
    p = client.get(f"{u}/presence", headers=b["agent2"]["h"]).json()
    assert {"who_kind": "contact", "name": "Customer"} in p["typing"]
    assert p["viewing"] == [{"name": b["agent"]["email"]}]

    # Stopping clears it at once.
    client.post(f"{u}/typing", json={"typing": False}, headers=b["agent"]["h"])
    assert client.get(f"{v.u}/typing", params={"conversation_id": conv["id"]}, headers=v.h()).json() == {
        "team_typing": False
    }
    # Typing is never a recorded event (webhooks don't see it).
    with db.tx() as conn:
        assert not conn.execute(
            "SELECT 1 FROM commai_events WHERE customer_id = %s AND type LIKE '%%typing%%'", (b["id"],)
        ).fetchone()

    # Another visitor can't read or set typing on this conversation.
    other = Visitor(client, key)
    r = client.get(f"{other.u}/typing", params={"conversation_id": conv["id"]}, headers=other.h())
    assert r.status_code == 404

    # The inbox live feed carries presence.
    with client.websocket_connect(f"/api/v1/commai/customers/{b['id']}/live") as ws:
        ws.send_json({"token": b["agent"]["h"]["Authorization"].split()[1]})
        assert ws.receive_json()["type"] == "ready"
        client.post(f"{v.u}/typing", json={"conversation_id": conv["id"], "typing": True}, headers=v.h())
        msg = ws.receive_json()
        assert msg["type"] == "presence" and msg["data"]["typing"] is True
        assert msg["data"]["name"] == "Customer" and msg["data"]["conversation_id"] == str(conv["id"])


def test_visitor_ai_call_from_the_widget(client):
    b = ai_business(client)
    key = _widget_key(client, b)
    v = Visitor(client, key)
    u = f"/api/v1/commai/widget/{key['public_key']}/calls"
    # Off until the business switches it on for the widget.
    assert client.get(f"{v.u}/config", headers={"Origin": SITE}).json()["ai_calls"] is False
    r = client.post(u, json={}, headers=v.h())
    assert r.status_code == 403
    r = client.patch(
        f"{base(b)}/widget-keys/{key['id']}", json={"settings": {"ai_calls": True}}, headers=b["agent"]["h"]
    )
    assert r.status_code == 200, r.text
    assert client.get(f"{v.u}/config", headers={"Origin": SITE}).json()["ai_calls"] is True

    # Needs a session and an allowed website.
    assert client.post(u, json={}, headers={"Origin": SITE}).status_code == 401
    assert (
        client.post(u, json={}, headers={"Origin": "https://evil.example", "X-Widget-Session": v.token}).status_code
        == 403
    )

    r = client.post(u, json={}, headers=v.h())
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["handler"] == "ai" and call["greeting"]
    cid = call["conversation_id"]
    r = client.post(f"{u}/{cid}/turns", json={"text": "When are you open on Saturday?"}, headers=v.h())
    assert r.status_code == 200, r.text
    assert "9am to 1pm" in r.json()["reply"] and r.json()["handed_over"] is False

    # Another visitor can't drive this call.
    other = Visitor(client, key)
    r = client.post(f"{u}/{cid}/turns", json={"text": "hello"}, headers=other.h())
    assert r.status_code == 404

    # The call sits on the visitor's own contact, so staff see it in their history.
    with db.tx() as conn:
        contact = conn.execute(
            """SELECT ci.contact_id FROM contact_identities ci WHERE ci.customer_id = %s AND ci.channel = 'web'""",
            (b["id"],),
        ).fetchone()["contact_id"]
    h = client.get(f"{base(b)}/contacts/{contact}/history", headers=b["agent"]["h"]).json()
    assert [c["conversation_id"] for c in h["calls"]] == [cid]

    r = client.post(f"{u}/{cid}/end", json={}, headers=v.h())
    assert r.status_code == 200 and r.json()["ended"] is True
    assert client.post(f"{u}/{cid}/turns", json={"text": "hello?"}, headers=v.h()).status_code == 409

    # Rate limited per visitor: 3 calls an hour.
    for _ in range(2):
        assert client.post(u, json={}, headers=v.h()).status_code == 201
    r = client.post(u, json={}, headers=v.h())
    assert r.status_code == 429 and "minutes" in r.json()["detail"]
    assert client.post(u, json={}, headers=other.h()).status_code == 201

    # The hard monthly limit on AI voice minutes stops new calls.
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO usage_limits (customer_id, meter, monthly_hard) VALUES (%s, 'ai_voice_minute', 1)", (b["id"],)
        )
    third = Visitor(client, key)
    assert client.post(u, json={}, headers=third.h()).status_code == 429
