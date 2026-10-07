"""CommAI self-service (ADR 0037): staff "My settings" and the help centre for a
business's own customers: magic links, end-user isolation, bookings through the
action service, opt-outs, published articles only, tenant isolation."""

from __future__ import annotations

import datetime as dt
import re
import time

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import actions, branding, inbox, jobs
from exaconnect_controller.commai.ai import knowledge, runtime
from exaconnect_controller.commai.ai.model import SimulatedModel
from exaconnect_controller.commai.channels import widget
from exaconnect_controller.commai.selfservice import staff

from .commai_helpers import base, business, connect_app, run_jobs

W = {"X-Requested-With": "exa-help"}
HOURS = "Opening hours\n\nWe are open Monday to Friday from 8am to 4pm. We are closed on public holidays."
FEES = "Card fees\n\nA replacement debit card costs 25 dollars and arrives within five working days."
SECRET_DRAFT = "Internal pricing draft\n\nStaff discount code is ORCHID and margins are 40 percent."


@pytest.fixture(autouse=True)
def simulated():
    runtime.set_model(SimulatedModel())
    yield
    runtime.set_model(None)


def source(b, text, approved=True) -> str:
    title, body = text.split("\n\n", 1)
    with db.tx() as conn:
        return str(
            knowledge.add_source(conn, b["id"], title=title, body=body, approved=approved, created_by="user:t")["id"]
        )


