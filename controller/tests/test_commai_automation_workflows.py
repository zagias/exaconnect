# ruff: noqa: F811  (pytest fixtures imported from test_commai_automation_helpers)
"""Jibsy workflows (ADR 0020): plain English to a draft, versions, publish
with explicit permission, trigger dispatch from the event log, collect /
action / assign / remind steps, approvals, pause, test mode with no external
effects, idempotent runs and starter packs."""

from types import SimpleNamespace

from exaconnect_controller import db
from exaconnect_controller.commai import actions, inbox
from exaconnect_controller.commai.automation import compose, llm, workflows

from .commai_helpers import base, business, connect_app
from .test_commai_automation_helpers import fire_timers, run, secrets_env  # noqa: F401

QUOTE = (
    "When a new customer asks for a quote, collect their requirements, create a lead, assign it to Sales "
    "and remind the owner if nobody responds"
)


def _sales(client, b) -> str:
    r = client.post(f"{base(b)}/teams", json={"name": "Sales", "members": [b["agent"]["id"]]}, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _say(b, text, address="+18685550101", name="Ana"):
    with db.tx() as conn:
        out = inbox.receive(conn, b["id"], "web", address, text, name=name)
        # Website chat asks for an email before the chat starts.
        conn.execute(
            "UPDATE contacts SET email = %s WHERE id = %s AND email = ''",
            (f"{name.lower()}@example.com", out["conversation"]["contact_id"]),
        )
        return out


def _runs(wf_id, test=False):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM commai_workflow_runs WHERE workflow_id = %s AND test = %s ORDER BY started_at", (wf_id, test)
        ).fetchall()


def _quote_workflow(client, b):
    u, h = base(b), b["agent"]["h"]
    _sales(client, b)
    connect_app(b["id"], "sim_crm", ["create_lead", "create_ticket"])
    d = client.post(f"{u}/workflows/draft", json={"text": QUOTE}, headers=h)
    assert d.status_code == 200, d.text
    draft = d.json()
    assert draft["source"] == "rules" and not draft["unrecognised"]
    assert [s["type"] for s in draft["definition"]["steps"]] == ["collect", "action", "assign", "remind"]
    assert draft["definition"]["trigger"]["event"] == "message.received"
    assert draft["preview"][0].startswith("When message.received")
    c = client.post(f"{u}/workflows", json={"definition": draft["definition"]}, headers=h)
    assert c.status_code == 201, c.text
    wf_id = c.json()["workflow"]["id"]
    # Workflows can't act until a person allows the action: publishing without that is refused.
    p = client.post(f"{u}/workflows/{wf_id}/publish", json={}, headers=h)
    assert p.status_code == 409 and "not allowed to create a lead" in p.json()["detail"]
    p = client.post(f"{u}/workflows/{wf_id}/publish", json={"grant_tools": True}, headers=h)
    assert p.status_code == 200, p.text
    assert p.json()["workflow"]["status"] == "live" and p.json()["workflow"]["live_version"] == 1
    return u, h, wf_id


