# ruff: noqa: F811  (pytest fixtures imported from commai_connector_kit)
"""Vendor connectors (ADR 0028): Salesforce, Dynamics 365, Zoho CRM, Pipedrive,
Microsoft 365, Gmail and Calendly. Each is tested the same way:

- OAuth sign-in (mocked token endpoint), tokens encrypted, provider fields kept;
- a read and a write through the action service against a mock of the
  provider's API (its stand-in served behind the fake transport, so the
  requests are the provider's own shapes), with request-shape checks;
- idempotent writes (a retry with the same key never creates twice);
- rate limits (short Retry-After waited out; long one becomes a provider error);
- an expired sign-in shows the repair cause;
- webhook signatures (good accepted once, bad refused) where the app has them;
- tenant isolation; and the stand-in until the app is switched on.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
import urllib.parse

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import actions, connectors
from exaconnect_controller.commai.automation import oauth
from exaconnect_controller.commai.connectors import kit

from .commai_connector_kit import (  # noqa: F401
    connect_real,
    connect_simulated,
    execute_direct,
    fake,
    go_live,
    propose_and_run,
    real_env,
)
from .commai_helpers import base, business

TOMORROW = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()


def slot(hour: int) -> str:
    return dt.datetime.combine(TOMORROW, dt.time(hour), tzinfo=dt.UTC).isoformat()


def sign_calendly(secret, body):
    t = str(int(time.time()))
    return {"Calendly-Webhook-Signature": f"t={t},v1={kit.hmac_hex(secret, f'{t}.'.encode() + body)}"}


V = {
    "salesforce": {
        "host": r"https://acme\.my\.salesforce\.com",
        "token_url": r"login\.salesforce\.com/services/oauth2/token",
        "token_extra": {"instance_url": "https://acme.my.salesforce.com"},
        "kept": ("instance_url", "https://acme.my.salesforce.com"),
        "authorize": "login.salesforce.com/services/oauth2/authorize",
        "scope": "api",
        "read": ("find_contact", {"email": "ann@example.com"}),
        "write": ("create_contact", {"name": "Ann Brown", "email": "ann@example.com", "phone": "+18685550100"}),
        "write_call": ("POST", r"/sobjects/Contact$"),
        "store": "Contact",
        "read_path": r"/query",
        "id": "contact_id",
        "auth": "Bearer ",
    },
    "dynamics365": {
        "settings": {"org_host": "acme.crm4.dynamics.com"},
        "host": r"https://acme\.crm4\.dynamics\.com",
        "token_url": r"login\.microsoftonline\.com/common/oauth2/v2\.0/token",
        "authorize": "login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "scope": "https://acme.crm4.dynamics.com/user_impersonation",
        "read": ("find_contact", {"email": "ann@example.com"}),
        "write": ("create_contact", {"name": "Ann Brown", "email": "ann@example.com"}),
        "write_call": ("PATCH", r"/contacts\("),
        "store": "contacts",
        "read_path": r"/contacts\?",
        "id": "contact_id",
        "auth": "Bearer ",
    },
    "zoho_crm": {
        "settings": {"dc": "eu"},
        "host": r"https://www\.zohoapis\.eu",
        "token_url": r"accounts\.zoho\.eu/oauth/v2/token",
        "token_extra": {"api_domain": "https://www.zohoapis.eu"},
        "kept": ("api_domain", "https://www.zohoapis.eu"),
        "authorize": "accounts.zoho.eu/oauth/v2/auth",
        "scope": "ZohoCRM.modules.contacts.ALL",
        "read": ("find_contact", {"email": "ann@example.com"}),
        "write": ("create_contact", {"name": "Ann Brown", "email": "ann@example.com"}),
        "write_call": ("POST", r"/Contacts/upsert$"),
        "store": "Contacts",
        "read_path": r"/Contacts/search",
        "id": "contact_id",
        "auth": "Zoho-oauthtoken ",
        "hook": lambda secret, body: ({}, {}),
        "hook_body": lambda secret: {
            "channel_id": "1000",
            "module": "Contacts",
            "operation": "insert",
            "ids": ["57"],
            "token": secret[:50],
        },
    },
    "pipedrive": {
        "host": r"https://acme\.pipedrive\.com",
        "token_url": r"oauth\.pipedrive\.com/oauth/token",
        "token_extra": {"api_domain": "https://acme.pipedrive.com"},
        "kept": ("api_domain", "https://acme.pipedrive.com"),
        "authorize": "oauth.pipedrive.com/oauth/authorize",
        "scope": None,
        "basic_token_auth": True,
        "read": ("find_contact", {"email": "ann@example.com"}),
        "write": ("create_contact", {"name": "Ann Brown", "email": "ann@example.com"}),
        "write_call": ("POST", r"/api/v1/persons$"),
        "store": "persons",
        "read_path": r"/persons/search",
        "id": "contact_id",
        "auth": "Bearer ",
        "hook": lambda secret, body: (
            {"Authorization": "Basic " + base64.b64encode(f"commai:{secret}".encode()).decode()},
            {},
        ),
        "hook_body": lambda secret: {"meta": {"object": "person", "action": "added", "id": 57}, "current": {"id": 57}},
    },
    "microsoft365": {
        "host": r"https://graph\.microsoft\.com/v1\.0",
        "token_url": r"login\.microsoftonline\.com/common/oauth2/v2\.0/token",
        "authorize": "login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "scope": "Calendars.ReadWrite",
        "read": ("find_slots", {"date": TOMORROW.isoformat()}),
        "write": ("book", {"start": slot(10), "name": "Ann Brown", "contact": "ann@example.com"}),
        "write_call": ("POST", r"/me/events$"),
        "store": "event",
        "read_path": r"/me/calendarView",
        "id": "booking_id",
        "auth": "Bearer ",
        "hook": lambda secret, body: ({}, {}),
        "hook_body": lambda secret: {
            "value": [
                {
                    "subscriptionId": "s1",
                    "clientState": secret[:128],
                    "changeType": "created",
                    "resource": "Users/u/Events/AAMk1",
                    "resourceData": {"id": "AAMk1"},
                }
            ]
        },
    },
    "gmail": {
        "host": r"https://gmail\.googleapis\.com/gmail/v1/users/me",
        "token_url": r"oauth2\.googleapis\.com/token",
        "authorize": "accounts.google.com/o/oauth2/v2/auth",
        "scope": "gmail.send",
        "read": ("find_messages", {"email": "ann@example.com"}),
        "write": ("send_email", {"to": "ann@example.com", "subject": "Your booking", "body": "See you tomorrow."}),
        "write_call": ("POST", r"/messages/send$"),
        "store": "message",
        "read_path": r"/messages\?",
        "id": "message_id",
        "auth": "Bearer ",
        "hook": lambda secret, body: ({}, {"token": secret}),
        "hook_body": lambda secret: {
            "message": {
                "data": base64.b64encode(json.dumps({"emailAddress": "a@x.org", "historyId": "9"}).encode()).decode(),
                "messageId": "m-1",
            },
            "subscription": "projects/p/subscriptions/s",
        },
    },
    "calendly": {
        "host": r"https://api\.calendly\.com",
        "token_url": r"auth\.calendly\.com/oauth/token",
        "token_extra": {
            "owner": "https://api.calendly.com/users/ABC",
            "organization": "https://api.calendly.com/organizations/ORG",
        },
        "kept": ("owner", "https://api.calendly.com/users/ABC"),
        "authorize": "auth.calendly.com/oauth/authorize",
        "scope": None,
        "read": ("list_event_types", {}),
        "write": ("create_booking_link", {"event_type": "https://api.calendly.com/event_types/CONSULT30"}),
        "write_call": ("POST", r"/scheduling_links$"),
        "read_path": r"/event_types",
        "id": "booking_url",
        "auth": "Bearer ",
        "hook": lambda secret, body: (sign_calendly(secret, body), {}),
        "hook_body": lambda secret: {
            "event": "invitee.created",
            "payload": {
                "uri": "https://api.calendly.com/scheduled_events/E1/invitees/I1",
                "email": "a@x.org",
                "name": "Ann",
                "status": "active",
                "scheduled_event": {
                    "uri": "https://api.calendly.com/scheduled_events/E1",
                    "start_time": "2026-10-08T14:00:00Z",
                },
            },
        },
    },
}
APPS = list(V)


def mock_provider(fake, app: str, owner_id: str) -> None:
    """The provider's API at its real host, answered by its stand-in (provider shapes)."""
    spec, c = V[app], connectors.get(app)

    def handler(call):
        raw = json.dumps(call["body"]).encode() if isinstance(call["body"], dict) else None
        with db.tx() as conn:
            r = c.simulator.handle(
                conn,
                {"customer_id": owner_id, "settings": {}},
                kit.Request(call["method"], call["url"], call["headers"], call["body"], raw),
            )
        return r.status, r.body, r.headers

    for m in ("GET", "POST", "PATCH", "PUT", "DELETE"):
        fake.on(m, spec["host"], handler)


