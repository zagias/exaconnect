# ruff: noqa: F401, F811  (pytest fixtures imported from the helpers)
"""Freshdesk and ServiceNow connectors (ADR 0035) against a fake HTTP layer
(live mode) and their simulated stand-ins."""

import base64
import json
import urllib.parse

from exaconnect_controller import db
from exaconnect_controller.commai.automation import integrations
from exaconnect_controller.commai.connectors import kit

from .commai_helpers import base, business
from .connectors_more_helpers import (
    connect_real,
    connect_simulated,
    deliver,
    execute_direct,
    fake,
    go_live,
    hook,
    propose_and_run,
    q,
    real_env,
)

FD = r"https://acme\.freshdesk\.com"
SN = r"https://acme\.service-now\.com"
SYS = "a" * 32


def _enter_credentials(customer_id, app, creds, settings):
    with db.tx() as conn:
        integrations.connect(conn, customer_id, app, "user:admin@example.org")
        conn.execute(
            "UPDATE integration_connections SET settings = settings || %s::jsonb WHERE customer_id = %s AND app = %s",
            (json.dumps(settings), customer_id, app),
        )
        return integrations.enter_credentials(conn, customer_id, app, creds, "user:admin@example.org")


def test_freshdesk_api_key_entry_and_basic_auth(client, real_env, fake):
    b = business(client)
    go_live("freshdesk", b["id"])
    key = "fd" + "k" * 18
    fake.on("GET", FD + r"/api/v2/agents/me$", (200, {"id": 7}))
    row = _enter_credentials(b["id"], "freshdesk", {"api_key": key}, {"domain": "acme"})
    assert row["auth_method"] == "credentials" and row["signed_in"]
    expected = "Basic " + base64.b64encode(f"{key}:X".encode()).decode()
    assert fake.last("GET", "agents/me")["headers"]["Authorization"] == expected
    fake.on("GET", FD + r"/api/v2/agents/me$", (401, {"code": "invalid_credentials", "message": "Bad key"}))
    try:
        _enter_credentials(b["id"], "freshdesk", {"api_key": key}, {"domain": "acme"})
        raise AssertionError("a refused key must not be stored")
    except integrations.SetupError as e:
        assert "did not accept" in str(e)