def test_quote_workflow_from_plain_english_end_to_end(client, secrets_env):
    b = business(client)
    u, h, wf_id = _quote_workflow(client, b)

    # Not a quote: nothing starts.
    _say(b, "What time do you open?", address="+18685550199", name="Ben")
    run()
    assert _runs(wf_id) == []

    out = _say(b, "Hi, can I get a quote for a new website?")
    conv_id = out["conversation"]["id"]
    run()
    runs = _runs(wf_id)
    assert len(runs) == 1 and runs[0]["status"] == "waiting"
    with db.tx() as conn:
        msgs = inbox.messages(conn, b["id"], conv_id)
    assert msgs[-1]["author_kind"] == "workflow" and "requirements" in msgs[-1]["body"]

    # The customer answers: the lead is created through the action service, then assigned.
    _say(b, "Five pages and online booking, by December")
    run()
    run()
    r = _runs(wf_id)[0]
    assert r["context"]["vars"]["requirements"] == "Five pages and online booking, by December"
    with db.tx() as conn:
        ar = conn.execute("SELECT * FROM action_runs WHERE customer_id = %s AND app = 'sim_crm'", (b["id"],)).fetchall()
        lead = conn.execute("SELECT * FROM sim_records WHERE customer_id = %s AND kind = 'lead'", (b["id"],)).fetchall()
        conv = conn.execute("SELECT * FROM conversations WHERE id = %s", (conv_id,)).fetchone()
    assert len(ar) == 1 and ar[0]["role"] == "workflow" and ar[0]["status"] == "succeeded"
    assert ar[0]["proposed_by"] == f"workflow:{wf_id}"
    assert len(lead) == 1 and lead[0]["data"]["notes"].startswith("Five pages")
    assert str(conv["assignee_id"]) == b["agent"]["id"]
    assert r["status"] == "waiting" and r["wait"]["kind"] == "remind"

    # Nobody replies within the hour: the owner is reminded with a private note.
    fire_timers(r["id"])
    r = _runs(wf_id)[0]
    assert r["status"] == "done"
    with db.tx() as conn:
        notes = conn.execute("SELECT * FROM commai_notes WHERE conversation_id = %s", (conv_id,)).fetchall()
        reminders = conn.execute("SELECT count(*) AS n FROM commai_events WHERE type = 'workflow.reminder'").fetchone()[
            "n"
        ]
    assert reminders == 1 and notes[-1]["body"].startswith(f"@{b['agent']['email']}")

    # The run log has every step.
    log = client.get(f"{u}/workflow-runs/{r['id']}", headers=h).json()
    kinds = [(s["type"], s["status"]) for s in log["steps"]]
    assert kinds[0] == ("trigger", "done")
    assert ("collect", "done") in kinds and ("action", "done") in kinds and ("assign", "done") in kinds
    assert kinds[-1] == ("remind", "done") and "token" not in log["wait"]

    # Runs are idempotent per (version, event): reading the log again starts nothing new.
    with db.tx() as conn:
        conn.execute("UPDATE commai_workflow_cursor SET last_seq = 0")
        workflows.dispatch_pass(conn)
    assert len(_runs(wf_id)) == 1


def test_reply_in_time_means_no_reminder_and_pause_stops_new_runs(client, secrets_env):
    b = business(client)
    u, h, wf_id = _quote_workflow(client, b)
    out = _say(b, "Could I get a quote please?")
    conv_id = out["conversation"]["id"]
    run()
    _say(b, "Logo and a shop")
    run()
    run()
    r = _runs(wf_id)[0]
    assert r["wait"]["kind"] == "remind"
    r2 = client.post(f"{u}/conversations/{conv_id}/messages", json={"body": "Hi Ana, I'll send it today."}, headers=h)
    assert r2.status_code == 201, r2.text
    fire_timers(r["id"])
    with db.tx() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM commai_events WHERE type = 'workflow.reminder'").fetchone()["n"]
            == 0
        )
    assert _runs(wf_id)[0]["status"] == "done"

    # Paused: matching messages start nothing. Resumed: new ones do.
    assert client.post(f"{u}/workflows/{wf_id}/pause", headers=h).json()["status"] == "paused"
    _say(b, "A quote for printing?", address="+18685550222", name="Cy")
    run()
    assert len(_runs(wf_id)) == 1
    assert client.post(f"{u}/workflows/{wf_id}/resume", headers=h).json()["status"] == "live"
    _say(b, "A quote for signs?", address="+18685550333", name="Di")
    run()
    assert len(_runs(wf_id)) == 2


def test_dry_run_has_no_external_effects_and_versions(client, secrets_env):
    b = business(client)
    u, h, wf_id = _quote_workflow(client, b)
    client.post(f"{u}/workflows/{wf_id}/pause", headers=h)
    out = _say(b, "Quote for a kitchen?")
    run()

    def counts():
        with db.tx() as conn:
            return [
                conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
                for t in ("messages", "commai_notes", "action_runs", "sim_records")
            ]

    before = counts()
    t = client.post(f"{u}/workflows/{wf_id}/test", json={}, headers=h)
    assert t.status_code == 200, t.text
    res = t.json()
    assert res["trigger_matches"] and not res["external_effects"]
    assert res["event"]["type"] == "message.received"
    assert [s["status"] for s in res["steps"][1:]] == ["would_run"] * 4
    assert "Would ask" in res["steps"][1]["summary"]
    assert res["steps"][2]["inputs"]["name"] == "Ana"
    assert counts() == before
    assert _runs(wf_id, test=True)[0]["status"] == "done"
    # A sample with no conversation: steps that need one are skipped.
    s = client.post(
        f"{u}/workflows/{wf_id}/test", json={"sample": {"message": {"text": "quote please", "first": True}}}, headers=h
    ).json()
    assert s["trigger_matches"] and s["steps"][1]["status"] == "skipped"
    assert out["conversation"]["id"]

    # Editing saves version 2; version 1 stays live until 2 is published.
    d = client.get(f"{u}/workflows/{wf_id}", headers=h).json()
    d["definition"]["steps"][3]["after_s"] = 7200
    up = client.put(f"{u}/workflows/{wf_id}", json={"definition": d["definition"]}, headers=h).json()
    assert up["version"]["version"] == 2
    again = client.get(f"{u}/workflows/{wf_id}", headers=h).json()
    assert again["live_version"] == 1 and again["draft_pending"] and len(again["versions"]) == 2
    client.post(f"{u}/workflows/{wf_id}/resume", headers=h)
    p = client.post(f"{u}/workflows/{wf_id}/publish", json={"version": 2}, headers=h)
    assert p.status_code == 200 and p.json()["workflow"]["live_version"] == 2
    assert (
        client.get(f"{u}/workflows/{wf_id}/versions/1", headers=h).json()["definition"]["steps"][3]["after_s"] == 3600
    )