def _utc(cid: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_settings (customer_id, timezone) VALUES (%s, 'UTC')"
            " ON CONFLICT (customer_id) DO UPDATE SET timezone = 'UTC'",
            (cid,),
        )


def sign_in(client, b, app, fake) -> None:
    """Connect, follow the OAuth sign-in with a mocked token endpoint."""
    spec, u, h = V[app], base(b), b["agent"]["h"]
    r = client.post(f"{u}/integrations/{app}/connect", headers=h)
    assert r.status_code == 200, r.text
    if spec.get("settings"):
        r = client.put(f"{u}/integrations/{app}/settings", json={"settings": spec["settings"]}, headers=h)
        assert r.status_code == 200, r.text
        r = client.post(f"{u}/integrations/{app}/sign-in", headers=h)
        assert r.status_code == 200, r.text
        url = r.json()["url"] if "url" in r.json() else r.json()["sign_in_url"]
    else:
        url = r.json()["sign_in_url"]
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(parts.query)
    assert spec["authorize"] in parts.netloc + parts.path
    assert q["redirect_uri"] == [f"https://connect.example.org/api/v1/commai/oauth/{app}/callback"]
    if spec["scope"]:
        assert spec["scope"] in q["scope"][0]
    access = f"FAKEaccess-{app}"
    fake.on(
        "POST",
        spec["token_url"],
        (
            200,
            {
                "access_token": access,
                "refresh_token": f"FAKErefresh-{app}",
                "expires_in": 3600,
                **spec.get("token_extra", {}),
            },
        ),
    )
    cb = client.get(f"/api/v1/commai/oauth/{app}/callback?code=c0de&state={q['state'][0]}", follow_redirects=False)
    assert cb.status_code == 303 and "signin=ok" in cb.headers["location"], cb.headers.get("location")
    call = fake.last("POST", spec["token_url"])
    assert call["body"]["grant_type"] == "authorization_code" and call["body"]["code"] == "c0de"
    if spec.get("basic_token_auth"):
        assert call["headers"]["Authorization"].startswith("Basic ") and "client_secret" not in call["body"]
    else:
        # Apps that share one OAuth client (Microsoft 365 and OneDrive) read the same setting.
        assert call["body"]["client_secret"] == os.environ[f"{oauth.PROVIDERS[app].env}_CLIENT_SECRET"]
    with db.tx() as conn:
        row = connectors.connection(conn, b["id"], app)
        assert all("FAKE" not in s["ciphertext"] for s in conn.execute("SELECT ciphertext FROM commai_secrets"))
    assert row["auth_method"] == "oauth" and row["auth_status"] == "signed_in"
    if spec.get("kept"):
        assert row["settings"][spec["kept"][0]] == spec["kept"][1]