def help_business(client, name="Example Bank", *, enable=True) -> dict:
    b = business(client, name, ("agent", "agent2", "internal"))
    r = client.post(
        f"{base(b)}/channel-accounts",
        json={"channel": "email", "provider": "simulated", "address": f"help@{b['id'][:8]}.example", "name": "Email"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    b["src"] = {"hours": source(b, HOURS), "fees": source(b, FEES), "draft": source(b, SECRET_DRAFT, False)}
    for k in ("hours", "fees"):
        r = client.put(
            f"{base(b)}/help-centre/articles/{b['src'][k]}",
            json={"published": True, "category": "Cards"},
            headers=b["agent"]["h"],
        )
        assert r.status_code == 200, r.text
    r = client.patch(f"{base(b)}/help-centre", json={"enabled": enable}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    b["slug"] = r.json()["slug"]
    b["help"] = f"/api/v1/commai/help/{b['slug']}"
    return b


def email_contact(b, address="ana@example.org", body="Hello, a question about my card.") -> dict:
    with db.tx() as conn:
        return inbox.receive(conn, b["id"], "email", address, body, subject="Card", name="Ana")


def magic_token(client, b, email) -> str | None:
    with db.tx() as conn:
        before = conn.execute("SELECT coalesce(max(id), 0) AS n FROM sim_channel_outbox").fetchone()["n"]
    r = client.post(f"{b['help']}/signin", json={"email": email}, headers=W)
    assert r.status_code == 202, r.text
    with db.tx() as conn:
        row = conn.execute(
            "SELECT body FROM sim_channel_outbox WHERE id > %s AND customer_id = %s ORDER BY id DESC LIMIT 1",
            (before, b["id"]),
        ).fetchone()
    if row is None:
        return None
    return re.search(r"signin=([\w-]+)", row["body"]).group(1)


def sign_in(client, b, email="ana@example.org") -> dict:
    tok = magic_token(client, b, email)
    r = client.post(f"{b['help']}/signin/link", json={"token": tok}, headers=W)
    assert r.status_code == 200, r.text
    return {**W, "X-Help-Session": r.json()["token"]}


# ---- My settings ----------------------------------------------------------------------------


def test_staff_change_only_their_own_settings(client):
    b = business(client, "Settings Bank")
    u = base(b)
    r = client.patch(
        f"{u}/me/settings",
        json={
            "display_name": "Agent One",
            "language": "es",
            "notify": {"mention": {"email": False}},
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        },
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["profile"]["display_name"] == "Agent One" and me["language"] == "es"
    assert me["notify"]["mention"] == {"in_app": True, "email": False}
    # agent2's settings are untouched: there is no way to name another person.
    other = client.get(f"{u}/me/settings", headers=b["agent2"]["h"]).json()
    assert other["profile"]["display_name"] == "" and other["language"] == "en"
    assert other["notify"]["mention"]["email"] is True
    # A user id smuggled into the body is ignored (not a field) and changes nothing.
    r = client.patch(
        f"{u}/me/settings", json={"user_id": b["agent2"]["id"], "display_name": "Hacked"}, headers=b["agent"]["h"]
    )
    assert r.status_code == 200
    with db.tx() as conn:
        names = {
            r["id"]: r["display_name"]
            for r in conn.execute(
                "SELECT id::text, display_name FROM users WHERE customer_id = %s", (b["id"],)
            ).fetchall()
        }
    assert names[b["agent2"]["id"]] == "" and names[b["agent"]["id"]] == "Hacked"
    # Another business's member can't reach these settings at all.
    other_biz = business(client, "Other Bank", ("agent",))
    assert client.get(f"{u}/me/settings", headers=other_biz["agent"]["h"]).status_code == 403
    assert client.patch(f"{u}/me/settings", json={"language": "fr"}, headers=other_biz["agent"]["h"]).status_code == 403
    # Bad values are refused.
    assert (
        client.patch(
            f"{u}/me/settings", json={"quiet_hours": {"start": "25:00", "end": "07:00"}}, headers=b["agent"]["h"]
        ).status_code
        == 422
    )


def test_availability_feeds_routing(client):
    b = business(client, "Routing Bank", ("agent", "agent2"))
    u = base(b)
    team = client.post(
        f"{u}/teams", json={"name": "Desk", "members": [b["agent"]["id"], b["agent2"]["id"]]}, headers=b["agent"]["h"]
    ).json()
    client.post(f"{u}/routing-rules", json={"name": "all", "team_id": team["id"]}, headers=b["agent"]["h"])
    r = client.put(f"{u}/me/availability", json={"availability": "away"}, headers=b["agent"]["h"])
    assert r.status_code == 200
    for i in range(3):
        with db.tx() as conn:
            conv = inbox.receive(conn, b["id"], "web", f"v{i}", "hello")["conversation"]
        assert str(conv["assignee_id"]) == b["agent2"]["id"]
    members = {m["id"]: m for m in client.get(f"{u}/members", headers=b["agent"]["h"]).json()}
    assert members[b["agent"]["id"]]["available"] is False and members[b["agent2"]["id"]]["available"] is True
    assert client.get(f"{u}/me/settings", headers=b["agent"]["h"]).json()["availability"] == "away"


def test_notifications_and_quiet_hours(client):
    b = business(client, "Notify Bank", ("agent", "agent2"))
    u = base(b)
    with db.tx() as conn:
        conv = inbox.receive(conn, b["id"], "web", "v1", "hello")["conversation"]
        inbox.assign(conn, b["id"], conv["id"], actor="t", assignee_id=b["agent"]["id"])
        inbox.add_note(conn, b["id"], conv["id"], author="x", body="look", mentions=[b["agent"]["email"]])
    kinds = {n["kind"] for n in client.get(f"{u}/me/notifications", headers=b["agent"]["h"]).json()}
    assert {"assignment", "mention"} <= kinds
    assert client.get(f"{u}/me/notifications", headers=b["agent2"]["h"]).json() == []
    client.patch(f"{u}/me/settings", json={"notify": {"mention": {"in_app": False}}}, headers=b["agent"]["h"])
    kinds = {n["kind"] for n in client.get(f"{u}/me/notifications", headers=b["agent"]["h"]).json()}
    assert "mention" not in kinds
    p = {"notify": {}, "quiet_start": "22:00", "quiet_end": "07:00", "timezone": "UTC"}
    night = dt.datetime(2026, 10, 7, 23, 30, tzinfo=dt.UTC)
    day = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
    assert not staff.should_notify(p, "mention", "email", night)
    assert staff.should_notify(p, "mention", "email", day)
    assert staff.should_notify(p, "mention", "in_app", night)


def test_sessions_sign_out_others_only_mine(client):
    b = business(client, "Session Bank", ("agent", "agent2"))
    u = base(b)
    from .commai_helpers import PASSWORD

    second = client.post("/api/v1/auth/login", json={"email": b["agent"]["email"], "password": PASSWORD}).json()
    h2 = {"Authorization": f"Bearer {second['token']}"}
    rows = client.get(f"{u}/me/sessions", headers=b["agent"]["h"]).json()
    assert len(rows) == 2 and sum(r["current"] for r in rows) == 1
    r = client.post(f"{u}/me/sessions/sign-out-others", headers=b["agent"]["h"])
    assert r.json()["ended"] == 1
    assert client.get("/api/v1/auth/me", headers=h2).status_code == 401
    assert client.get("/api/v1/auth/me", headers=b["agent"]["h"]).status_code == 200
    assert client.get("/api/v1/auth/me", headers=b["agent2"]["h"]).status_code == 200  # someone else: untouched


# ---- help centre: articles, admin, isolation ---------------------------------------------


def test_help_centre_shows_only_published_articles(client):
    b = help_business(client)
    home = client.get(b["help"]).json()
    titles = [a["title"] for c in home["categories"] for a in c["articles"]]
    assert sorted(titles) == ["Card fees", "Opening hours"]
    assert client.get(f"{b['help']}/search", params={"q": "replacement card"}).json()[0]["title"] == "Card fees"
    assert client.get(f"{b['help']}/search", params={"q": "ORCHID discount"}).json() == []
    # An unapproved source can't be published.
    r = client.put(
        f"{base(b)}/help-centre/articles/{b['src']['draft']}", json={"published": True}, headers=b["agent"]["h"]
    )
    assert r.status_code == 409
    # Editing a published source hides it until a person publishes it again.
    art = next(a for c in home["categories"] for a in c["articles"] if a["title"] == "Card fees")
    with db.tx() as conn:
        knowledge.update_source(conn, b["id"], b["src"]["fees"], body="Unreviewed change: cards cost 99 dollars.")
        knowledge.set_approved(conn, b["id"], b["src"]["fees"], True, "user:t")
    assert client.get(f"{b['help']}/articles/{art['id']}").status_code == 404
    assert "99 dollars" not in client.get(f"{b['help']}/search", params={"q": "cards cost"}).text
    adm = client.get(f"{base(b)}/help-centre", headers=b["agent"]["h"]).json()
    assert next(a for a in adm["articles"] if a["title"] == "Card fees")["needs_republish"] is True
    client.put(f"{base(b)}/help-centre/articles/{b['src']['fees']}", json={"published": True}, headers=b["agent"]["h"])
    assert "99 dollars" in client.get(f"{b['help']}/articles/{art['id']}").json()["body"]
    # Withdrawn: gone.
    client.put(f"{base(b)}/help-centre/articles/{b['src']['fees']}", json={"published": False}, headers=b["agent"]["h"])
    assert client.get(f"{b['help']}/articles/{art['id']}").status_code == 404


def test_help_centre_off_until_switched_on_and_admin_only(client):
    b = help_business(client, enable=False)
    assert client.get(b["help"]).status_code == 404
    assert client.get(f"{base(b)}/help-centre", headers=b["internal"]["h"]).status_code == 403
    adm = client.get(f"{base(b)}/help-centre", headers=b["agent"]["h"]).json()
    assert adm["enabled"] is False and adm["preview"]["categories"]  # admins preview it while off
    r = client.patch(
        f"{base(b)}/help-centre", json={"settings": {"show": {"articles": False}}}, headers=b["agent"]["h"]
    )
    assert r.json()["settings"]["show"]["articles"] is False and r.json()["settings"]["show"]["ask"] is True


def test_help_centres_are_isolated_between_businesses(client):
    a = help_business(client, "Alpha Bank")
    z = help_business(client, "Zulu Bank")
    a_art = client.get(a["help"]).json()["categories"][0]["articles"][0]["id"]
    assert client.get(f"{z['help']}/articles/{a_art}").status_code == 404
    # A session from one business's help centre is nothing on another's.
    email_contact(a, "sam@example.org")
    email_contact(z, "sam@example.org", "Zulu question")
    ha = sign_in(client, a, "sam@example.org")
    assert client.get(f"{z['help']}/me", headers=ha).status_code == 401
    convs = client.get(f"{a['help']}/me/conversations", headers=ha).json()
    assert len(convs) == 1 and "Zulu" not in str(convs)
    # Admin of one can't change the other's.
    assert client.get(f"{base(z)}/help-centre", headers=a["agent"]["h"]).status_code == 403
    # Slugs are unique.
    r = client.patch(f"{base(z)}/help-centre", json={"slug": a["slug"]}, headers=z["agent"]["h"])
    assert r.status_code == 409


# ---- magic links ---------------------------------------------------------------------------


def test_magic_link_single_use_and_expiry(client):
    b = help_business(client)
    email_contact(b)
    tok = magic_token(client, b, "Ana@Example.org")
    assert tok
    r = client.post(f"{b['help']}/signin/link", json={"token": tok}, headers=W)
    assert r.status_code == 200
    assert client.post(f"{b['help']}/signin/link", json={"token": tok}, headers=W).status_code == 401
    tok2 = magic_token(client, b, "ana@example.org")
    with db.tx() as conn:
        conn.execute("UPDATE ss_magic_links SET expires_at = now() - interval '1 second' WHERE used_at IS NULL")
    assert client.post(f"{b['help']}/signin/link", json={"token": tok2}, headers=W).status_code == 401
    # A link for one business doesn't work on another.
    other = help_business(client, "Other Bank")
    tok3 = magic_token(client, b, "ana@example.org")
    assert client.post(f"{other['help']}/signin/link", json={"token": tok3}, headers=W).status_code == 401
    # Without the help centre's header, writes are refused.
    assert client.post(f"{b['help']}/signin/link", json={"token": tok3}).status_code == 403


def test_magic_link_does_not_reveal_who_is_a_customer(client):
    b = help_business(client)
    email_contact(b)
    known = client.post(f"{b['help']}/signin", json={"email": "ana@example.org"}, headers=W)
    unknown = client.post(f"{b['help']}/signin", json={"email": "nobody@example.org"}, headers=W)
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    with db.tx() as conn:
        sent = conn.execute("SELECT to_address FROM sim_channel_outbox WHERE customer_id = %s", (b["id"],)).fetchall()
    assert [s["to_address"] for s in sent] == ["ana@example.org"]
    # Per-address limit: silent (same answer, no more emails). Per-client limit: 429 for everyone.
    for _ in range(4):
        assert client.post(f"{b['help']}/signin", json={"email": "ana@example.org"}, headers=W).status_code == 202
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM ss_magic_links WHERE customer_id = %s", (b["id"],)).fetchone()["n"]
    assert n == 3
    codes = [
        client.post(f"{b['help']}/signin", json={"email": f"x{i}@example.org"}, headers=W).status_code
        for i in range(20)
    ]
    assert codes[-1] == 429


def test_business_token_sign_in(client):
    b = help_business(client)
    r = client.post(
        f"{base(b)}/widget-keys",
        json={"name": "Site", "allowed_origins": ["https://bank.example"]},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    good = widget.sign_user_token(secret, {"sub": "cust-42", "email": "c@example.org", "exp": int(time.time()) + 600})
    r = client.post(f"{b['help']}/signin/token", json={"user_token": good}, headers=W)
    assert r.status_code == 200, r.text
    bad = widget.sign_user_token(secret + "x", {"sub": "cust-42", "exp": int(time.time()) + 600})
    assert client.post(f"{b['help']}/signin/token", json={"user_token": bad}, headers=W).status_code == 401


# ---- end-user conversations ----------------------------------------------------------------


def test_end_user_sees_only_own_conversations_and_never_notes(client):
    b = help_business(client)
    mine = email_contact(b)["conversation"]
    theirs = email_contact(b, "bob@example.org", "Bob's private question")["conversation"]
    with db.tx() as conn:
        inbox.add_note(conn, b["id"], mine["id"], author="agent", body="NOTE: customer is a fraud risk")
        inbox.send(
            conn,
            b["id"],
            mine["id"],
            "Thanks Ana, we're on it.",
            author_kind="user",
            author=b["agent"]["email"],
            user_id=b["agent"]["id"],
        )
        conn.execute(
            "INSERT INTO handovers (customer_id, conversation_id, reason, packet) VALUES (%s, %s, %s, '{}')",
            (b["id"], mine["id"], "AI REASONING: unsure"),
        )
    h = sign_in(client, b)
    convs = client.get(f"{b['help']}/me/conversations", headers=h).json()
    assert [c["id"] for c in convs] == [str(mine["id"])]
    detail = client.get(f"{b['help']}/me/conversations/{mine['id']}", headers=h)
    text = detail.text
    assert "Thanks Ana" in text
    for hidden in ("NOTE:", "fraud risk", "AI REASONING", b["agent"]["email"]):
        assert hidden not in text
    assert client.get(f"{b['help']}/me/conversations/{theirs['id']}", headers=h).status_code == 404
    assert client.get(f"{b['help']}/me/conversations/not-a-uuid", headers=h).status_code == 404
    r = client.post(
        f"{b['help']}/me/conversations/{theirs['id']}/messages",
        json={"body": "hi", "client_id": "abcdefgh1"},
        headers=h,
    )
    assert r.status_code == 404
    # Reply to an open conversation of their own.
    r = client.post(
        f"{b['help']}/me/conversations/{mine['id']}/messages",
        json={"body": "Any news?", "client_id": "abcdefgh2"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    with db.tx() as conn:
        last = inbox.messages(conn, b["id"], mine["id"])[-1]
    assert last["body"] == "Any news?" and last["direction"] == "in"
    # Not signed in: nothing.
    assert client.get(f"{b['help']}/me/conversations", headers=W).status_code == 401


def test_ask_uses_the_ai_agent_with_its_guard_rails(client):
    b = help_business(client)
    r = client.post(
        f"{b['help']}/ask", json={"question": "When are you open on Monday?", "client_id": "ask0000001"}, headers=W
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["answered"] and "Monday to Friday" in out["replies"][-1]["body"]
    # Not in approved knowledge (the draft is unapproved): no answer, a person instead.
    r = client.post(
        f"{b['help']}/ask", json={"question": "What is the staff discount code?", "client_id": "ask0000002"}, headers=W
    )
    out = r.json()
    assert not out["answered"] and "ORCHID" not in r.text and "contact form" in out["message"]
    # Ask is hidden when the business keeps the AI off.
    client.patch(f"{base(b)}/settings", json={"mode": "human_only"}, headers=b["agent"]["h"])
    assert client.get(b["help"]).json()["show"]["ask"] is False
    r = client.post(f"{b['help']}/ask", json={"question": "Open Monday?", "client_id": "ask0000003"}, headers=W)
    assert r.status_code == 409


def test_contact_form_opens_web_conversation(client):
    b = help_business(client)
    r = client.post(
        f"{b['help']}/contact",
        json={"name": "Kim", "email": "kim@example.org", "message": "Please call me", "client_id": "contact001"},
        headers=W,
    )
    assert r.status_code == 201, r.text
    with db.tx() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE customer_id = %s AND subject = %s", (b["id"], "Help centre contact form")
        ).fetchone()
    assert conv["channel"] == "web"
    # The same submission retried is stored once.
    client.post(
        f"{b['help']}/contact",
        json={"name": "Kim", "email": "kim@example.org", "message": "Please call me", "client_id": "contact001"},
        headers=W,
    )
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM messages WHERE conversation_id = %s", (conv["id"],)).fetchone()
    assert n["n"] == 1


# ---- bookings ------------------------------------------------------------------------------


def _booked(b, conv_id) -> str:
    start = (dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    with db.tx() as conn:
        run = actions.propose(
            conn,
            b["id"],
            role="person",
            app="sim_calendar",
            action="book",
            inputs={"start": start.isoformat(), "name": "Ana", "contact": "ana@example.org"},
            actor="user:t",
            conversation_id=conv_id,
        )
    run_jobs()
    return str(run["id"])


def _new_time(hour: int) -> str:
    t = (dt.datetime.now(dt.UTC) + dt.timedelta(days=3)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return t.isoformat()


def test_reschedule_confirms_only_after_calendar_success(client):
    b = help_business(client)
    connect_app(b["id"], "sim_calendar", ["book", "cancel"])
    conv = email_contact(b)["conversation"]
    run_id = _booked(b, conv["id"])
    h = sign_in(client, b)
    bk = client.get(f"{b['help']}/me/bookings", headers=h).json()
    assert [x["state"] for x in bk] == ["confirmed"]
    # Outside opening hours: the calendar refuses. Nothing says "moved"; the old time stands.
    r = client.post(f"{b['help']}/me/bookings/{run_id}/reschedule", json={"start": _new_time(20)}, headers=h)
    assert r.status_code == 200, r.text
    with db.tx() as conn:
        assert not any("moved" in m["body"] for m in inbox.messages(conn, b["id"], conv["id"]))
    run_jobs()
    run_jobs()
    with db.tx() as conn:
        bodies = [m["body"] for m in inbox.messages(conn, b["id"], conv["id"])]
    assert not any("has moved" in x for x in bodies) and any("original time is unchanged" in x for x in bodies)
    bk = client.get(f"{b['help']}/me/bookings", headers=h).json()
    assert bk[0]["state"] == "confirmed" and "couldn't move" in bk[0]["note"]
    # A good time: "moved" only after the calendar books it; the old time is then released (by the business).
    r = client.post(f"{b['help']}/me/bookings/{run_id}/reschedule", json={"start": _new_time(11)}, headers=h)
    assert r.status_code == 200
    with db.tx() as conn:
        assert not any("has moved" in m["body"] for m in inbox.messages(conn, b["id"], conv["id"]))
    run_jobs()
    run_jobs()
    with db.tx() as conn:
        bodies = [m["body"] for m in inbox.messages(conn, b["id"], conv["id"])]
        cancel = conn.execute(
            "SELECT * FROM action_runs WHERE customer_id = %s AND action = 'cancel'", (b["id"],)
        ).fetchone()
    assert any("has moved" in x for x in bodies)
    assert cancel["status"] == "awaiting_approval"  # cancelling is sensitive: a person approves
    states = sorted(x["state"] for x in client.get(f"{b['help']}/me/bookings", headers=h).json())
    assert states == ["cancelling", "confirmed"]


def test_cancel_waits_for_the_business_and_others_cannot(client):
    b = help_business(client)
    connect_app(b["id"], "sim_calendar", ["book", "cancel"])
    conv = email_contact(b)["conversation"]
    run_id = _booked(b, conv["id"])
    email_contact(b, "bob@example.org", "Bob here")
    hb = sign_in(client, b, "bob@example.org")
    assert client.post(f"{b['help']}/me/bookings/{run_id}/cancel", headers=hb).status_code == 404
    assert client.get(f"{b['help']}/me/bookings", headers=hb).json() == []
    h = sign_in(client, b)
    r = client.post(f"{b['help']}/me/bookings/{run_id}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval"
    with db.tx() as conn:
        assert not any("cancelled" in m["body"] for m in inbox.messages(conn, b["id"], conv["id"]))
        c = conn.execute(
            "SELECT id FROM action_runs WHERE action = 'cancel' AND customer_id = %s", (b["id"],)
        ).fetchone()
        actions.approve(conn, b["id"], c["id"], approver=f"user:{b['agent']['email']}")
    run_jobs()
    assert client.get(f"{b['help']}/me/bookings", headers=h).json()[0]["state"] == "cancelled"
    with db.tx() as conn:
        assert any("is cancelled" in m["body"] for m in inbox.messages(conn, b["id"], conv["id"]))


# ---- preferences and data requests -----------------------------------------------------------


def test_opt_out_is_honoured_by_the_gateway(client):
    b = help_business(client)
    r = client.post(
        f"{base(b)}/channel-accounts",
        json={"channel": "sms", "provider": "simulated", "address": "+18685550100", "name": "SMS"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    contact = email_contact(b)["conversation"]["contact_id"]
    with db.tx() as conn:
        ident = inbox.find_or_create_identity(conn, b["id"], "sms", "+18685550123", contact_id=contact)
        sms_conv = inbox.receive(conn, b["id"], "sms", "+18685550123", "hi by text")["conversation"]
    h = sign_in(client, b)
    prefs = client.get(f"{b['help']}/me/preferences", headers=h).json()
    sms = next(p for p in prefs["channels"] if p["channel"] == "sms")
    assert sms["receive"] and "•••" in sms["address"]  # an address this session didn't prove is masked
    r = client.put(f"{b['help']}/me/preferences/{ident['id']}", json={"receive": False}, headers=h)
    assert r.status_code == 200
    with db.tx() as conn, pytest.raises(inbox.InboxError) as e:
        inbox.send(conn, b["id"], sms_conv["id"], "Offer!", author_kind="user", author="a", user_id=b["agent"]["id"])
    assert "opted out" in str(e.value)
    # Opting back in needs an address this session proved.
    r = client.put(f"{b['help']}/me/preferences/{ident['id']}", json={"receive": True}, headers=h)
    assert r.status_code == 403


def test_data_requests_reach_the_business_admin(client):
    b = help_business(client)
    conv = email_contact(b)["conversation"]
    with db.tx() as conn:
        inbox.add_note(conn, b["id"], conv["id"], author="agent", body="PRIVATE NOTE")
    h = sign_in(client, b)
    r = client.post(f"{b['help']}/me/data-requests", json={"kind": "download"}, headers=h)
    assert r.status_code == 201
    again = client.post(f"{b['help']}/me/data-requests", json={"kind": "download"}, headers=h)
    assert again.json()["id"] == r.json()["id"]
    reqs = client.get(f"{base(b)}/help-centre/data-requests", headers=b["agent"]["h"]).json()
    assert len(reqs) == 1 and reqs[0]["kind"] == "download"
    exp = client.get(f"{base(b)}/help-centre/data-requests/{reqs[0]['id']}/export", headers=b["agent"]["h"])
    assert exp.status_code == 200 and "PRIVATE NOTE" not in exp.text and "a question about my card" in exp.text
    done = client.post(
        f"{base(b)}/help-centre/data-requests/{reqs[0]['id']}", json={"status": "done"}, headers=b["agent"]["h"]
    )
    assert done.json()["status"] == "done"
    assert client.get(f"{b['help']}/me/data-requests", headers=h).json()[0]["status"] == "done"


def test_delete_request_is_carried_out_through_data_governance(client):
    b = help_business(client)
    contact = email_contact(b)["conversation"]["contact_id"]
    h = sign_in(client, b)
    rid = client.post(f"{b['help']}/me/data-requests", json={"kind": "delete"}, headers=h).json()["id"]
    url = f"{base(b)}/help-centre/data-requests/{rid}"

    # A legal hold refuses it, with the reason on the request.
    with db.tx() as conn:
        conn.execute("UPDATE contacts SET legal_hold = true, legal_hold_reason = 'Claim 7' WHERE id = %s", (contact,))
    r = client.post(url, json={"status": "done"}, headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "refused" and r.json()["subject_request_id"]
    assert client.get(f"{b['help']}/me/data-requests", headers=h).json()[0]["status"] == "refused"

    # Without the hold, a new request deletes the contact and records the subject request.
    with db.tx() as conn:
        conn.execute("UPDATE contacts SET legal_hold = false WHERE id = %s", (contact,))
    rid = client.post(f"{b['help']}/me/data-requests", json={"kind": "delete"}, headers=h).json()["id"]
    r = client.post(
        f"{base(b)}/help-centre/data-requests/{rid}", json={"status": "done", "mode": "delete"}, headers=b["agent"]["h"]
    )
    assert r.status_code == 200 and r.json()["status"] == "done"
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM contacts WHERE id = %s", (contact,)).fetchone() is None
        sr = conn.execute(
            "SELECT kind, status FROM commai_subject_requests WHERE id = %s", (r.json()["subject_request_id"],)
        ).fetchone()
    assert sr == {"kind": "delete", "status": "done"}


def test_help_centre_uses_white_label_branding(client):
    b = help_business(client)
    with db.tx() as conn:
        branding.save(
            conn, customer_id=b["id"], product_name="Example Bank", colour="#0B5A3C", ink=None,
            support_email="", actor="test",
        )  # fmt: skip
        pid = conn.execute(
            "INSERT INTO commai_partners (name, kind) VALUES ('Example Partner', 'msp') RETURNING id"
        ).fetchone()["id"]
        branding.save(
            conn, partner_id=pid, product_name="Island Desk", colour="#155EEF", ink=None, support_email="",
            actor="test",
        )  # fmt: skip
        conn.execute(
            """INSERT INTO commai_partner_links (partner_id, customer_id, status, requested_scopes, white_label)
               VALUES (%s, %s, 'active', '{}', true)""",
            (pid, b["id"]),
        )
    home = client.get(f"{b['help']}").json()
    assert home["business"].startswith("Example Bank") and home["colour"] == "#0B5A3C"
    assert home["platform"] == "Island Desk"


def test_jobs_registered():
    assert "selfservice.booking_follow_up" in jobs._handlers
