"""Every connector, live against local fake servers: the payload each sends,
dedup and resolve for PagerDuty, ServiceNow, Jira and Opsgenie, retries
with backoff, syslog over UDP, TCP and TLS, SNMP traps, and the simulated
mode that is the default until credentials exist."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest

from exaconnect_controller import db
from exaconnect_controller.integrations import cloudevents, publish
from exaconnect_controller.integrations.providers import snmp

from .integrations_helpers import Tcp, Udp, add, deliveries, other_org, run_jobs, self_signed

T = dt.datetime(2026, 10, 7, 12, 0, 0, tzinfo=dt.UTC)
N = 0


def fire(cid: str, name: str = "path.down", *, action=None, severity=None, dedup="path:s1:carrier-a", data=None):
    global N
    N += 1
    with db.tx() as conn:
        eid = publish.emit(
            conn,
            name,
            customer_id=cid,
            data=data or {"site": "kingston", "path": "carrier-a", "carrier": "Carrier A", "summary": "Carrier A down"},
            event_id=f"evt-{N}",
            time=T,
            severity=severity,
            action=action,
            dedup_key=dedup,
        )
    run_jobs()
    return eid


@pytest.fixture(autouse=True)
def _numbering():
    global N
    N = 0


@pytest.fixture
def org(client):
    return other_org(client, "Island Bank")


def site(cid: str, name: str) -> str:
    global N
    N += 1
    with db.tx() as conn:
        return str(
            conn.execute(
                "INSERT INTO sites (customer_id, name, kind, asn, overlay_host) VALUES (%s, %s, 'site', %s, %s)"
                " RETURNING id",
                (cid, name, 65100 + N, 200 + N),
            ).fetchone()["id"]
        )


def _status(integ) -> list[str]:
    return [d["status"] for d in deliveries(integ["id"])]


# ---- webhooks: CloudEvents with Standard Webhooks signatures ---------------------------------


def test_webhook_structured_signed(client, org, live, fake):
    w = add(client, org["h"], "webhook", secrets={"url": fake.url + "/hook"})
    secret = w["signing_secret"]
    assert secret.startswith("whsec_") and "signing_secret" in w["secrets_set"] and w["mode"] == "live"
    fire(org["id"])
    [req] = fake.requests
    assert req["path"] == "/hook" and req["headers"]["content-type"].startswith("application/cloudevents+json")
    assert cloudevents.verify(secret, req["headers"], req["raw"], now=int(req["headers"]["webhook-timestamp"]))
    ce = req["json"]
    assert ce["specversion"] == "1.0" and ce["type"] == "com.exacarib.connect.path.down" and ce["id"] == "evt-1"
    assert ce["organisationid"] == org["id"] and ce["data"]["carrier"] == "Carrier A"
    assert _status(w) == ["delivered"]
    # The secret never comes back.
    got = client.get(f"/api/v1/integrations/{w['id']}", headers=org["h"]).json()
    assert "signing_secret" not in got and secret not in json.dumps(got) and fake.url not in json.dumps(got)
    log = client.get(f"/api/v1/integrations/{w['id']}/deliveries", headers=org["h"]).json()
    assert secret not in json.dumps(log) and "/hook" not in json.dumps(log)


def test_webhook_binary_mode(client, org, live, fake):
    w = add(
        client,
        org["h"],
        "webhook",
        config={"mode": "binary"},
        secrets={"url": fake.url + "/b", "signing_secret": cloudevents.new_secret()},
    )
    assert "signing_secret" not in w  # given, so not shown
    fire(org["id"])
    req = fake.requests[0]
    h = req["headers"]
    assert h["ce-specversion"] == "1.0" and h["ce-type"] == "com.exacarib.connect.path.down" and h["ce-id"] == "evt-1"
    assert h["content-type"] == "application/json" and req["json"]["site"] == "kingston"
    assert h["webhook-id"] == "evt-1" and h["webhook-signature"].startswith("v1,")


def test_tmf688_listener_gets_tmf_events(client, org, live, fake):
    t = add(client, org["h"], "tmf688", secrets={"url": fake.url + "/listener"})
    fire(org["id"])
    body = fake.requests[0]["json"]
    assert body["@type"] == "Event" and body["eventType"] == "ExaCaribConnectPathDown"
    assert (
        body["correlationId"] == "path:s1:carrier-a"
        and body["priority"] == "1"
        and body["event"]["path"] == "carrier-a"
    )
    assert _status(t) == ["delivered"]


# ---- chat ------------------------------------------------------------------------------


def test_slack_webhook_and_web_api(client, org, live, fake):
    add(client, org["h"], "slack", secrets={"webhook_url": fake.url + "/services/T/B/X"})
    fake.reply("POST", "/api/chat.postMessage", 200, {"ok": True, "ts": "1.2"})
    api = add(
        client,
        org["h"],
        "slack",
        name="bot",
        config={"mode": "api", "channel": "C0123456789", "base_url": fake.url + "/api"},
        secrets={"bot_token": "xoxb-test-token"},
    )
    fire(org["id"])
    hook = next(r for r in fake.requests if r["path"].startswith("/services"))
    assert hook["json"]["text"].startswith("Path down") and hook["json"]["blocks"][0]["type"] == "header"
    bot = next(r for r in fake.requests if r["path"] == "/api/chat.postMessage")
    assert bot["headers"]["authorization"] == "Bearer xoxb-test-token" and bot["json"]["channel"] == "C0123456789"
    # Slack answers 200 with ok:false for errors.
    fake.reply("POST", "/api/chat.postMessage", 200, {"ok": False, "error": "channel_not_found"})
    fire(org["id"], "path.up", dedup="x")
    d = deliveries(api["id"])[-1]
    assert (
        d["status"] == "failed"
        and "channel_not_found" in d["last_error"]
        and "xoxb-test-token" not in json.dumps(d, default=str)
    )


def test_teams_adaptive_card(client, org, live, fake):
    add(client, org["h"], "teams", secrets={"workflow_url": fake.url + "/workflows/abc"})
    fire(org["id"])
    body = fake.requests[0]["json"]
    att = body["attachments"][0]
    assert body["type"] == "message" and att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4" and card["$schema"].startswith("http")


# ---- alerting: dedup and resolve -----------------------------------------------------------


def test_pagerduty_trigger_then_resolve_with_the_same_dedup_key(client, org, live, fake):
    fake.reply("POST", "/v2/enqueue", 202, {"status": "success", "dedup_key": "path:s1:carrier-a"})
    pd = add(
        client,
        org["h"],
        "pagerduty",
        config={"base_url": fake.url},
        secrets={"routing_key": "R0UTINGKEY0000000000000000000000"},
    )
    fire(org["id"], "path.down")
    fire(org["id"], "path.up")
    fire(org["id"], "storm.on", dedup="storm:x")  # news: a change event
    trig, res = [r["json"] for r in fake.requests if r["path"] == "/v2/enqueue"]
    assert trig["event_action"] == "trigger" and trig["dedup_key"] == "path:s1:carrier-a"
    assert trig["routing_key"] == "R0UTINGKEY0000000000000000000000"
    p = trig["payload"]
    assert p["severity"] == "critical" and p["source"] == "kingston" and p["component"] == "carrier-a"
    assert p["class"] == "path.down" and p["timestamp"] == "2026-10-07T12:00:00Z" and p["summary"]
    assert res == {
        "routing_key": "R0UTINGKEY0000000000000000000000",
        "event_action": "resolve",
        "dedup_key": "path:s1:carrier-a",
    }
    change = next(r for r in fake.requests if r["path"] == "/v2/change/enqueue")
    assert "event_action" not in change["json"] and change["json"]["payload"]["source"] == "ExaCarib Connect"
    with db.tx() as conn:
        inc = conn.execute("SELECT * FROM connect_incidents WHERE integration_id = %s", (pd["id"],)).fetchone()
    assert inc["dedup_key"] == "path:s1:carrier-a" and inc["state"] == "resolved"
    # The routing key never reaches the delivery log.
    for d in deliveries(pd["id"]):
        assert "R0UTINGKEY" not in json.dumps(d, default=str)


def test_opsgenie_alias_and_close(client, org, live, fake):
    og = add(
        client,
        org["h"],
        "opsgenie",
        config={"base_url": fake.url, "responders": ["NOC"]},
        secrets={"api_key": "og-key"},
    )
    fake.reply("POST", "/v2/alerts", 202, {"result": "Request will be processed", "requestId": "r1"})
    fire(org["id"], "path.down")
    fire(org["id"], "path.up")
    fire(org["id"], "storm.on", dedup="s")
    create, close = fake.requests
    assert create["path"] == "/v2/alerts" and create["headers"]["authorization"] == "GenieKey og-key"
    assert create["json"]["alias"] == "path:s1:carrier-a" and create["json"]["priority"] == "P1"
    assert create["json"]["responders"] == [{"name": "NOC", "type": "team"}]
    assert close["path"] == "/v2/alerts/path%3As1%3Acarrier-a/close?identifierType=alias"
    assert _status(og) == ["delivered", "delivered", "skipped"]


def test_servicenow_create_update_resolve(client, org, live, fake):
    fake.reply(
        "POST", "/api/now/table/incident", 201, {"result": {"sys_id": "a" * 32, "number": "INC0012345", "state": "1"}}
    )
    fake.reply("PATCH", "/api/now/table/incident/", 200, {"result": {"sys_id": "a" * 32}})
    sn = add(
        client, org["h"], "servicenow",
        config={"instance_url": fake.url, "username": "connect.api", "assignment_group": "Network"},
        secrets={"password": "pw-123"},
    )  # fmt: skip
    fire(org["id"], "path.down")
    fire(org["id"], "path.down", data={"site": "kingston", "path": "carrier-a", "summary": "Still down"})
    fire(org["id"], "path.up")
    fire(org["id"], "path.up")  # nothing open any more
    create, update, resolve = fake.requests
    assert create["method"] == "POST" and create["path"] == "/api/now/table/incident?sysparm_fields=sys_id,number,state"
    assert create["headers"]["authorization"] == "Basic " + base64.b64encode(b"connect.api:pw-123").decode()
    c = create["json"]
    assert c["correlation_id"] == "path:s1:carrier-a" and c["urgency"] == "1" and c["assignment_group"] == "Network"
    assert c["category"] == "network" and c["short_description"].startswith("Path down")
    assert update["method"] == "PATCH" and update["path"] == f"/api/now/table/incident/{'a' * 32}"
    assert "Still down" in update["json"]["work_notes"]
    assert resolve["method"] == "PATCH" and resolve["json"]["state"] == "6"
    assert resolve["json"]["close_code"] == "Solution provided" and resolve["json"]["close_notes"].startswith(
        "Resolved by"
    )
    assert _status(sn) == ["delivered", "delivered", "delivered", "skipped"]


def test_jira_create_comment_and_resolve_by_finding_the_transition(client, org, live, fake):
    fake.reply("POST", "/rest/servicedeskapi/request", 201, {"issueKey": "NOC-7"})
    fake.reply(
        "GET",
        "/rest/servicedeskapi/request/NOC-7/transition",
        200,
        {"values": [{"id": "5", "name": "Start"}, {"id": "11", "name": "Resolve this issue"}]},
    )
    fake.reply("POST", "/rest/servicedeskapi/request/NOC-7/transition", 204, b"")
    fake.reply("POST", "/rest/servicedeskapi/request/NOC-7/comment", 201, {"id": "1"})
    add(
        client, org["h"], "jira",
        config={"site_url": fake.url, "email": "noc@bank.example", "service_desk_id": 4, "request_type_id": "21"},
        secrets={"api_token": "atl-token"},
    )  # fmt: skip
    fire(org["id"], "path.down")
    fire(org["id"], "path.down", data={"site": "kingston", "summary": "again"})
    fire(org["id"], "path.up")
    create, comment, listing, transition = fake.requests
    assert create["json"]["serviceDeskId"] == "4" and create["json"]["requestTypeId"] == "21"
    assert create["json"]["requestFieldValues"]["summary"].startswith("Path down")
    assert create["headers"]["authorization"] == "Basic " + base64.b64encode(b"noc@bank.example:atl-token").decode()
    assert comment["path"].endswith("/NOC-7/comment") and comment["json"]["public"] is False
    assert listing["method"] == "GET" and transition["json"]["id"] == "11"


# ---- monitoring and SIEM ---------------------------------------------------------------------


def test_datadog_event(client, org, live, fake):
    add(client, org["h"], "datadog", config={"base_url": fake.url}, secrets={"api_key": "dd-key"})
    fake.reply("POST", "/api/v1/events", 202, {"status": "ok", "event": {"id": 9}})
    fire(org["id"])
    fire(org["id"], "path.up")
    a, b = fake.requests
    assert a["path"] == "/api/v1/events" and a["headers"]["dd-api-key"] == "dd-key"
    assert a["json"]["alert_type"] == "error" and a["json"]["aggregation_key"] == "path:s1:carrier-a"
    assert a["json"]["date_happened"] == int(T.timestamp()) and "site:kingston" in a["json"]["tags"]
    assert b["json"]["alert_type"] == "success"


def test_splunk_hec(client, org, live, fake):
    add(client, org["h"], "splunk", config={"url": fake.url, "index": "network"}, secrets={"token": "hec-token"})
    fake.reply("POST", "/services/collector/event", 200, {"text": "Success", "code": 0})
    fire(org["id"])
    r = fake.requests[0]
    assert r["headers"]["authorization"] == "Splunk hec-token" and r["json"]["index"] == "network"
    assert r["json"]["sourcetype"] == "exacarib:connect" and r["json"]["event"]["type"].endswith("path.down")
    assert r["json"]["time"] == T.timestamp()


def test_elastic_bulk_ndjson_and_conflict_is_fine(client, org, live, fake):
    e = add(client, org["h"], "elastic", config={"url": fake.url, "index": "connect"}, secrets={"api_key": "aWQ6a2V5"})
    fake.queue("POST", "/_bulk", (200, {"errors": True, "items": [{"create": {"status": 409}}]}))
    fire(org["id"])
    r = fake.requests[0]
    assert r["headers"]["content-type"] == "application/x-ndjson" and r["headers"]["authorization"] == "ApiKey aWQ6a2V5"
    action, doc, tail = r["raw"].split(b"\n")
    assert tail == b"" and json.loads(action) == {"create": {"_index": "connect", "_id": "evt-1"}}
    doc = json.loads(doc)
    assert doc["@timestamp"] == "2026-10-07T12:00:00Z" and doc["event"]["dataset"] == "exacarib.connect"
    assert _status(e) == ["delivered"]
    fake.queue(
        "POST",
        "/_bulk",
        (200, {"errors": True, "items": [{"create": {"status": 400, "error": {"reason": "mapping"}}}]}),
    )
    fire(org["id"], "path.up")
    assert deliveries(e["id"])[-1]["status"] == "failed"


def test_opensearch_basic_auth(client, org, live, fake):
    add(client, org["h"], "elastic", config={"url": fake.url, "username": "connect"}, secrets={"password": "os-pw"})
    fake.reply("POST", "/_bulk", 200, {"errors": False, "items": []})
    fire(org["id"])
    assert fake.requests[0]["headers"]["authorization"] == "Basic " + base64.b64encode(b"connect:os-pw").decode()


def test_sentinel_logs_ingestion(client, org, live, fake):
    fake.reply("POST", "/tenant-1/oauth2/v2.0/token", 200, {"access_token": "eyJ.sentinel", "expires_in": 3600})
    s = add(
        client, org["h"], "sentinel",
        config={"tenant_id": "tenant-1", "client_id": "app-1", "endpoint": fake.url + "/dce",
                "dcr_id": "dcr-abc", "login_url": fake.url},
        secrets={"client_secret": "cs-1"},
    )  # fmt: skip
    fire(org["id"])
    tok, post = fake.requests
    assert tok["raw"].decode().count("grant_type=client_credentials") == 1
    assert "scope=https%3A%2F%2Fmonitor.azure.com%2F%2F.default" in tok["raw"].decode()
    assert post["path"] == "/dce/dataCollectionRules/dcr-abc/streams/Custom-ExaCaribConnect_CL?api-version=2023-01-01"
    assert post["headers"]["authorization"] == "Bearer eyJ.sentinel"
    rec = post["json"][0]
    assert rec["EventType"] == "path.down" and rec["Site"] == "kingston" and rec["OrganisationId"] == org["id"]
    d = deliveries(s["id"])[0]
    assert "eyJ.sentinel" not in json.dumps(d, default=str) and "cs-1" not in json.dumps(d, default=str)


def test_otlp_logs_and_grafana_cloud(client, org, live, fake):
    add(
        client,
        org["h"],
        "otlp",
        config={"endpoint": fake.url + "/otel", "header_name": "X-Key"},
        secrets={"header_value": "k1"},
    )
    add(
        client,
        org["h"],
        "grafana_cloud",
        config={"endpoint": fake.url + "/otlp", "instance_id": "12345"},
        secrets={"token": "glc_x"},
    )
    fire(org["id"])
    o = next(r for r in fake.requests if r["path"] == "/otel/v1/logs")
    assert o["headers"]["x-key"] == "k1" and o["json"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    g = next(r for r in fake.requests if r["path"] == "/otlp/v1/logs")
    assert g["headers"]["authorization"] == "Basic " + base64.b64encode(b"12345:glc_x").decode()


def test_otlp_metrics_export(client, org, live, fake):
    from exaconnect_controller.integrations import runner

    add(client, org["h"], "otlp", config={"endpoint": fake.url}, event_types=["path.*"])
    site(org["id"], "kingston")
    n = runner.export_metrics()
    assert n >= 1
    r = next(r for r in fake.requests if r["path"] == "/v1/metrics")
    names = {m["name"] for m in r["json"]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    assert "exacarib.site.storm_mode" in names


# ---- syslog and SNMP -------------------------------------------------------------------------


def test_syslog_udp(client, org, live):
    u = Udp()
    try:
        add(client, org["h"], "syslog", config={"host": "127.0.0.1", "port": u.port, "transport": "udp"})
        fire(org["id"])
        msg = u.recv().decode()
        assert msg.startswith("<130>1 2026-10-07T12:00:00Z exacarib-connect connect - path.down [exacarib@32473 ")
    finally:
        u.close()


def test_syslog_tcp_octet_counted(client, org, live):
    t = Tcp()
    try:
        add(
            client,
            org["h"],
            "syslog",
            config={"host": "127.0.0.1", "port": t.port, "transport": "tcp", "facility": "local7"},
        )
        fire(org["id"])
        assert t.done.wait(5)
        n, _, msg = t.received[0].partition(b" ")
        assert int(n) == len(msg) and msg.startswith(b"<186>1 ")
    finally:
        t.close()


def test_syslog_tls_with_a_private_ca(client, org, live, tmp_path):
    ctx, pem = self_signed(tmp_path)
    t = Tcp(tls=ctx)
    try:
        s = add(
            client, org["h"], "syslog", config={"host": "127.0.0.1", "port": t.port, "transport": "tls", "ca_pem": pem}
        )
        fire(org["id"])
        assert t.done.wait(5)
        assert t.received, (t.errors, deliveries(s["id"]))
        n, _, msg = t.received[0].partition(b" ")
        assert int(n) == len(msg) and b"path.down" in msg
    finally:
        t.close()


def test_syslog_tls_refuses_an_untrusted_collector(client, org, live, tmp_path):
    ctx, _ = self_signed(tmp_path)
    t = Tcp(tls=ctx)
    try:
        s = add(client, org["h"], "syslog", config={"host": "127.0.0.1", "port": t.port, "transport": "tls"})
        fire(org["id"])
        d = deliveries(s["id"])[0]
        assert d["status"] == "pending" and d["last_error"]  # retried later, not delivered
    finally:
        t.close()


def test_snmp_trap(client, org, live):
    u = Udp()
    try:
        s = add(client, org["h"], "snmp", config={"host": "127.0.0.1", "port": u.port}, secrets={"community": "n0c-ro"})
        fire(org["id"])
        version, community, (_, _, _, vbs) = snmp.decode(u.recv())
        assert version == 1 and community == "n0c-ro"
        assert dict(vbs)["1.3.6.1.4.1.32473.1.2.2"] == "path.down"
        fire(org["id"], "storm.on", dedup="s")  # not a trap
        assert _status(s) == ["delivered", "skipped"]
        assert "n0c-ro" not in json.dumps(deliveries(s["id"]), default=str)
    finally:
        u.close()


# ---- retries, backoff and failures --------------------------------------------------------


def _job(delivery_id):
    with db.tx() as conn:
        return conn.execute(
            "SELECT *, extract(epoch FROM run_after - now()) AS wait FROM jobs WHERE kind = 'integrations.deliver'"
            " AND payload->>'delivery_id' = %s",
            (str(delivery_id),),
        ).fetchone()


def _due(job_id):
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job_id,))


def test_retries_with_exponential_backoff_then_delivers(client, org, live, fake):
    w = add(client, org["h"], "webhook", secrets={"url": fake.url + "/flaky"})
    fake.queue("POST", "/flaky", (503, {}), (429, {}), (500, {}))
    fake.reply("POST", "/flaky", 200, {})
    fire(org["id"])
    d = deliveries(w["id"])[0]
    assert d["status"] == "pending" and d["attempts"] == 1 and d["response_code"] == 503
    waits = []
    for _ in range(3):
        job = _job(d["id"])
        waits.append(round(float(job["wait"])))
        _due(job["id"])
        run_jobs()
    assert waits == [10, 20, 40]
    d = deliveries(w["id"])[0]
    assert d["status"] == "delivered" and d["attempts"] == 4 and len(fake.requests) == 4
    # Every attempt carried the same event id, so a receiver can de-duplicate.
    assert {r["headers"]["webhook-id"] for r in fake.requests} == {"evt-1"}


def test_a_client_error_stops_at_once(client, org, live, fake):
    w = add(client, org["h"], "webhook", secrets={"url": fake.url + "/gone"})
    fake.reply("POST", "/gone", 410, {"error": "gone"})
    fire(org["id"])
    d = deliveries(w["id"])[0]
    assert d["status"] == "failed" and d["response_code"] == 410 and len(fake.requests) == 1
    r = client.post(f"/api/v1/integrations/{w['id']}/deliveries/{d['id']}/retry", headers=org["h"])
    assert r.status_code == 200
    fake.reply("POST", "/gone", 200, {})
    run_jobs()
    assert deliveries(w["id"])[0]["status"] == "delivered"


def test_gives_up_after_six_attempts(client, org, live, fake):
    w = add(client, org["h"], "webhook", secrets={"url": fake.url + "/down"})
    fake.reply("POST", "/down", 502, {})
    fire(org["id"])
    d = deliveries(w["id"])[0]
    for _ in range(6):
        job = _job(d["id"])
        if job["status"] != "queued":
            break
        _due(job["id"])
        run_jobs()
    d = deliveries(w["id"])[0]
    assert d["status"] == "failed" and d["attempts"] == 6 and len(fake.requests) == 6
    assert _job(d["id"])["status"] == "done"  # the last attempt records the failure itself


def test_unreachable_endpoint_is_retried(client, org, live):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here
    w = add(client, org["h"], "webhook", secrets={"url": f"http://127.0.0.1:{port}/x"})
    fire(org["id"])
    d = deliveries(w["id"])[0]
    assert d["status"] == "pending" and d["last_error"] and f"127.0.0.1:{port}/x" not in d["last_error"]


def test_private_addresses_are_refused_without_the_lab_switch(client, org, live, fake, monkeypatch):
    w = add(client, org["h"], "webhook", secrets={"url": fake.url + "/x"})
    monkeypatch.delenv("EXA_WEBHOOK_ALLOW_PRIVATE")
    fire(org["id"])
    assert not fake.requests and deliveries(w["id"])[0]["status"] in ("pending", "failed")


# ---- simulated by default -----------------------------------------------------------------


def test_simulated_until_live_records_the_outbox_without_secrets(client, org, vault, fake):
    pd = add(client, org["h"], "pagerduty", secrets={"routing_key": "R" * 32})
    assert pd["mode"] == "simulated"
    fire(org["id"])
    assert not fake.requests  # nothing left the machine
    d = deliveries(pd["id"])[0]
    assert d["status"] == "simulated" and d["detail"]["mode"] == "simulated"
    out = client.get(f"/api/v1/integrations/{pd['id']}/outbox", headers=org["h"]).json()
    assert (
        out[0]["target"] == "https://events.pagerduty.com/v2/enqueue"
        and json.loads(out[0]["body"])["routing_key"] == "[secret]"
    )
    assert "R" * 32 not in json.dumps(out)
    # The simulated incident still opens, so a resolve closes it.
    fire(org["id"], "path.up")
    with db.tx() as conn:
        assert conn.execute("SELECT state FROM connect_incidents").fetchone()["state"] == "resolved"


def test_test_send_and_switching_off(client, org, live, fake):
    w = add(client, org["h"], "webhook", secrets={"url": fake.url + "/t"}, event_types=["path.*"])
    r = client.post(f"/api/v1/integrations/{w['id']}/test", headers=org["h"])
    assert r.status_code == 200 and r.json()["status"] == "delivered" and r.json()["test"] is True
    assert fake.requests[0]["json"]["type"] == "com.exacarib.connect.test.ping"
    client.patch(f"/api/v1/integrations/{w['id']}", json={"enabled": False}, headers=org["h"])
    fire(org["id"])
    assert len(fake.requests) == 1  # off: nothing queued


def test_subscription_filters_kind_severity_and_site(client, org, live, fake):
    s1, s2 = site(org["id"], "a"), site(org["id"], "b")
    add(
        client,
        org["h"],
        "webhook",
        secrets={"url": fake.url + "/f"},
        event_types=["path.*", "storm.on"],
        min_severity="warning",
        site_ids=[s1],
    )
    with db.tx() as conn:
        for i, (name, sid, sev) in enumerate(
            [
                ("path.down", s1, None),
                ("path.down", s2, None),
                ("path.up", s1, "info"),
                ("node.offline", s1, None),
                ("storm.on", s1, None),
            ]
        ):
            publish.emit(conn, name, customer_id=org["id"], data={}, event_id=f"f{i}", site_id=sid, severity=sev)
    run_jobs()
    assert sorted(r["json"]["id"] for r in fake.requests) == ["f0", "f4"]


def test_one_organisation_never_receives_anothers_events(client, org, live, fake):
    other = other_org(client, "Harbour Co")
    add(client, other["h"], "webhook", secrets={"url": fake.url + "/other"})
    add(client, org["h"], "webhook", secrets={"url": fake.url + "/mine"})
    fire(org["id"])
    assert [r["path"] for r in fake.requests] == ["/mine"]
    # And neither can read the other's integrations.
    mine = client.get("/api/v1/integrations", headers=org["h"]).json()
    assert len(mine) == 1 and client.get(f"/api/v1/integrations/{mine[0]['id']}", headers=other["h"]).status_code == 404