@pytest.mark.parametrize("app", APPS)
def test_vendor_contract(client, real_env, fake, app):
    spec = V[app]
    b = business(client, f"Biz {app}", people=("agent",))
    _utc(b["id"])
    # Before ExaCarib switches it on, the catalogue offers the stand-in and says why.
    item = next(x for x in client.get(f"{base(b)}/integrations", headers=b["agent"]["h"]).json() if x["app"] == app)
    assert item["simulated"] and "go-live" in item["not_live_reason"]
    assert item["golive_key"] == f"integration-{app}" and item["needs_from_exacarib"]
    reads = {a["name"] for a in item["actions"] if a["kind"] == "read"}
    assert spec["read"][0] in reads and spec["write"][0] not in reads

    go_live(app, b["id"])
    mock_provider(fake, app, b["id"])
    sign_in(client, b, app, fake)
    with db.tx() as conn:
        conn.execute(
            "UPDATE integration_connections SET status = 'live', allowed_actions = %s"
            " WHERE customer_id = %s AND app = %s",
            ([spec["read"][0], spec["write"][0]], b["id"], app),
        )

    # A write and a read through the action service.
    run = propose_and_run(b["id"], app, spec["write"][0], spec["write"][1], f"{app}-w1")
    assert run["status"] == "succeeded", run["error"]
    first = run["result"][spec["id"]]
    m, pat = spec["write_call"]
    assert fake.count(m, pat) == 1
    call = fake.last(m, pat)
    assert call["headers"]["Authorization"] == spec["auth"] + f"FAKEaccess-{app}"
    run = propose_and_run(b["id"], app, spec["read"][0], spec["read"][1], f"{app}-r1")
    assert run["status"] == "succeeded", run["error"]

    # Idempotency: the same key never writes twice, even when our record of it is lost.
    again = execute_direct(b["id"], app, spec["write"][0], spec["write"][1], f"{app}-w1")
    assert again.get(spec["id"]) == first or again.get("replayed")
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects WHERE app = %s", (app,))
    if app not in ("calendly",):  # a one-time link is only remembered by us; nothing is booked by making it
        again = execute_direct(b["id"], app, spec["write"][0], spec["write"][1], f"{app}-w1")
        with db.tx() as conn:
            n = conn.execute(
                "SELECT count(*) AS n FROM sim_records WHERE customer_id = %s AND app = %s AND kind = %s",
                (b["id"], f"sim:{app}", spec["store"]),
            ).fetchone()["n"]
        assert n == 1, f"{app} created twice"

    # Rate limits: a short Retry-After is waited out; a long one is a provider error.
    original = [r for r in fake.routes]
    n = {"i": 0}

    def limited(call):
        n["i"] += 1
        if n["i"] == 1:
            return 429, {"error": {"message": "slow down"}}, {"Retry-After": "1"}
        return next(h for mm, p, h in original if mm == call["method"] and p.search(call["url"]))(call)

    fake.on("GET", spec["host"] + ".*" + spec["read_path"].lstrip("/"), limited)
    fake.on("POST", spec["host"] + ".*" + spec["read_path"].lstrip("/"), limited)
    out = execute_direct(b["id"], app, spec["read"][0], spec["read"][1], f"{app}-r2")
    assert n["i"] >= 2 and out
    fake.on("GET", spec["host"], (429, {"error": {"message": "slow down"}}, {"Retry-After": "900"}))
    fake.on("POST", spec["host"], (429, {"error": {"message": "slow down"}}, {"Retry-After": "900"}))
    with pytest.raises(connectors.ConnectorError) as e:
        execute_direct(b["id"], app, spec["read"][0], spec["read"][1], f"{app}-r3")
    assert e.value.cause == "provider"

    # Expired sign-in: the token is refused and cannot be renewed.
    for mm in ("GET", "POST", "PATCH", "PUT"):
        fake.on(mm, spec["host"], (401, {"error": {"code": "InvalidAuthenticationToken", "message": "expired"}}))
    fake.on("POST", spec["token_url"], (400, {"error": "invalid_grant"}))
    with db.tx() as conn:
        conn.execute(
            "UPDATE integration_connections SET token_expires_at = now() - interval '1 minute' WHERE app = %s", (app,)
        )
    run = propose_and_run(b["id"], app, spec["write"][0], spec["write"][1], f"{app}-w2")
    assert run["status"] == "failed" and "(expired_signin)" in run["error"], run["error"]
    hl = client.get(f"{base(b)}/integrations/{app}/health", headers=b["agent"]["h"]).json()
    assert hl["cause"] == "expired_signin" and hl["status"] == "broken"
    assert hl["repair"]["steps"][0]["id"] == "sign_in"

    # Tenant isolation: another business has no connection and sees none.
    z = business(client, f"Other {app}", people=("agent",))
    with db.tx() as conn:
        with pytest.raises(actions.ActionRefused) as refused:
            actions.propose(
                conn, z["id"], role="person", app=app, action=spec["read"][0], inputs=spec["read"][1], actor="user:z"
            )
        assert refused.value.code in (403, 409)
    other = next(x for x in client.get(f"{base(z)}/integrations", headers=z["agent"]["h"]).json() if x["app"] == app)
    assert other["connection"] is None