def test_approval_step_needs_a_person_and_ai_cannot_skip_rules(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    # Sending to many customers needs an approval step before it.
    bad = {
        "name": "Blast",
        "trigger": {"event": "message.received", "conditions": []},
        "steps": [{"type": "send_message", "body": "Sale!", "audience": "matching", "filter": {"channel": "web"}}],
    }
    v = client.post(f"{u}/workflows/validate", json={"definition": bad}, headers=h).json()
    assert any("needs an approval step" in p for p in v["problems"])
    # A definition can't approve itself, skip checks or grant tools.
    sneaky = {
        "name": "Sneaky",
        "trigger": {"event": "message.received", "conditions": []},
        "steps": [
            {
                "type": "action",
                "app": "sim_crm",
                "action": "create_lead",
                "approved": True,
                "inputs": {"name": "x", "email": "x@example.com"},
                "grant": ["sim_crm.*"],
            }
        ],
    }
    v = client.post(f"{u}/workflows/validate", json={"definition": sneaky}, headers=h).json()
    assert sum("can't approve its own actions" in p for p in v["problems"]) == 2
    assert "approved" not in v["definition"]["steps"][0]
    # A workflow can't start from its own events.
    loop = {"name": "Loop", "trigger": {"event": "workflow.run_finished"}, "steps": [{"type": "wait", "duration_s": 5}]}
    v = client.post(f"{u}/workflows/validate", json={"definition": loop}, headers=h).json()
    assert any("loop" in p for p in v["problems"])

    ok = {
        "name": "Refunds",
        "trigger": {
            "event": "message.received",
            "conditions": [{"field": "text", "op": "contains", "value": "refund"}],
        },
        "steps": [
            {"type": "approval", "prompt": "Refund for {{contact.name}}?"},
            {"type": "send_message", "body": "We've passed your refund request to our team."},
        ],
    }
    wf_id = client.post(f"{u}/workflows", json={"definition": ok}, headers=h).json()["workflow"]["id"]
    assert client.post(f"{u}/workflows/{wf_id}/publish", json={}, headers=h).status_code == 200
    conv_id = _say(b, "I want a refund")["conversation"]["id"]
    run()
    r = _runs(wf_id)[0]
    assert r["status"] == "awaiting_approval"
    pend = client.get(f"{u}/workflow-approvals", headers=h).json()
    assert pend[0]["prompt"] == "Refund for Ana?"
    with db.tx() as conn:
        try:
            workflows.decide(conn, b["id"], r["id"], approve=True, actor=f"workflow:{wf_id}")
            raise AssertionError("a workflow approved itself")
        except workflows.WorkflowError as e:
            assert e.code == 403
    assert client.post(f"{u}/workflow-runs/{r['id']}/approve", json={}, headers=b["internal"]["h"]).status_code == 403
    a = client.post(f"{u}/workflow-runs/{r['id']}/approve", json={}, headers=h)
    assert a.status_code == 200 and a.json()["status"] == "done", a.text
    with db.tx() as conn:
        last = inbox.messages(conn, b["id"], conv_id)[-1]
    assert last["author_kind"] == "workflow" and "refund request" in last["body"]


def test_sensitive_action_waits_for_a_person(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    connect_app(b["id"], "sim_calendar", ["cancel"])
    d = {
        "name": "Cancel",
        "trigger": {"event": "message.received", "conditions": []},
        "steps": [
            {
                "type": "action",
                "app": "sim_calendar",
                "action": "cancel",
                "inputs": {"booking_id": "nope"},
                "on_failure": "continue",
            },
            {"type": "add_note", "body": "Cancellation handled"},
        ],
    }
    wf = client.post(f"{u}/workflows", json={"definition": d}, headers=h).json()
    assert any("sensitive" in w for w in wf["warnings"])
    client.post(f"{u}/workflows/{wf['workflow']['id']}/publish", json={"grant_tools": True}, headers=h)
    _say(b, "Please cancel")
    run()
    r = _runs(wf["workflow"]["id"])[0]
    assert r["status"] == "waiting"
    with db.tx() as conn:
        ar = conn.execute("SELECT * FROM action_runs WHERE app = 'sim_calendar'").fetchone()
        assert ar["status"] == "awaiting_approval"
        actions.approve(conn, b["id"], ar["id"], approver=f"user:{b['agent2']['email']}")
    run(["action.execute"])
    run()
    r = _runs(wf["workflow"]["id"])[0]
    assert r["status"] == "done"  # the cancel failed (no such booking) but the step says continue
    steps = client.get(f"{u}/workflow-runs/{r['id']}", headers=h).json()["steps"]
    assert ("action", "failed") in [(s["type"], s["status"]) for s in steps]


def test_model_draft_is_validated_and_falls_back_to_rules(client, secrets_env, monkeypatch):
    b = business(client)
    settings = SimpleNamespace(llm_api_key="test", llm_base_url="", llm_model="")
    monkeypatch.setattr(
        llm,
        "complete_json",
        lambda *a, **k: {
            "name": "x",
            "trigger": {"event": "message.received"},
            "steps": [
                {
                    "type": "action",
                    "app": "sim_crm",
                    "action": "create_lead",
                    "approved": True,
                    "inputs": {"name": "{{contact.name}}", "email": "{{contact.email}}"},
                }
            ],
        },
    )
    with db.tx() as conn:
        out = compose.from_text(conn, b["id"], QUOTE, settings)
    assert out["source"] == "rules" and any("did not pass" in n for n in out["notes"])
    good = {
        "name": "Leads",
        "trigger": {"event": "message.received", "conditions": []},
        "steps": [{"type": "assign", "team": "Sales"}],
    }
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: good)
    with db.tx() as conn:
        out = compose.from_text(conn, b["id"], "When anyone writes, give it to Sales", settings)
    assert out["source"] == "model" and out["definition"]["steps"][0]["team"] == "Sales"
    assert any("no team called Sales" in w for w in out["warnings"])


def test_plain_english_phrasings_and_starter_packs(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    with db.tx() as conn:
        p = compose.parse(
            conn,
            b["id"],
            "When a message arrives on WhatsApp that mentions refund, add a note "
            '"check the order", get approval then send "We\'re on it" and escalate '
            "to Billing if nobody replies within 2 hours",
        )
    d = p["definition"]
    assert {"field": "channel", "op": "eq", "value": "whatsapp"} in d["trigger"]["conditions"]
    assert [s["type"] for s in d["steps"]] == ["add_note", "approval", "send_message", "escalate"]
    assert d["steps"][3] == {"type": "escalate", "after_s": 7200, "priority": "high", "team": "Billing"}
    with db.tx() as conn:
        p = compose.parse(conn, b["id"], "When a booking is confirmed, juggle some flaming torches")
    assert p["definition"]["trigger"]["event"] == "booking.confirmed" and p["unrecognised"]

    packs = client.get(f"{u}/workflows/packs", headers=h).json()
    assert [x["id"] for x in packs] == ["appointments", "ecommerce", "professional_services", "property", "hospitality"]
    r = client.post(f"{u}/workflows/packs/professional_services", json={}, headers=h)
    assert r.status_code == 201, r.text
    names = [w["name"] for w in client.get(f"{u}/workflows", headers=h).json()]
    assert sorted(names) == ["New client intake", "Quote requests"]
    assert all(w["status"] == "draft" for w in client.get(f"{u}/workflows", headers=h).json())
    for pack in ("appointments", "ecommerce", "property", "hospitality"):
        assert client.post(f"{u}/workflows/packs/{pack}", json={}, headers=h).status_code == 201
    with db.tx() as conn:
        apps = conn.execute(
            "SELECT DISTINCT s->>'app' AS app FROM commai_workflow_versions v,"
            " jsonb_array_elements(v.definition->'steps') s WHERE s->>'type' = 'action'"
        ).fetchall()
    assert {a["app"] for a in apps} == {"sim_crm"}  # bound to the example CRM until HubSpot is live
