"""Inbox gap fixes (ADR 0038): routing by intent, language and skills; snooze
wake-up; built-in service-target reminders and escalations; tag and priority
events."""

import datetime as dt

from psycopg.types.json import Jsonb

from exaconnect_controller import db
from exaconnect_controller.commai import inbox, inbox_jobs, jobs

from .commai_helpers import base, business, run_jobs


def _inbound(b, body, address="+18685550101", channel="web"):
    with db.tx() as conn:
        return inbox.receive(conn, b["id"], channel, address, body, name="Ana")


def _team(client, b, name, members, skills=()):
    r = client.post(
        f"{base(b)}/teams", json={"name": name, "members": members, "skills": list(skills)}, headers=b["agent"]["h"]
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rule(client, b, **body):
    r = client.post(f"{base(b)}/routing-rules", json=body, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    return r.json()


def _member(client, b, who, **body):
    r = client.put(f"{base(b)}/members/{b[who]['id']}", json=body, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text


def _events(b, type_):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM commai_events WHERE customer_id = %s AND type = %s ORDER BY seq", (b["id"], type_)
        ).fetchall()


def test_language_detected_on_inbound_and_routes_human_first(client):
    b = business(client)
    _member(client, b, "agent", languages=["en"])
    _member(client, b, "agent2", languages=["en", "es"])
    spanish = _team(client, b, "Spanish desk", [b["agent"]["id"], b["agent2"]["id"]])
    _rule(client, b, name="Spanish", match={"language": "es"}, team_id=spanish)
    out = _inbound(b, "Hola, necesito ayuda con mi tarjeta por favor, gracias")
    conv = out["conversation"]
    assert conv["language"] == "es"
    assert str(conv["team_id"]) == spanish and str(conv["assignee_id"]) == b["agent2"]["id"]
    # Unsure text leaves the language empty, never a guess.
    assert _inbound(b, "ok", address="+18685550102")["conversation"]["language"] == ""


def test_intent_rule_and_reroute_on_handover(client):
    b = business(client)
    general = _team(client, b, "General", [b["agent"]["id"]])
    billing = _team(client, b, "Billing", [b["agent2"]["id"]])
    _rule(client, b, name="Refunds", position=1, match={"intent": "refund"}, team_id=billing)
    _rule(client, b, name="Everything", position=50, match={}, team_id=general)
    conv = _inbound(b, "Hello there, I have a question")["conversation"]
    assert str(conv["team_id"]) == general and str(conv["assignee_id"]) == b["agent"]["id"]

    # The AI hands over having judged the intent: written on the conversation and re-routed.
    with db.tx() as conn:
        out = inbox.hand_over(conn, b["id"], conv["id"], reason="Wants money back", packet={"intent": "refund"})
    assert out["intent"] == "refund"
    assert str(out["team_id"]) == billing and str(out["assignee_id"]) == b["agent2"]["id"]
    detail = client.get(f"{base(b)}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()
    assert any(e["kind"] == "intent" and e["to_value"] == "refund" for e in detail["log"])

    # Staff and the API can set the intent too.
    r = client.patch(f"{base(b)}/conversations/{conv['id']}", json={"intent": "complaint"}, headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["intent"] == "complaint"


def test_skills_routing(client):
    b = business(client)
    _member(client, b, "agent", skills=[])
    _member(client, b, "agent2", skills=["mortgages", "spanish"])
    loans = _team(client, b, "Loans", [b["agent"]["id"], b["agent2"]["id"]], skills=["mortgages"])
    rule = _rule(client, b, name="Mortgages", match={"keywords": ["mortgage"]}, skills=["mortgages"])
    assert rule["skills"] == ["mortgages"]
    # No team on the rule: the team offering the skill, then the person who has it.
    conv = _inbound(b, "Question about my mortgage")["conversation"]
    assert str(conv["team_id"]) == loans and str(conv["assignee_id"]) == b["agent2"]["id"]
    assert conv["required_skills"] == ["mortgages"]
    # Nobody with the skill available: it waits in the team's queue instead of going to the wrong person.
    _member(client, b, "agent2", skills=["mortgages"], available=False)
    conv2 = _inbound(b, "Another mortgage question", address="+18685550199")["conversation"]
    assert str(conv2["team_id"]) == loans and conv2["assignee_id"] is None


def test_snoozed_conversation_wakes(client):
    b = business(client)
    conv = _inbound(b, "Call me next week")["conversation"]
    until = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
    r = client.post(
        f"{base(b)}/conversations/{conv['id']}/state",
        json={"state": "snoozed", "snoozed_until": until.isoformat()},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text
    run_jobs()  # not due: stays snoozed
    assert client.get(f"{base(b)}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()["state"] == "snoozed"
    with db.tx() as conn:  # time passes
        conn.execute(
            "UPDATE conversations SET snoozed_until = snoozed_until - interval '3 hours' WHERE id = %s", (conv["id"],)
        )
    with db.tx() as conn:
        c = inbox.get(conn, b["id"], conv["id"])
        conn.execute(
            "UPDATE jobs SET payload = %s WHERE kind = 'inbox.wake'",
            (Jsonb({"conversation_id": str(conv["id"]), "until": c["snoozed_until"].isoformat()}),),
        )
    run_jobs()
    detail = client.get(f"{base(b)}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()
    assert detail["state"] == "open" and detail["snoozed_until"] is None
    assert len(_events(b, "conversation.woken")) == 1

    # A stale wake job (snoozed again until later) does nothing.
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    client.post(
        f"{base(b)}/conversations/{conv['id']}/state",
        json={"state": "snoozed", "snoozed_until": later.isoformat()},
        headers=b["agent"]["h"],
    )
    with db.tx() as conn:
        assert (
            inbox_jobs._wake_job(conn, {"payload": {"conversation_id": str(conv["id"]), "until": "2020-01-01"}}) is None
        )
        assert inbox.get(conn, b["id"], conv["id"])["state"] == "snoozed"


def test_service_target_reminder_and_escalation(client):
    b = business(client)
    general = _team(client, b, "General", [b["agent"]["id"]])
    seniors = _team(client, b, "Seniors", [b["agent2"]["id"]])
    _rule(client, b, name="All", match={}, team_id=general)
    r = client.put(
        f"{base(b)}/service-targets", json={"escalate_team_id": seniors, "remind_percent": 75}, headers=b["agent"]["h"]
    )
    assert r.status_code == 200 and r.json()["escalate_team_id"] == seniors, r.text
    conv = _inbound(b, "Hello, my card was declined")["conversation"]
    with db.tx() as conn:
        queued = conn.execute(
            "SELECT payload FROM jobs WHERE kind = 'inbox.target' AND payload->>'conversation_id' = %s",
            (str(conv["id"]),),
        ).fetchall()
    assert {(j["payload"]["target"], j["payload"]["stage"]) for j in queued} == {
        ("first_reply", "soon"),
        ("first_reply", "missed"),
        ("resolve", "soon"),
        ("resolve", "missed"),
    }
    run_jobs()  # nothing due yet
    assert not _events(b, "conversation.target_due_soon")

    with db.tx() as conn:  # the first-reply target is close, then gone
        c = inbox.get(conn, b["id"], conv["id"])
        assert inbox_jobs.check(conn, c, "first_reply", "soon") == "reminded"
        assert inbox_jobs.check(conn, inbox.get(conn, b["id"], conv["id"]), "first_reply", "soon") is None  # once
        assert inbox_jobs.check(conn, inbox.get(conn, b["id"], conv["id"]), "first_reply", "missed") == "escalated"
    assert len(_events(b, "conversation.target_due_soon")) == 1
    missed = _events(b, "conversation.target_missed")
    assert len(missed) == 1 and missed[0]["data"]["target"] == "first_reply"
    detail = client.get(f"{base(b)}/conversations/{conv['id']}", headers=b["agent"]["h"]).json()
    assert detail["priority"] == "high" and detail["team_id"] == seniors
    assert detail["assignee_id"] == b["agent2"]["id"]
    notes = client.get(f"{base(b)}/conversations/{conv['id']}/notes", headers=b["agent"]["h"]).json()
    assert any("Reminder" in n["body"] and b["agent"]["email"] in n["mentions"] for n in notes)
    assert any(n["body"].startswith("Escalated") for n in notes)
    assert len(_events(b, "conversation.escalated")) == 1


def test_target_job_runs_when_due_and_skips_when_met(client):
    b = business(client)
    conv = _inbound(b, "Hello")["conversation"]
    with db.tx() as conn:  # the first-reply target passes without a reply
        conn.execute(
            "UPDATE conversations SET first_reply_due = now() - interval '1 minute' WHERE id = %s", (conv["id"],)
        )
        c = inbox.get(conn, b["id"], conv["id"])
        inbox_jobs.schedule_targets(conn, c)
    run_jobs()
    assert [e["data"]["target"] for e in _events(b, "conversation.target_missed")] == ["first_reply"]

    # Replied in time: no alert.
    conv2 = _inbound(b, "Hello again", address="+18685550177")["conversation"]
    client.post(f"{base(b)}/conversations/{conv2['id']}/messages", json={"body": "Hi"}, headers=b["agent"]["h"])
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversations SET first_reply_due = now() - interval '1 minute' WHERE id = %s", (conv2["id"],)
        )
        inbox_jobs.schedule_targets(conn, inbox.get(conn, b["id"], conv2["id"]))
    run_jobs()
    assert len(_events(b, "conversation.target_missed")) == 1
    assert "inbox.target" in jobs._handlers


def test_tag_and_priority_events(client):
    b = business(client)
    conv = _inbound(b, "Hello")["conversation"]
    r = client.patch(
        f"{base(b)}/conversations/{conv['id']}",
        json={"tags": ["vip", "card"], "priority": "urgent"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 200, r.text
    tags = _events(b, "conversation.tags_changed")
    assert len(tags) == 1 and tags[0]["data"]["added"] == ["card", "vip"]
    pr = _events(b, "conversation.priority_changed")
    assert len(pr) == 1 and pr[0]["data"]["from"] == "normal" and pr[0]["data"]["to"] == "urgent"
    # The same values again: no new events.
    client.patch(f"{base(b)}/conversations/{conv['id']}", json={"tags": ["card", "vip"]}, headers=b["agent"]["h"])
    assert len(_events(b, "conversation.tags_changed")) == 1
    # Webhooks can subscribe to them.
    from exaconnect_controller.commai import events

    assert {"conversation.tags_changed", "conversation.priority_changed", "conversation.target_missed"} <= events.TYPES