@pytest.mark.parametrize("app", [a for a in APPS if "hook" in V[a]])
def test_vendor_webhooks_good_and_bad(client, real_env, app):
    spec = V[app]
    b = business(client, f"Hook {app}", people=("agent",))
    connect_simulated(b["id"], app, [spec["read"][0]])
    r = client.post(f"{base(b)}/integrations/{app}/hook", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    hook = r.json()
    path = urllib.parse.urlsplit(hook["url"]).path
    body = json.dumps(spec["hook_body"](hook["secret"])).encode()
    headers, query = spec["hook"](hook["secret"], body)
    url = path + ("?" + urllib.parse.urlencode(query) if query else "")
    ok = client.post(url, content=body, headers={"Content-Type": "application/json", **headers})
    assert ok.status_code == 200, ok.text
    assert ok.json()["events"] == 1
    # The same delivery again is accepted but recorded once.
    again = client.post(url, content=body, headers={"Content-Type": "application/json", **headers})
    assert again.status_code == 200 and again.json()["events"] == 0
    with db.tx() as conn:
        evs = conn.execute(
            "SELECT data FROM commai_events WHERE customer_id = %s AND type = 'integration.event'", (b["id"],)
        ).fetchall()
    assert len(evs) == 1 and evs[0]["data"]["app"] == app and evs[0]["data"]["type"].startswith(app.split("_")[0])

    # A wrong secret is refused and counted.
    bad_body = json.dumps(spec["hook_body"]("whsec_wrongwrongwrongwrongwrong")).encode()
    bh, bq = spec["hook"]("whsec_wrongwrongwrongwrongwrong", bad_body)
    bad = client.post(
        path + ("?" + urllib.parse.urlencode(bq) if bq else ""),
        content=bad_body,
        headers={"Content-Type": "application/json", **bh},
    )
    assert bad.status_code == 401
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT rejected_count FROM commai_inbound_hooks WHERE customer_id = %s", (b["id"],)
            ).fetchone()["rejected_count"]
            == 1
        )


