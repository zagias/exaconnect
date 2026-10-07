"""Staff-only inbox features (ADR 0032): mentions, files on notes, saved views,
staff chat and satisfaction surveys. Includes the proof that staff chat and
note files are unreachable through the widget, channels, customer API keys,
exports and the customer AI."""

from __future__ import annotations

import base64

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import inbox
from exaconnect_controller.commai.ai import runtime
from exaconnect_controller.commai.ai.model import ModelInput, ModelOutput, SimulatedModel

from .commai_helpers import api_key, base, business, run_jobs

SITE = "https://www.examplebank.tt"
SECRET_FILE = b"SECRET-FILE-CONTENT staff only"
SECRET_CHAT = "SECRET-CHAT the branch audit is on Friday"


class Capturing:
    name = "capturing"

    def __init__(self):
        self.inputs: list[ModelInput] = []

    def complete(self, inp: ModelInput) -> ModelOutput:
        self.inputs.append(inp)
        return SimulatedModel().complete(inp)


@pytest.fixture(autouse=True)
def simulated():
    runtime.set_model(SimulatedModel())
    yield
    runtime.set_model(None)


def inbound(b, body, address="v1", channel="web", name=""):
    with db.tx() as conn:
        return inbox.receive(conn, b["id"], channel, address, body, name=name)


def note(client, b, conv_id, body, mentions=(), who="agent"):
    r = client.post(
        f"{base(b)}/conversations/{conv_id}/notes", json={"body": body, "mentions": list(mentions)}, headers=b[who]["h"]
    )
    assert r.status_code == 201, r.text
    return r.json()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---- mentions -----------------------------------------------------------------------------


def test_mentions_notify_the_person_and_only_people_in_the_business(client):
    b = business(client)
    other = business(client, "Other Bank", people=("agent",))
    u = base(b)
    conv = inbound(b, "Hello")["conversation"]
    n = note(
        client,
        b,
        conv["id"],
        f"@agent2 can you check this? cc @{b['internal']['email']} and @{other['agent']['email']}",
        mentions=[b["agent"]["email"]],  # the author: not notified
    )
    with db.tx() as conn:
        stored = conn.execute("SELECT mentions FROM commai_notes WHERE id = %s", (n["id"],)).fetchone()["mentions"]
    assert sorted(stored) == sorted([b["agent2"]["email"], b["internal"]["email"]])
    got = client.get(f"{u}/notifications", headers=b["agent2"]["h"]).json()
    assert got["unread"] == 1 and got["items"][0]["kind"] == "mention"
    assert got["items"][0]["title"] == f"{b['agent']['email']} mentioned you in a note"
    assert str(got["items"][0]["conversation_id"]) == str(conv["id"])
    assert client.get(f"{u}/notifications", headers=b["agent"]["h"]).json()["unread"] == 0
    # Nobody in another business hears about it.
    assert client.get(f"{base(other)}/notifications", headers=other["agent"]["h"]).json()["items"] == []
    assert client.get(f"{u}/notifications", headers=other["agent"]["h"]).status_code == 403
    client.post(f"{u}/notifications/read", headers=b["agent2"]["h"])
    assert client.get(f"{u}/notifications", headers=b["agent2"]["h"]).json()["unread"] == 0


# ---- saved views --------------------------------------------------------------------------


