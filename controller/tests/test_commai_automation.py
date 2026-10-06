# ruff: noqa: F811  (pytest fixtures imported from test_commai_automation_helpers)
"""CommAI automation (ADR 0020): integration setup, the Google Calendar and
HubSpot connectors against a fake HTTP layer, encrypted tokens, health and
repair (acceptance test 6), the platform assistant and support cases."""

import datetime as dt
import urllib.parse

from exaconnect_controller import db
from exaconnect_controller.commai import actions, connectors
from exaconnect_controller.commai.automation import assistant, workflows

from .commai_helpers import allow_tools, base, business, connect_app, run_jobs
from .test_commai_automation_helpers import fake_http, run, secrets_env  # noqa: F401

TOMORROW = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()


def _slot(hour: int) -> str:
    return dt.datetime.combine(TOMORROW, dt.time(hour), tzinfo=dt.UTC).isoformat()


def _utc(b) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_settings (customer_id, timezone) VALUES (%s, 'UTC')"
            " ON CONFLICT (customer_id) DO UPDATE SET timezone = 'UTC'",
            (b["id"],),
        )


def _google_signed_in(client, b, fake_http):
    _utc(b)
    u = base(b)
    h = b["agent"]["h"]
    r = client.post(f"{u}/integrations/google_calendar/connect", headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sign_in_ready"] and out["connection"]["status"] == "draft"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(out["sign_in_url"]).query)
    assert q["redirect_uri"] == ["https://connect.example.org/api/v1/commai/oauth/google_calendar/callback"]
    assert "calendar.events" in q["scope"][0] and q["access_type"] == ["offline"]
    fake_http.on(
        "POST",
        r"oauth2\.googleapis\.com/token",
        (
            200,
            {
                "access_token": "ya29.FAKEaccess",
                "refresh_token": "1//FAKErefresh",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy",
            },
        ),
    )
    cb = client.get(
        f"/api/v1/commai/oauth/google_calendar/callback?code=abc&state={q['state'][0]}", follow_redirects=False
    )
    assert cb.status_code == 303 and cb.headers["location"].endswith("/commai/integrations/google_calendar?signin=ok")
    # The state is single-use.
    again = client.get(
        f"/api/v1/commai/oauth/google_calendar/callback?code=abc&state={q['state'][0]}", follow_redirects=False
    )
    assert "signin=failed" in again.headers["location"]
    return u, h


def test_google_calendar_setup_oauth_tokens_encrypted(client, secrets_env, fake_http):
    b = business(client)
    u, h = _google_signed_in(client, b, fake_http)
    token_call = next(c for c in fake_http.calls if "oauth2" in c["url"])
    assert token_call["body"]["grant_type"] == "authorization_code" and token_call["body"]["code"] == "abc"

    # Tokens are encrypted at rest and never returned.
    with db.tx() as conn:
        sec = conn.execute("SELECT ciphertext FROM commai_secrets").fetchall()
    assert sec and all("FAKE" not in s["ciphertext"] for s in sec)
    listed = client.get(f"{u}/integrations", headers=h)
    assert listed.status_code == 200
    assert "FAKE" not in listed.text and "secret_ref" not in listed.text
    g = next(x for x in listed.json() if x["app"] == "google_calendar")
    assert g["connection"]["status"] == "authorised" and g["connection"]["signed_in"]
    assert {a["name"] for a in g["actions"]} == {"find_slots", "book", "cancel"}
    assert g["read_actions"] == ["find_slots"] and "cancel" in g["write_actions"]

    # Allowed actions: read is listed apart from writes.
    r = client.put(f"{u}/integrations/google_calendar/actions", json={"actions": ["book", "find_slots"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["permissions"]["read"] == ["find_slots"]
    assert r.json()["permissions"]["write"][0]["name"] == "book"
    assert (
        client.put(f"{u}/integrations/google_calendar/actions", json={"actions": ["nuke"]}, headers=h).status_code
        == 422
    )
    client.put(
        f"{u}/integrations/google_calendar/settings", json={"settings": {"calendar_id": "bookings@example"}}, headers=h
    )
    assert (
        client.put(
            f"{u}/integrations/google_calendar/settings", json={"settings": {"simulate_failure": "x"}}, headers=h
        ).status_code
        == 422
    )

    # Test mode: a free/busy lookup against the (fake) calendar.
    busy = {"start": _slot(10), "end": (dt.datetime.fromisoformat(_slot(10)) + dt.timedelta(minutes=30)).isoformat()}
    fake_http.on(
        "POST",
        r"/calendar/v3/freeBusy",
        lambda c: (200, {"calendars": {c["body"]["items"][0]["id"]: {"busy": [busy]}}}),
    )
    t = client.post(f"{u}/integrations/google_calendar/test", headers=h)
    assert t.status_code == 200, t.text
    res = t.json()["test"]
    assert res["ok"] and t.json()["connection"]["status"] == "testing"
    slots = res["results"][0]["result"]["slots"]
    assert _slot(9) in slots and _slot(10) not in slots  # busy time is never offered
    call = next(c for c in fake_http.calls if "freeBusy" in c["url"])
    assert call["headers"]["Authorization"] == "Bearer ya29.FAKEaccess"

    # Field mapping: confident matches are suggested; unclear ones need a person before go-live.
    m = client.get(f"{u}/integrations/google_calendar/mapping", headers=h).json()
    fields = {f["source"]: f for f in m["objects"]["appointment"]["fields"]}
    assert fields["appointment.start"]["target"] == "start.dateTime" and not fields["appointment.start"]["needs_person"]
    assert m["open"]
    client.post(f"{u}/integrations/google_calendar/mapping/accept", headers=h)
    r = client.post(f"{u}/integrations/google_calendar/approve", headers=h)
    assert r.status_code == 409 and "field mapping" in r.json()["detail"]
    saved = {f["source"].split(".")[1]: f["target"] for f in m["objects"]["appointment"]["fields"]}
    r = client.put(f"{u}/integrations/google_calendar/mapping", json={"mapping": {"appointment": saved}}, headers=h)
    assert r.status_code == 200 and r.json()["mapping_open"] == []
    r = client.post(f"{u}/integrations/google_calendar/approve", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "live" and r.json()["approved_by"] == f"user:{b['agent']['email']}"


def test_google_calendar_booking_never_books_twice(client, secrets_env, fake_http):
    b = business(client)
    _google_signed_in(client, b, fake_http)
    with db.tx() as conn:
        conn.execute(
            "UPDATE integration_connections SET status = 'live', allowed_actions = '{book,find_slots}'"
            " WHERE app = 'google_calendar'"
        )
    created: dict = {}

    def insert(c):
        created[c["body"]["id"]] = c["body"]
        return 200, {**c["body"], "status": "confirmed", "htmlLink": "https://calendar.google.com/x"}

    def get_event(c):
        eid = c["url"].rsplit("/", 1)[1]
        return (200, {**created[eid], "status": "confirmed"}) if eid in created else (404, {"error": {}})

    fake_http.on("POST", r"/freeBusy", (200, {"calendars": {"primary": {"busy": []}}}))
    fake_http.on("POST", r"/calendars/primary/events$", insert)
    fake_http.on("GET", r"/calendars/primary/events/", get_event)
    allow_tools(b["id"], "customer_agent", ["google_calendar.book"])
    with db.tx() as conn:
        run_ = actions.propose(
            conn,
            b["id"],
            role="customer_agent",
            app="google_calendar",
            action="book",
            inputs={"start": _slot(11), "name": "Ana", "contact": "ana@example.com"},
            actor="ai:customer_agent",
            idempotency_key="conv1:book",
        )
    run_jobs()
    with db.tx() as conn:
        done = conn.execute("SELECT * FROM action_runs WHERE id = %s", (run_["id"],)).fetchone()
        confirmed = conn.execute("SELECT count(*) AS n FROM commai_events WHERE type = 'booking.confirmed'").fetchone()[
            "n"
        ]
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["booking_id"] == workflows_event_id("conv1:book")
    assert confirmed == 1
    assert fake_http.count("POST", r"/events$") == 1

    # A retry with the same key (e.g. after a crash) finds the event and does not insert again.
    c = connectors.get("google_calendar")
    with db.tx() as conn:
        row = connectors.connection(conn, b["id"], "google_calendar")
        again = c.execute(conn, row, "book", done["inputs"], "conv1:book")
    assert again["replayed"] and fake_http.count("POST", r"/events$") == 1

    # If Google already has the id (409), the first booking is returned, not a second.
    fake_http.on(
        "GET",
        r"/calendars/primary/events/",
        lambda call: (
            (404, {}) if fake_http.count("POST", r"/events$") < 2 else (200, {"id": "x", "status": "confirmed"})
        ),
    )
    fake_http.on("POST", r"/calendars/primary/events$", (409, {"error": {"message": "duplicate"}}))
    with db.tx() as conn:
        out = c.execute(conn, row, "book", {**done["inputs"], "start": _slot(12)}, "conv2:book")
    assert out["replayed"]


def workflows_event_id(key: str) -> str:
    from exaconnect_controller.commai.connectors.google_calendar import event_id

    return event_id(key)


def test_hubspot_token_entry_and_no_duplicate_creates(client, secrets_env, fake_http):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    # A fake private-app token, built at run time so secret scanners do not mistake it for a real one.
    token = "-".join(["pat", "na1", "1" * 8, "2" * 4, "3" * 4, "4" * 4, "5" * 12])
    fake_http.on("GET", r"api\.hubapi\.com/crm/v3/objects/contacts\?", (401, {"message": "Authentication failed"}))
    r = client.post(f"{u}/integrations/hubspot/token", json={"token": token}, headers=h)
    assert r.status_code == 422 and token not in r.text
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM commai_secrets").fetchone()["n"] == 0

    fake_http.on("GET", r"api\.hubapi\.com/crm/v3/objects/contacts\?", (200, {"results": []}))
    r = client.post(f"{u}/integrations/hubspot/token", json={"token": token}, headers=h)
    assert r.status_code == 200, r.text
    assert token not in r.text and r.json()["auth_method"] == "token" and r.json()["signed_in"]
    with db.tx() as conn:
        assert token not in str(conn.execute("SELECT * FROM commai_secrets").fetchall())
        assert token not in str(conn.execute("SELECT * FROM audit_log").fetchall())

    posts = []

    def create(c):
        posts.append(c)
        return 201, {"id": str(100 + len(posts)), "properties": c["body"]["properties"]}

    fake_http.on("POST", r"/crm/v3/objects/contacts/search", (200, {"results": []}))
    fake_http.on("POST", r"/crm/v3/objects/contacts$", create)
    fake_http.on("POST", r"/crm/v3/objects/tickets/search", (200, {"results": []}))
    fake_http.on("POST", r"/crm/v3/objects/tickets$", create)
    hs = connectors.get("hubspot")
    with db.tx() as conn:
        row = connectors.connection(conn, b["id"], "hubspot")
        out = hs.execute(
            conn, row, "create_lead", {"name": "Ana", "email": "ana@example.com", "notes": "5 pages"}, "K1"
        )
        again = hs.execute(conn, row, "create_lead", {"name": "Ana", "email": "ana@example.com"}, "K1")
    assert out["lead_id"] == "101" and again == {"lead_id": "101", "replayed": True}
    assert len(posts) == 1
    props = posts[0]["body"]["properties"]
    assert props == {"firstname": "Ana", "email": "ana@example.com", "message": "5 pages", "lifecyclestage": "lead"}
    assert posts[0]["headers"]["Authorization"] == f"Bearer {token}"

    # A ticket an earlier attempt created (but never recorded) is found by its reference, not created again.
    fake_http.on(
        "POST",
        r"/crm/v3/objects/tickets/search",
        lambda c: (
            (200, {"results": [{"id": "777"}]})
            if (c["body"]["filterGroups"][0]["filters"][0]["propertyName"] == "content")
            else (400, {})
        ),
    )
    with db.tx() as conn:
        t = hs.execute(conn, row, "create_ticket", {"subject": "Broken card"}, "K2")
    assert t == {"ticket_id": "777", "existing": True} and len(posts) == 1

    # Causes are named for repair.
    fake_http.on("POST", r"/crm/v3/objects/deals/search", (403, {"category": "MISSING_SCOPES", "message": "scopes"}))
    fake_http.on(
        "POST",
        r"/crm/v3/objects/contacts/search",
        (400, {"category": "VALIDATION_ERROR", "message": 'Property "emial" does not exist'}),
    )
    with db.tx() as conn:
        for action, inputs, cause in (
            ("create_deal", {"title": "Website"}, "permission"),
            ("find_contact", {"email": "x@example.com"}, "mapping"),
        ):
            try:
                hs.execute(conn, row, action, inputs, f"k-{action}")
                raise AssertionError("expected a failure")
            except connectors.ConnectorError as e:
                assert e.cause == cause


def _setup_sim_calendar(client, b) -> tuple[str, dict]:
    u, h = base(b), b["agent"]["h"]
    assert (
        client.post(f"{u}/integrations/sim_calendar/connect", headers=h).json()["connection"]["status"] == "authorised"
    )
    client.put(f"{u}/integrations/sim_calendar/actions", json={"actions": ["find_slots", "book"]}, headers=h)
    t = client.post(f"{u}/integrations/sim_calendar/test", headers=h)
    assert t.json()["test"]["ok"], t.text
    r = client.post(f"{u}/integrations/sim_calendar/approve", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "live", r.text
    return u, h


def _book(client, u, h, hour):
    r = client.post(
        f"{u}/actions",
        json={
            "app": "sim_calendar",
            "action": "book",
            "inputs": {"start": _slot(hour), "name": "Ana", "contact": "ana@x.org"},
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    run_jobs()
    with db.tx() as conn:
        return conn.execute("SELECT * FROM action_runs WHERE id = %s", (r.json()["id"],)).fetchone()


def test_acceptance_6_broken_integration_status_evidence_and_safe_recovery(client, secrets_env, fake_http):
    """A broken integration shows a clear status, the evidence and a safe way to recover."""
    b = business(client)
    u, h = _setup_sim_calendar(client, b)
    assert _book(client, u, h, 9)["status"] == "succeeded"
    with db.tx() as conn:
        created = workflows.create(
            conn,
            b["id"],
            {
                "name": "Book follow-ups",
                "trigger": {"event": "message.received", "conditions": []},
                "steps": [
                    {
                        "type": "action",
                        "app": "sim_calendar",
                        "action": "book",
                        "inputs": {"start": "{{vars.when}}", "name": "{{contact.name}}", "contact": "x"},
                    }
                ],
            },
            actor="user:x",
        )
        workflows.publish(conn, b["id"], created["workflow"]["id"], actor="user:x", grant_tools=True)
    healthy = client.get(f"{u}/integrations/sim_calendar/health", headers=h).json()
    assert healthy["level"] == "healthy" and healthy["repair"] is None and healthy["last_success_at"]

    # The calendar's sign-in expires.
    client.put(
        f"{u}/integrations/sim_calendar/settings", json={"settings": {"simulate_failure": "expired_signin"}}, headers=h
    )
    failed = _book(client, u, h, 10)
    assert failed["status"] == "failed" and "expired" in failed["error"]
    hh = client.get(f"{u}/integrations/sim_calendar/health", headers=h).json()
    assert hh["status"] == "broken" and hh["level"] == "broken"
    assert hh["cause"] == "expired_signin" and hh["repair"]["label"] == "Sign-in expired"
    assert [s["id"] for s in hh["repair"]["steps"]] == ["sign_in", "recheck"]
    assert hh["evidence"][0]["run_id"] == str(failed["id"]) and "expired" in hh["evidence"][0]["error"]
    assert hh["failures_since_success"] == 1 and hh["last_success_at"]
    assert [w["name"] for w in hh["affected_workflows"]] == ["Book follow-ups"]

    # Rechecking while it is still broken changes nothing.
    r = client.post(f"{u}/integrations/sim_calendar/repair", json={"step": "recheck"}, headers=h).json()
    assert not r["ok"] and r["health"]["status"] == "broken"
    # Pointing to the sign-in never asks for a password in chat.
    s = client.post(f"{u}/integrations/sim_calendar/repair", json={"step": "sign_in"}, headers=h).json()
    assert s["needs_person"] and "never asks for passwords" in s["detail"]

    # The platform assistant finds the same cause, with evidence, and proposes a fix from its fixed list.
    a = client.post(f"{u}/assistant/ask", json={"question": "Why are bookings failing?"}, headers=h)
    assert a.status_code == 200, a.text
    ans = a.json()
    assert ans["confidence"] == "confirmed" and "Confirmed cause" in ans["answer"]
    assert any(f["area"] == "integration:sim_calendar" for f in ans["findings"])
    fix = next(f for f in ans["fixes"] if f["fix_id"] == "recheck_integration")
    assert fix["status"] == "proposed" and fix["appliable"]

    # The business signs in again (here: the simulated failure is cleared); the admin approves the fix.
    client.put(f"{u}/integrations/sim_calendar/settings", json={"settings": {"simulate_failure": ""}}, headers=h)
    applied = client.post(f"{u}/assistant/fixes/{fix['id']}/apply", headers=h)
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied" and applied.json()["resolved"]
    again = client.post(f"{u}/assistant/fixes/{fix['id']}/apply", headers=h)
    assert again.status_code == 409
    after = client.get(f"{u}/integrations/sim_calendar/health", headers=h).json()
    assert after["status"] == "live" and after["level"] == "healthy"
    assert _book(client, u, h, 11)["status"] == "succeeded"


def test_google_expired_refresh_shows_signin_cause(client, secrets_env, fake_http):
    b = business(client)
    u, h = _google_signed_in(client, b, fake_http)
    with db.tx() as conn:
        conn.execute(
            "UPDATE integration_connections SET status = 'live', allowed_actions = '{find_slots}',"
            " token_expires_at = now() - interval '1 minute' WHERE app = 'google_calendar'"
        )
    fake_http.on("POST", r"oauth2\.googleapis\.com/token", (400, {"error": "invalid_grant"}))
    hh = client.get(f"{u}/integrations/google_calendar/health?check=true", headers=h).json()
    assert hh["cause"] == "expired_signin" and hh["level"] == "broken"
    assert hh["evidence"][0]["check"] == "live" and not hh["evidence"][0]["ok"]
    assert hh["repair"]["steps"][0]["id"] == "sign_in"
    assert "FAKE" not in str(hh)
    # A provider outage is named as such, with a safe retry.
    fake_http.on("POST", r"oauth2\.googleapis\.com/token", (200, {"access_token": "ya29.FAKE2", "expires_in": 3600}))
    fake_http.on("GET", r"/calendars/primary$", (503, {"error": {"message": "backend error"}}))
    hh = client.get(f"{u}/integrations/google_calendar/health?check=true", headers=h).json()
    assert hh["cause"] == "provider" and [s["id"] for s in hh["repair"]["steps"]] == ["recheck", "retry_failed"]


def test_assistant_refuses_secrets_and_opens_support_case(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    r = client.post(f"{u}/assistant/ask", json={"question": "my api key is sk-abcdef1234567890 why no work"}, headers=h)
    assert r.status_code == 200 and "don't paste" in r.json()["answer"]
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM commai_assistant_answers").fetchone()["n"] == 0

    # Nothing wrong: the cause is unknown and a support case can be opened.
    ok = client.post(f"{u}/assistant/ask", json={"question": "Why has WhatsApp stopped sending?"}, headers=h).json()
    assert ok["confidence"] in ("unknown", "confirmed", "likely")
    connect_app(b["id"], "sim_crm", ["create_lead"])
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO action_runs (customer_id, role, app, action, idempotency_key, status, error, proposed_by)
               VALUES (%s, 'workflow', 'sim_crm', 'create_lead', 'k', 'failed',
                       'CRM said: Bearer abcdefghijklmnop for ana@example.com (provider)', 'workflow:x')""",
            (b["id"],),
        )
    case = client.post(f"{u}/support-cases", json={"subject": "Leads not created", "answer_id": ok["id"]}, headers=h)
    assert case.status_code == 201, case.text
    c = case.json()
    assert c["reference"].startswith("CASE-")
    assert c["errors"] and "abcdefghijklmnop" not in str(c["errors"]) and "ana@example.com" not in str(c["errors"])
    assert any(x.startswith("action:") for x in c["correlation_ids"])
    assert any(i["app"] == "sim_crm" for i in c["configuration"]["integrations"])
    assert "secret_ref" not in str(c["configuration"])
    assert c["diagnostics"]
    # Only fixes from the fixed list can be applied.
    assert "recheck_integration" in assistant.fix_ids() and "delete_everything" not in assistant.fix_ids()


def test_internal_seat_and_other_tenant_cannot_change_integrations(client, secrets_env):
    b = business(client)
    other = business(client, "Other Shop", people=("agent",))
    u = base(b)
    assert client.post(f"{u}/integrations/sim_crm/connect", headers=b["internal"]["h"]).status_code == 403
    assert client.post(f"{u}/integrations/sim_crm/connect", headers=other["agent"]["h"]).status_code == 403
    assert client.get(f"{u}/integrations", headers=other["agent"]["h"]).status_code == 403