def test_freshdesk_tickets_paging_idempotency_and_webhook(client, real_env, fake):
    b = business(client)
    go_live("freshdesk", b["id"])
    connect_real(
        b["id"],
        "freshdesk",
        ["create_ticket", "list_tickets", "add_comment"],
        creds={"api_key": "k" * 20},
        settings={"domain": "acme"},
    )
    created = []

    def create(c):
        created.append(c)
        return 201, {"id": 55, "status": 2, "priority": 3, "subject": "S"}

    fake.on("GET", FD + r"/api/v2/search/tickets", (200, {"results": [], "total": 0}))
    fake.on("POST", FD + r"/api/v2/tickets$", create)
    inputs = {"subject": "S", "description": "D", "email": "ana@example.com", "priority": "high"}
    run = propose_and_run(b["id"], "freshdesk", "create_ticket", inputs, "k1")
    assert run["status"] == "succeeded", run["error"]
    assert created[0]["body"]["tags"] == ["commai", kit.ref("k1")] and created[0]["body"]["priority"] == 3
    assert "tag:'" + kit.ref("k1") in urllib.parse.unquote_plus(fake.last("GET", "search/tickets")["url"])
    assert execute_direct(b["id"], "freshdesk", "create_ticket", inputs, "k1")["replayed"] and len(created) == 1

    page2 = "https://acme.freshdesk.com/api/v2/tickets?email=ana%40example.com&page=2"
    fake.on("GET", FD + r"/api/v2/tickets\?email=.*page=2", (200, [{"id": 2, "status": 4, "priority": 1}]))
    fake.on(
        "GET",
        FD + r"/api/v2/tickets\?email=ana%40example\.com&order_by",
        (200, [{"id": 1, "status": 2, "priority": 2}], {"Link": f'<{page2}>; rel="next"'}),
    )
    run = propose_and_run(b["id"], "freshdesk", "list_tickets", {"email": "ana@example.com"}, "k2")
    assert [t["status"] for t in run["result"]["tickets"]] == ["open", "solved"]

    fake.on("POST", FD + r"/api/v2/tickets/55/notes", (201, {"id": 9, "private": True}))
    run = propose_and_run(b["id"], "freshdesk", "add_comment", {"ticket_id": "55", "body": "Noted"}, "k3")
    assert run["status"] == "succeeded" and fake.last("POST", "notes")["body"]["private"] is True

    r = client.post(f"{base(b)}/connectors/freshdesk/webhooks", headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["manual"]
    secret = r.json()["headers"]["X-Jibsy-Token"]
    body = json.dumps({"ticket_id": "55", "status": "Resolved", "id": "55-Resolved"}).encode()
    assert deliver(b["id"], "freshdesk", {"X-Jibsy-Token": "wrong"}, body) is None
    assert deliver(b["id"], "freshdesk", {}, body) is None
    evs = deliver(b["id"], "freshdesk", {"X-Jibsy-Token": secret}, body)
    assert evs[0]["data"]["status"] == "solved"
    assert q("SELECT status FROM commai_ticket_links WHERE ticket_id = '55'")[0]["status"] == "solved"


def test_servicenow_incident_lifecycle_and_signed_rule(client, real_env, fake):
    b = business(client)
    go_live("servicenow", b["id"])
    connect_real(
        b["id"],
        "servicenow",
        ["create_ticket", "find_ticket", "update_ticket"],
        creds={"api_key": "s" * 24},
        settings={"instance": "acme"},
    )
    inc = {"sys_id": SYS, "number": "INC0010001", "state": "1", "priority": "2", "short_description": "S"}
    fake.on("GET", SN + r"/api/now/table/incident\?sysparm_query=correlation_id", (200, {"result": []}))
    fake.on("GET", SN + r"/api/now/table/sys_user", (200, {"result": [{"sys_id": "u" * 32}]}))
    fake.on("POST", SN + r"/api/now/table/incident$", (201, {"result": inc}))
    inputs = {"subject": "S", "description": "D", "email": "ana@example.com", "priority": "urgent"}
    run = propose_and_run(b["id"], "servicenow", "create_ticket", inputs, "k1")
    assert run["status"] == "succeeded", run["error"]
    assert run["result"]["number"] == "INC0010001"
    sent = fake.last("POST", "incident$")
    assert sent["headers"]["x-sn-apikey"] == "s" * 24 and "Authorization" not in sent["headers"]
    assert sent["body"]["correlation_id"] == kit.ref("k1") and sent["body"]["caller_id"] == "u" * 32
    assert (sent["body"]["urgency"], sent["body"]["impact"]) == ("1", "1")

    fake.on("GET", SN + r"/api/now/table/incident\?sysparm_query=number%3DINC0010001", (200, {"result": [inc]}))
    fake.on("PATCH", SN + rf"/api/now/table/incident/{SYS}", lambda c: (200, {"result": {**inc, **c["body"]}}))
    run = propose_and_run(b["id"], "servicenow", "update_ticket", {"ticket_id": "INC0010001", "status": "solved"}, "k2")
    assert run["status"] == "succeeded", run["error"]
    patch = fake.last("PATCH", "incident/")["body"]
    assert patch["state"] == "6" and patch["close_code"] and patch["close_notes"]

    r = client.post(f"{base(b)}/connectors/servicenow/webhooks", headers=b["agent"]["h"])
    assert r.status_code == 200 and "GlideCertificateEncryption" in r.json()["business_rule"]
    secret = hook(b["id"], "servicenow")["secret"]
    assert secret in r.json()["business_rule"]
    body = json.dumps({"id": f"{SYS}-3", "sys_id": SYS, "number": "INC0010001", "state": "7"}).encode()
    assert deliver(b["id"], "servicenow", {"X-Jibsy-Signature": kit.hmac_b64("other", body)}, body) is None
    evs = deliver(b["id"], "servicenow", {"X-Jibsy-Signature": kit.hmac_b64(secret, body)}, body)
    assert evs[0]["data"] == {"ticket_id": SYS, "status": "closed"}


def test_servicenow_and_freshdesk_stand_ins(client, fake):
    b, other = business(client), business(client, "Other Bank")
    every = ["create_ticket", "find_ticket", "list_tickets", "update_ticket", "add_comment"]
    for app in ("servicenow", "freshdesk"):
        connect_simulated(b["id"], app, every)
        connect_simulated(other["id"], app, every)
        inputs = {"subject": "S", "description": "D", "email": "ana@example.com"}
        run = propose_and_run(b["id"], app, "create_ticket", inputs, f"{app}-1")
        assert run["status"] == "succeeded", (app, run["error"])
        tid = run["result"]["ticket_id"]
        assert execute_direct(b["id"], app, "create_ticket", inputs, f"{app}-1")["ticket_id"] == tid
        run = propose_and_run(b["id"], app, "update_ticket", {"ticket_id": tid, "status": "solved"}, f"{app}-2")
        assert run["status"] == "succeeded" and run["result"]["status"] == "solved", (app, run["error"])
        run = propose_and_run(b["id"], app, "list_tickets", {"email": "ana@example.com"}, f"{app}-3")
        assert run["result"]["count"] == 1, app
        run = propose_and_run(b["id"], app, "add_comment", {"ticket_id": tid, "body": "x", "public": "yes"}, f"{app}-4")
        assert run["status"] == "succeeded" and run["result"]["public"] is True, (app, run["error"])
        # Tenant isolation: the other business's stand-in has no such ticket.
        run = propose_and_run(other["id"], app, "list_tickets", {"email": "ana@example.com"}, f"{app}-o")
        assert run["result"]["count"] == 0, app
        # A rehearsed expired sign-in names its repair cause.
        connect_simulated(b["id"], app, ["find_ticket"], settings={"simulate_failure": "expired_signin"})
        run = propose_and_run(b["id"], app, "find_ticket", {"ticket_id": tid}, f"{app}-5")
        assert run["status"] == "failed" and "expired_signin" in run["error"], app
    assert not fake.calls