def test_graph_validation_handshake(client):
    b = business(client, people=("agent",))
    connect_simulated(b["id"], "microsoft365", ["find_slots"])
    hook = client.post(f"{base(b)}/integrations/microsoft365/hook", headers=b["agent"]["h"]).json()
    path = urllib.parse.urlsplit(hook["url"]).path
    r = client.post(path + "?validationToken=Validation%3A+Testing+client+application")
    assert r.status_code == 200 and r.text == "Validation: Testing client application"
    assert r.headers["content-type"].startswith("text/plain")
    # Apps without notifications have no hook address.
    connect_simulated(b["id"], "dynamics365", ["find_contact"])
    assert client.post(f"{base(b)}/integrations/dynamics365/hook", headers=b["agent"]["h"]).status_code == 409


@pytest.mark.parametrize("app", APPS)
def test_vendor_stand_in_read_and_write(client, app):
    """Every vendor works end to end on its stand-in before it is switched on."""
    spec = V[app]
    b = business(client, f"Stand {app}", people=("agent",))
    _utc(b["id"])
    u, h = base(b), b["agent"]["h"]
    r = client.post(f"{u}/integrations/{app}/connect", headers=h)
    assert r.json()["connection"]["auth_method"] == "simulated"
    client.put(f"{u}/integrations/{app}/actions", json={"actions": [spec["read"][0], spec["write"][0]]}, headers=h)
    t = client.post(f"{u}/integrations/{app}/test", headers=h).json()
    assert t["test"]["ok"], t
    with db.tx() as conn:
        conn.execute("UPDATE integration_connections SET status = 'live' WHERE customer_id = %s", (b["id"],))
    write = dict(spec["write"][1])
    if app == "calendly":
        write["event_type"] = "https://standin.api.calendly.com/event_types/CONSULT30"
    run = propose_and_run(b["id"], app, spec["write"][0], write, f"s-{app}")
    assert run["status"] == "succeeded", run["error"]
    run = propose_and_run(b["id"], app, spec["read"][0], spec["read"][1], f"sr-{app}")
    assert run["status"] == "succeeded", run["error"]
    # A stand-in told to fail shows the cause, with no real call made.
    client.put(f"{u}/integrations/{app}/settings", json={"settings": {"simulate_failure": "permission"}}, headers=h)
    hl = client.get(f"{u}/integrations/{app}/health?check=true", headers=h).json()
    assert hl["cause"] == "permission"


