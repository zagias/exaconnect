"""Integration standards without a database: CloudEvents, Standard Webhooks,
syslog framing, SNMP encoding, OTLP payloads, the catalogue, AsyncAPI,
SigV4 and the maintenance rule in the routing engine."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json

import pytest

from exaconnect_controller.integrations import asyncapi, catalogue, cloudevents, metrics, restconf
from exaconnect_controller.integrations.adapters import clouds
from exaconnect_controller.integrations.providers import otlp, snmp, syslog
from exaconnect_controller.routing.engine import PathInput, Policy, Sla, Window, decide, evaluate
from exaconnect_controller.routing.forecast import TrendForecaster

T = dt.datetime(2026, 10, 7, 12, 0, 0, tzinfo=dt.UTC)


def _row(name="path.down", **kw):
    return {
        "id": "evt-1",
        "type": catalogue.type_of(name),
        "source": "/exacarib/connect/organisations/o1",
        "subject": "sites/kingston/paths/carrier-a",
        "time": T,
        "customer_id": "0000-1",
        "severity": "critical",
        "action": "trigger",
        "dedup_key": "path:s1:carrier-a",
        "data": {"site": "kingston", "path": "carrier-a", "carrier": "Carrier A", "summary": "Carrier A is down"},
        **kw,
    }


# ---- Standard Webhooks ---------------------------------------------------------------


def test_standard_webhooks_reference_vector():
    # The signing example from the Standard Webhooks specification.
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    body = b'{"test": 2432232314}'
    sig = cloudevents.sign(secret, "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, body)
    assert sig == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    headers = {
        "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
        "webhook-timestamp": "1614265330",
        "webhook-signature": f"v1,bm90IGl0 {sig}",  # several signatures while a secret rotates
    }
    assert cloudevents.verify(secret, headers, body, now=1614265330 + 10)
    assert not cloudevents.verify(secret, headers, body + b" ", now=1614265330)
    assert not cloudevents.verify(secret, headers, body, now=1614265330 + 301)  # too old
    assert not cloudevents.verify("whsec_" + base64.b64encode(b"other").decode(), headers, body, now=1614265330)
    assert not cloudevents.verify(secret, {**headers, "webhook-timestamp": "soon"}, body, now=1614265330)


def test_signed_headers_and_new_secret():
    secret = cloudevents.new_secret()
    assert secret.startswith("whsec_") and len(base64.b64decode(secret[6:])) == 24
    h = cloudevents.signed(secret, "evt-1", {"Content-Type": "application/json"}, b"{}", now=1700000000)
    assert h["webhook-id"] == "evt-1" and h["webhook-timestamp"] == "1700000000"
    mac = hmac.new(base64.b64decode(secret[6:]), b"evt-1.1700000000.{}", hashlib.sha256).digest()
    assert h["webhook-signature"] == "v1," + base64.b64encode(mac).decode()


# ---- CloudEvents -------------------------------------------------------------------


def test_cloudevent_structured_and_binary_round_trip():
    ce = cloudevents.event(_row())
    assert ce["specversion"] == "1.0" and ce["type"] == "com.exacarib.connect.path.down"
    assert ce["time"] == "2026-10-07T12:00:00Z" and ce["subject"] == "sites/kingston/paths/carrier-a"
    assert ce["dedupkey"] == "path:s1:carrier-a" and ce["severity"] == "critical" and ce["action"] == "trigger"
    # Extension attribute names: lower-case letters and digits only (CloudEvents §3.1.1).
    for k in ce:
        assert k.isalnum() and k == k.lower()
    headers, body = cloudevents.structured(ce)
    assert headers["Content-Type"].startswith("application/cloudevents+json")
    assert json.loads(body) == json.loads(json.dumps(ce, default=str))
    headers, body = cloudevents.binary(ce)
    assert headers["ce-specversion"] == "1.0" and headers["ce-id"] == "evt-1"
    assert headers["ce-type"] == ce["type"] and headers["Content-Type"] == "application/json"
    assert "ce-data" not in headers and json.loads(body) == ce["data"]
    back = cloudevents.from_binary(headers, body)
    assert back["id"] == "evt-1" and back["data"]["site"] == "kingston" and back["dedupkey"] == ce["dedupkey"]


# ---- the catalogue and AsyncAPI ------------------------------------------------------------


def test_catalogue_matching_and_validation():
    assert catalogue.matches(["*"], "path.down")
    assert not catalogue.matches(["*"], "audit.recorded")  # audit only by name
    assert catalogue.matches(["audit.recorded"], "audit.recorded")
    assert catalogue.matches(["path.*"], "path.up") and not catalogue.matches(["path.*"], "node.offline")
    assert catalogue.valid_patterns(["*", "path.*", "sla.breach", "carrier.*"]) == []
    assert catalogue.valid_patterns(["paths.*", "nope"]) == ["paths.*", "nope"]
    names = set(catalogue.KINDS)
    for needed in (
        "path.down", "path.up", "sla.breach_forecast", "sla.breach", "routing.moved", "storm.on", "storm.off",
        "hazard.alert", "node.enrolled", "node.revoked", "node.offline", "config.apply_failed", "ddos.blocked",
        "carrier.fault", "carrier.maintenance",
    ):  # fmt: skip
        assert needed in names
    # Every trigger has a resolve in its family, so alerts can close.
    assert catalogue.KINDS["path.down"].action == "trigger" and catalogue.KINDS["path.up"].action == "resolve"
    assert catalogue.KINDS["node.offline"].action == "trigger" and catalogue.KINDS["node.online"].action == "resolve"


def test_asyncapi_document_lists_every_event():
    doc = asyncapi.document()
    assert doc["asyncapi"] == "3.0.0"
    msgs = doc["components"]["messages"]
    assert len(msgs) == len(catalogue.KINDS)
    assert msgs["PathDown"]["name"] == "com.exacarib.connect.path.down"
    assert msgs["PathDown"]["examples"][0]["payload"]["specversion"] == "1.0"
    assert set(doc["channels"]["webhook"]["messages"]) == set(msgs)
    assert "webhook-signature" in doc["components"]["schemas"]["StandardWebhooksHeaders"]["properties"]


# ---- syslog ------------------------------------------------------------------------------


def test_syslog_rfc5424_format_and_framing():
    ce = cloudevents.event(_row(data={"site": 'king"ston]', "path": "carrier-a", "summary": "Carrier A is down"}))
    msg = syslog.format_5424(ce, facility="local0", hostname="connect host")
    # PRI = local0 (16) * 8 + crit (2) = 130, version 1.
    assert msg.startswith("<130>1 2026-10-07T12:00:00Z connecthost connect - path.down [exacarib@32473 ")
    assert 'site="king\\"ston\\]"' in msg  # SD-PARAM escaping (RFC 5424 §6.3.3)
    assert msg.endswith("﻿Carrier A is down")
    info = syslog.format_5424(cloudevents.event(_row(severity="info")), facility="local7")
    assert info.startswith("<190>1 ")  # 23 * 8 + 6
    assert syslog.frame(msg, "udp") == msg.encode()
    framed = syslog.frame(msg, "tcp")
    n, _, rest = framed.partition(b" ")
    assert int(n) == len(rest) == len(msg.encode()) and rest == msg.encode()  # octet counting, bytes not characters


# ---- SNMP --------------------------------------------------------------------------------


def test_snmp_v2c_trap_encodes_and_decodes():
    ce = cloudevents.event(_row())
    raw = snmp.trap_v2c("public", ce, request_id=7, uptime_cs=12345)
    version, community, pdu = snmp.decode(raw)
    assert version == 1 and community == "public"  # SNMPv2c
    assert raw[raw.index(b"public") + 6] == 0xA7  # SNMPv2-Trap-PDU
    req_id, err, idx, varbinds = pdu
    assert (req_id, err, idx) == (7, 0, 0)
    vb = dict((o, v) for o, v in varbinds)
    assert vb["1.3.6.1.2.1.1.3.0"] == 12345
    assert vb["1.3.6.1.6.3.1.1.4.1.0"] == "1.3.6.1.4.1.32473.1.0.1"  # path.down trap
    assert vb["1.3.6.1.4.1.32473.1.2.2"] == "path.down"
    assert vb["1.3.6.1.4.1.32473.1.2.3"] == 3  # critical
    assert vb["1.3.6.1.4.1.32473.1.2.4"] == "kingston"
    # Long lengths and large OID arcs encode correctly.
    long = snmp.octets("x" * 300)
    assert long[:4] == b"\x04\x82\x01\x2c"
    assert snmp.decode(snmp.oid("1.3.6.1.4.1.32473.99999")) == "1.3.6.1.4.1.32473.99999"


# ---- OTLP --------------------------------------------------------------------------------


def test_otlp_logs_payload():
    ce = cloudevents.event(_row())
    body = otlp.logs_payload([ce])
    rl = body["resourceLogs"][0]
    attrs = {a["key"]: a["value"] for a in rl["resource"]["attributes"]}
    assert attrs["service.name"] == {"stringValue": "exacarib-connect"}
    rec = rl["scopeLogs"][0]["logRecords"][0]
    assert rec["timeUnixNano"] == str(int(T.timestamp() * 1e9))
    assert rec["severityNumber"] == 17 and rec["severityText"] == "ERROR"
    assert rec["body"] == {"stringValue": "Carrier A is down"}
    ra = {a["key"]: a["value"] for a in rec["attributes"]}
    assert ra["event.name"] == {"stringValue": "path.down"}
    assert ra["exacarib.dedup_key"] == {"stringValue": "path:s1:carrier-a"}
    assert ra["exacarib.site"] == {"stringValue": "kingston"}


def test_otlp_metrics_payload_and_prometheus_render():
    fam = {
        "name": "exacarib_path_latency_ms",
        "otel": "exacarib.path.latency",
        "help": 'Round-trip "latency".',
        "unit": "ms",
        "type": "gauge",
        "samples": [({"organisation": "Bank", "site": "kingston", "path": "carrier-a"}, 25.5)],
    }
    body = otlp.metrics_payload([fam, {**fam, "name": "x", "samples": []}], at=T.timestamp())
    ms = body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    assert len(ms) == 1 and ms[0]["name"] == "exacarib.path.latency" and ms[0]["unit"] == "ms"
    pt = ms[0]["gauge"]["dataPoints"][0]
    assert pt["asDouble"] == 25.5 and pt["timeUnixNano"] == str(int(T.timestamp() * 1e9))
    assert {a["key"] for a in pt["attributes"]} == {"organisation", "site", "path"}
    text = metrics.render([fam])
    assert '# HELP exacarib_path_latency_ms Round-trip \\"latency\\".' in text
    assert "# TYPE exacarib_path_latency_ms gauge" in text
    assert 'exacarib_path_latency_ms{organisation="Bank",site="kingston",path="carrier-a"} 25.5' in text
    om = metrics.render([fam], openmetrics=True)
    assert om.endswith("# EOF\n") and "# UNIT exacarib_path_latency_ms ms" in om


# ---- SigV4 and the cloud adapters' checks ---------------------------------------------------


def test_sigv4_matches_the_aws_documented_example():
    # AWS's published GET example (IAM ListUsers) from the SigV4 documentation.
    h = clouds.sigv4(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        b"",
        "AKIDEXAMPLE",
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "us-east-1",
        "iam",
        now=dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.UTC),
    )
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_adapter_validation():
    aws = clouds.ADAPTERS["aws_dx"]
    assert aws.validate({"bandwidth_mbps": 50, "aws_account_id": "123456789012", "region": "us-east-1"})
    for bad in ({"bandwidth_mbps": 70, "aws_account_id": "123456789012"}, {"bandwidth_mbps": 50, "aws_account_id": "12"}):
        with pytest.raises(clouds.OnrampError):
            aws.validate(bad)
    gcp = clouds.ADAPTERS["gcp_pi"]
    ok = gcp.validate({"bandwidth_mbps": 100, "pairing_key": "7e51371e-72a3-40b5-b844-2e3efefaee59/us-central1/1"})
    assert ok["region"] == "us-central1"
    with pytest.raises(clouds.OnrampError):
        gcp.validate({"bandwidth_mbps": 100, "pairing_key": "nope"})
    az = clouds.ADAPTERS["azure_er"]
    with pytest.raises(clouds.OnrampError):
        az.validate({"bandwidth_mbps": 50, "circuit_id": "/subscriptions/x", "service_key": "k"})
    mp = clouds.ADAPTERS["megaport"]
    with pytest.raises(clouds.OnrampError):
        mp.validate({"bandwidth_mbps": 50, "b_end_product_uid": "not-a-uid"})


# ---- RESTCONF path selection --------------------------------------------------------------


def test_restconf_select():
    doc = {
        "exacarib-connect:connect": {
            "site": [{"name": "kingston", "link": [{"path": "carrier-a"}], "class": [{"name": "voice"}]}]
        }
    }
    assert restconf.select(doc, "exacarib-connect:connect") == doc
    assert restconf.select(doc, "exacarib-connect:connect/site=kingston") == {
        "exacarib-connect:site": [doc["exacarib-connect:connect"]["site"][0]]
    }
    assert restconf.select(doc, "exacarib-connect:connect/site=kingston/link=carrier-a") == {
        "exacarib-connect:link": [{"path": "carrier-a"}]
    }
    for bad in ("other:connect", "exacarib-connect:connect/site=nowhere", "exacarib-connect:connect/site=kingston/x"):
        with pytest.raises(restconf.NotFound):
            restconf.select(doc, bad)
    assert "module exacarib-connect" in restconf.yang_text()


# ---- the routing engine and planned maintenance ----------------------------------------------


def _windows(now: float, rtt: float = 25.0) -> list[Window]:
    return [Window(now - 10 * (29 - i), 200, 200, rtt, 2.0) for i in range(30)]


def test_engine_moves_off_a_link_under_maintenance_and_says_why():
    now = T.timestamp()
    sla = Sla(latency_ms=150, jitter_ms=30, loss_pct=1)
    reason = "planned maintenance by Carrier A Ltd until 10 Oct 04:00 UTC: Core upgrade"
    ps = {
        "carrier-a": PathInput("carrier-a", "Carrier A", 1, windows=_windows(now), maintenance=reason),
        "carrier-b": PathInput("carrier-b", "Carrier B", 2, windows=_windows(now, 35)),
    }
    evals = {k: evaluate(p, sla, TrendForecaster(), Policy(), now) for k, p in ps.items()}
    assert not evals["carrier-a"].up and evals["carrier-a"].maintenance
    from exaconnect_controller.routing.engine import State

    state = State(path="carrier-a", since=now - 1)  # within the hold time: maintenance still moves it
    new, d = decide("voice", evals, ["carrier-a", "carrier-b"], state, Policy(), now)
    assert new.path == "carrier-b" and d.kind == "move"
    assert d.reason == f"Moved voice from Carrier A to Carrier B ahead of {reason}."