def test_saved_views_are_personal_unless_shared(client):
    b = business(client)
    u = base(b)
    r = client.post(
        f"{u}/saved-views",
        json={"name": "My WhatsApp", "filters": {"channel": "whatsapp", "view": "mine"}},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert client.get(f"{u}/saved-views", headers=b["agent2"]["h"]).json() == []
    client.patch(f"{u}/saved-views/{vid}", json={"shared": True}, headers=b["agent"]["h"])
    seen = client.get(f"{u}/saved-views", headers=b["agent2"]["h"]).json()
    assert len(seen) == 1 and seen[0]["mine"] is False and seen[0]["filters"] == {"channel": "whatsapp", "view": "mine"}
    # Only the owner changes or deletes it.
    assert client.delete(f"{u}/saved-views/{vid}", headers=b["agent2"]["h"]).status_code == 404
    bad = client.post(f"{u}/saved-views", json={"name": "x", "filters": {"sql": "1"}}, headers=b["agent"]["h"])
    assert bad.status_code == 422
    dup = client.post(f"{u}/saved-views", json={"name": "My WhatsApp"}, headers=b["agent"]["h"])
    assert dup.status_code == 409
    assert client.delete(f"{u}/saved-views/{vid}", headers=b["agent"]["h"]).status_code == 204


# ---- staff chat ---------------------------------------------------------------------------


def test_direct_and_group_staff_chat(client):
    b = business(client)
    other = business(client, "Other Bank", people=("agent",))
    u = base(b)
    r = client.post(
        f"{u}/staff-chat", json={"kind": "direct", "members": [b["agent2"]["email"]]}, headers=b["agent"]["h"]
    )
    assert r.status_code == 201, r.text
    dm = r.json()["id"]
    again = client.post(
        f"{u}/staff-chat", json={"kind": "direct", "members": [b["agent"]["id"]]}, headers=b["agent2"]["h"]
    )
    assert again.json()["id"] == dm  # one direct chat per pair
    r = client.post(f"{u}/staff-chat/{dm}/messages", json={"body": "Lunch at one?"}, headers=b["agent"]["h"])
    assert r.status_code == 201
    chats = client.get(f"{u}/staff-chat", headers=b["agent2"]["h"]).json()
    assert chats[0]["unread"] == 1
    assert [m["body"] for m in client.get(f"{u}/staff-chat/{dm}/messages", headers=b["agent2"]["h"]).json()] == [
        "Lunch at one?"
    ]
    assert client.get(f"{u}/staff-chat", headers=b["agent2"]["h"]).json()[0]["unread"] == 0
    note_ = client.get(f"{u}/notifications", headers=b["agent2"]["h"]).json()["items"][0]
    assert note_["kind"] == "staff_chat" and note_["title"] == f"Message from {b['agent']['email']}"

    # Someone not in the chat can't read or post in it.
    assert client.get(f"{u}/staff-chat/{dm}/messages", headers=b["internal"]["h"]).status_code == 404
    assert (
        client.post(f"{u}/staff-chat/{dm}/messages", json={"body": "hi"}, headers=b["internal"]["h"]).status_code == 404
    )
    # Groups need a name; people from other businesses can't be added.
    r = client.post(
        f"{u}/staff-chat",
        json={"kind": "group", "name": "Front desk", "members": [b["agent2"]["email"], b["internal"]["email"]]},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201 and len(client.get(f"{u}/staff-chat", headers=b["internal"]["h"]).json()) == 1
    r = client.post(
        f"{u}/staff-chat",
        json={"kind": "group", "name": "Mixed", "members": [other["agent"]["email"]]},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 404
    assert client.get(f"{u}/staff-chat", headers=other["agent"]["h"]).status_code == 403


# ---- files on notes and the isolation proof -----------------------------------------------


def test_staff_chat_and_note_files_are_unreachable_by_any_customer_path(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    assert client.patch(f"{u}/settings", json={"mode": "ai_first"}, headers=h).status_code == 200
    client.post(
        f"{u}/ai/knowledge",
        json={"title": "Hours", "body": "We are open on Saturday from 9am to 1pm.", "approved": True},
        headers=h,
    )
    key = client.post(f"{u}/widget-keys", json={"name": "Site", "allowed_origins": [SITE]}, headers=h).json()
    pk = key["public_key"]
    s = client.post(f"/api/v1/commai/widget/{pk}/session", json={}, headers={"Origin": SITE}).json()["token"]
    vh = {"Origin": SITE, "X-Widget-Session": s}
    r = client.post(
        f"/api/v1/commai/widget/{pk}/messages", json={"body": "Hello there", "client_id": "client-0001"}, headers=vh
    )
    conv_id = r.json()["conversation_id"]
    run_jobs()

    # Staff attach a file to a note and chat about the conversation.
    n = note(client, b, conv_id, "See the attached statement")
    r = client.post(
        f"{u}/notes/{n['id']}/files",
        json={"name": "statement.txt", "type": "text/plain", "data": b64(SECRET_FILE)},
        headers=h,
    )
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    r = client.post(
        f"{u}/notes/{n['id']}/files", json={"name": "x.exe", "type": "text/plain", "data": b64(b"MZ\x90")}, headers=h
    )
    assert r.status_code == 415
    dm = client.post(f"{u}/staff-chat", json={"kind": "direct", "members": [b["agent2"]["email"]]}, headers=h).json()[
        "id"
    ]
    client.post(f"{u}/staff-chat/{dm}/messages", json={"body": SECRET_CHAT}, headers=h)

    # Staff with note access can read the file.
    r = client.get(f"{u}/note-files/{fid}", headers=h)
    assert r.status_code == 200 and r.content == SECRET_FILE
    assert (
        r.headers["content-disposition"].startswith("attachment") and r.headers["x-content-type-options"] == "nosniff"
    )

    # 1. The widget: its file and message endpoints never reach them.
    assert (
        client.get(f"/api/v1/commai/widget/{pk}/files/{fid}", params={"s": s}, headers={"Origin": SITE}).status_code
        == 404
    )
    msgs = client.get(f"/api/v1/commai/widget/{pk}/messages", params={"conversation_id": conv_id}, headers=vh)
    assert SECRET_CHAT not in msgs.text and "statement.txt" not in msgs.text and "SECRET" not in msgs.text

    # 2. Customer-facing API keys (and any key at all, for staff chat).
    cust = api_key(client, h, ["commai:read", "commai:write"])
    full = api_key(client, h, None, name="full")
    assert client.get(f"{u}/note-files/{fid}", headers=cust).status_code == 403
    assert client.get(f"{u}/files/{fid}", headers=cust).status_code == 404  # customer files never include note files
    assert client.get(f"{u}/files/{fid}", headers=h).status_code == 404
    for k in (cust, full):
        assert client.get(f"{u}/staff-chat", headers=k).status_code == 403
        assert client.get(f"{u}/staff-chat/{dm}/messages", headers=k).status_code == 403
        assert client.get(f"{u}/notifications", headers=k).status_code == 403
    for path in (f"/conversations/{conv_id}", f"/conversations/{conv_id}/messages", "/conversations"):
        r = client.get(f"{u}{path}", headers=cust)
        assert r.status_code == 200 and "SECRET" not in r.text and "statement.txt" not in r.text

    # 3. Exports.
    r = client.get(f"{u}/conversations/{conv_id}/export", headers=h)
    assert r.status_code == 200 and "SECRET" not in r.text and "statement.txt" not in r.text

    # 4. Channels: nothing staff-only was ever queued to go out, and no event carries it.
    with db.tx() as conn:
        out = conn.execute("SELECT body FROM messages WHERE customer_id = %s", (b["id"],)).fetchall()
        evs = conn.execute("SELECT data::text AS d FROM commai_events WHERE customer_id = %s", (b["id"],)).fetchall()
        sends = conn.execute("SELECT payload::text AS p FROM jobs WHERE kind = 'message.send'").fetchall()
    assert all("SECRET" not in m["body"] for m in out)
    assert all("SECRET" not in e["d"] and "statement.txt" not in e["d"] for e in evs)
    assert all("SECRET" not in j["p"] for j in sends)

    # 5. The customer AI: its inputs never hold staff chat or note files.
    cap = Capturing()
    runtime.set_model(cap)
    client.post(
        f"/api/v1/commai/widget/{pk}/messages",
        json={"body": "When are you open on Saturday?", "client_id": "client-0002", "conversation_id": conv_id},
        headers=vh,
    )
    with db.tx() as conn:
        conn.execute("UPDATE conversations SET handler = 'ai', handler_user_id = NULL WHERE id = %s", (conv_id,))
        last = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = %s AND direction = 'in' ORDER BY created_at DESC LIMIT 1",
            (conv_id,),
        ).fetchone()
    from exaconnect_controller.commai.ai import agent

    with db.tx() as conn:
        agent.respond(conn, b["id"], conv_id, last["id"])
    assert cap.inputs, "the AI ran"
    seen = "\n".join(i.system + i.as_json() for i in cap.inputs)
    assert "SECRET" not in seen and "statement.txt" not in seen and "See the attached" not in seen


# ---- satisfaction surveys -----------------------------------------------------------------


def test_survey_after_resolution_on_its_own_channel_and_in_reports(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    r = client.put(f"{u}/csat", json={"enabled": True, "delay_minutes": 0}, headers=h)
    assert r.status_code == 200 and r.json()["enabled"] is True
    conv = inbound(b, "Hello", "visitor-1", name="Ana")["conversation"]
    client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Hi Ana, all sorted."}, headers=h)
    assert (
        client.post(f"{u}/conversations/{conv['id']}/state", json={"state": "resolved"}, headers=h).status_code == 200
    )
    run_jobs()
    with db.tx() as conn:
        last = inbox.messages(conn, b["id"], conv["id"])[-1]
        conv_now = inbox.get(conn, b["id"], conv["id"])
    assert last["author_kind"] == "system" and "/api/v1/commai/csat/" in last["body"]
    assert conv_now["state"] == "resolved"  # the survey doesn't reopen it
    token = last["body"].split("/csat/")[1].split()[0]

    page = client.get(f"/api/v1/commai/csat/{token}")
    assert page.status_code == 200 and "How would you rate" in page.text and "Example Bank" in page.text
    r = client.post(
        f"/api/v1/commai/csat/{token}",
        content="rating=4&comment=Quick+help",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200 and "Thank you" in r.text
    assert client.get("/api/v1/commai/csat/not-a-token").status_code == 404

    # The same contact isn't surveyed again within the cool-down.
    client.post(f"{u}/conversations/{conv['id']}/state", json={"state": "reopened"}, headers=h)
    client.post(f"{u}/conversations/{conv['id']}/state", json={"state": "resolved"}, headers=h)
    run_jobs()

    over = client.get(f"{u}/csat", headers=h).json()
    statuses = sorted(s["status"] for s in over["recent"])
    assert statuses == ["answered", "skipped"]
    assert over["last_30_days"]["value"] == 4.0 and over["last_30_days"]["by_handler"]["human"] == 4.0
    rep = client.get(f"{u}/reports/outcomes", headers=h).json()
    assert rep["satisfaction"]["value"] == 4.0 and rep["satisfaction"]["answered"] == 1
    assert rep["satisfaction"]["satisfied_share"] == 1.0


def test_survey_respects_channel_rules(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    client.put(f"{u}/csat", json={"enabled": True, "delay_minutes": 0}, headers=h)
    conv = inbound(b, "Hi", "+18685550101", channel="whatsapp")["conversation"]
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversations SET last_inbound_at = now() - interval '25 hours' WHERE id = %s", (conv["id"],)
        )
    client.post(f"{u}/conversations/{conv['id']}/state", json={"state": "resolved"}, headers=h)
    run_jobs()
    s = client.get(f"{u}/csat", headers=h).json()["recent"][0]
    assert s["status"] == "blocked" and s["channel"] == "whatsapp" and s["reason"]
    with db.tx() as conn:
        assert all(
            m["author_kind"] != "system" or "csat" not in m["body"] for m in inbox.messages(conn, b["id"], conv["id"])
        )
    # Surveys off: nothing is sent.
    client.put(f"{u}/csat", json={"enabled": False}, headers=h)
    conv2 = inbound(b, "Hello", "visitor-2")["conversation"]
    client.post(f"{u}/conversations/{conv2['id']}/state", json={"state": "resolved"}, headers=h)
    run_jobs()
    assert len(client.get(f"{u}/csat", headers=h).json()["recent"]) == 1