def test_crm_specifics_on_stand_ins(client):
    b = business(client, people=("agent",))
    for app in ("salesforce", "dynamics365", "zoho_crm", "pipedrive"):
        connect_simulated(
            b["id"], app, ["find_contact", "create_contact", "create_lead", "create_deal", "update_contact"]
        )
    # Salesforce: SOQL paging through nextRecordsUrl, and opportunities found again by reference.
    for i in range(3):
        execute_direct(
            b["id"], "salesforce", "create_contact", {"name": f"Pat Lee{i}", "email": f"p{i}@x.org"}, f"sf{i}"
        )
    found = execute_direct(b["id"], "salesforce", "search_contacts", {"name": "Lee"}, "sf-s")["contacts"]
    assert len(found) == 3  # two pages
    deal = execute_direct(b["id"], "salesforce", "create_deal", {"title": "Fit-out", "amount": 1200}, "sf-d")
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects WHERE app = 'salesforce'")
    assert (
        execute_direct(b["id"], "salesforce", "create_deal", {"title": "Fit-out", "amount": 1200}, "sf-d")["deal_id"]
        == deal["deal_id"]
    )
    # Dynamics: the record id comes from the key; a second key, same email, finds the existing contact.
    c1 = execute_direct(b["id"], "dynamics365", "create_contact", {"name": "Ann Brown", "email": "ann@x.org"}, "d1")
    c2 = execute_direct(b["id"], "dynamics365", "create_contact", {"name": "Ann B", "email": "ann@x.org"}, "d2")
    assert c2["existing"] and c2["contact_id"] == c1["contact_id"]
    upd = execute_direct(
        b["id"], "dynamics365", "update_contact", {"contact_id": c1["contact_id"], "phone": "+1"}, "d3"
    )
    assert upd["updated"]
    with pytest.raises(connectors.ConnectorError) as e:
        execute_direct(
            b["id"],
            "dynamics365",
            "update_contact",
            {"contact_id": "00000000-0000-0000-0000-000000000009", "phone": "+1"},
            "d4",
        )
    assert e.value.cause == "input"
    # Zoho: upsert by email updates instead of duplicating; deals found again through COQL.
    z1 = execute_direct(b["id"], "zoho_crm", "create_lead", {"name": "Bo Diaz", "email": "bo@x.org"}, "z1")
    z2 = execute_direct(b["id"], "zoho_crm", "create_lead", {"name": "Bo Diaz", "email": "bo@x.org"}, "z2")
    assert z2["existing"] and z2["lead_id"] == z1["lead_id"]
    zd = execute_direct(b["id"], "zoho_crm", "create_deal", {"title": "Renewal"}, "zd")
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects WHERE app = 'zoho_crm'")
    assert execute_direct(b["id"], "zoho_crm", "create_deal", {"title": "Renewal"}, "zd")["deal_id"] == zd["deal_id"]
    # Pipedrive: a lead hangs off a person; found again by its reference after a lost record.
    pl = execute_direct(b["id"], "pipedrive", "create_lead", {"name": "Cy Ng", "email": "cy@x.org"}, "p1")
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects WHERE app = 'pipedrive'")
    again = execute_direct(b["id"], "pipedrive", "create_lead", {"name": "Cy Ng", "email": "cy@x.org"}, "p1")
    assert again["lead_id"] == pl["lead_id"] and again["person_id"] == pl["person_id"]


def test_mailbox_and_calendar_specifics_on_stand_ins(client):
    b = business(client, people=("agent",))
    _utc(b["id"])
    connect_simulated(b["id"], "microsoft365", ["find_slots", "book", "send_email", "cancel"])
    connect_simulated(b["id"], "gmail", ["send_email", "find_messages"])
    connect_simulated(b["id"], "calendly", ["list_event_types", "find_events", "cancel_event"])
    # Graph: transactionId de-duplicates; a booked slot is no longer free; mail found again in Sent Items.
    bk = execute_direct(b["id"], "microsoft365", "book", {"start": slot(14), "name": "Ann", "contact": "a@x.org"}, "m1")
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects WHERE app = 'microsoft365'")
    bk2 = execute_direct(
        b["id"], "microsoft365", "book", {"start": slot(14), "name": "Ann", "contact": "a@x.org"}, "m1"
    )
    assert bk2["booking_id"] == bk["booking_id"] or "already booked" in str(bk2)
    free = execute_direct(b["id"], "microsoft365", "find_slots", {"date": TOMORROW.isoformat()}, "m2")["slots"]
    assert all(dt.datetime.fromisoformat(s) != dt.datetime.fromisoformat(slot(14)) for s in free)
    mail = {"to": "a@x.org", "subject": "Hi", "body": "Hello"}
    execute_direct(b["id"], "microsoft365", "send_email", mail, "m3")
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects WHERE app = 'microsoft365'")
    assert execute_direct(b["id"], "microsoft365", "send_email", mail, "m3")["replayed"]
    # Gmail: the Message-ID comes from the key; a retry finds the sent mail (rfc822msgid).
    g1 = execute_direct(b["id"], "gmail", "send_email", mail, "g1")
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects WHERE app = 'gmail'")
    g2 = execute_direct(b["id"], "gmail", "send_email", mail, "g1")
    assert g2["replayed"] and g2["gmail_id"] == g1["gmail_id"]
    # Calendly: event types paged through pagination.next_page.
    types = execute_direct(b["id"], "calendly", "list_event_types", {}, "c1")["event_types"]
    assert len(types) == 3
