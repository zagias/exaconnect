"""Integration APIs against Postgres: Prometheus scoped to the key's
organisation, RESTCONF, carrier notices on own links only, planned
maintenance moving traffic ahead of time, TMF621/622/688 and MEF LSO
Sonata round trips, REST hooks, admin status, NetBox, cloud on-ramps and
the publish point fed by the rest of Connect."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from exaconnect_controller import db
from exaconnect_controller.integrations import publish
from exaconnect_controller.routing import runner

from .integrations_helpers import add, deliveries, lab, link, other_org, run_jobs
from .test_flow import _enrol
from .test_steering import _windows

TT = "/api/v1/tmf-api/troubleTicket/v4/troubleTicket"
PO = "/api/v1/tmf-api/productOrderingManagement/v4/productOrder"
EV = "/api/v1/tmf-api/event/v4"
POQ = "/api/v1/mefApi/sonata/productOfferingQualification/v7/productOfferingQualification"
QUOTE = "/api/v1/mefApi/sonata/quoteManagement/v8/quote"
SPO = "/api/v1/mefApi/sonata/productOrderingManagement/v10/productOrder"
STT = "/api/v1/mefApi/sonata/troubleTicket/v4/troubleTicket"


@pytest.fixture
def L(client):  # noqa: N802
    return lab(client)


def _key(client, h, scopes):
    r = client.post("/api/v1/auth/api-keys", json={"name": "scrape", "days": 30, "scopes": scopes}, headers=h)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _org_name(cid):
    with db.tx() as conn:
        return conn.execute("SELECT name FROM customers WHERE id = %s", (cid,)).fetchone()["name"]


def _bfd(client, h, kind, after_s=0):
    at = _in(seconds=after_s)
    r = client.post(
        "/api/v1/agent/telemetry",
        headers=h,
        json={"at": at, "events": [{"at": at, "kind": kind, "detail": {"tunnel": "wg-a"}}]},
    )
    assert r.status_code == 204, r.text


def _in(hours=0.0, minutes=0.0, seconds=0.0):
    t = dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---- Prometheus / OpenMetrics ---------------------------------------------------------------


def test_prometheus_is_scoped_to_the_keys_organisation(client, L):
    other = other_org(client, "Harbour Co")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO sites (customer_id, name, kind, asn, overlay_host) VALUES (%s, 'harbour', 'site', 65300, 250)",
            (other["id"],),
        )
    mine = _org_name(L["customer_id"])
    key = _key(client, L["customer"], ["metrics"])
    r = client.get("/api/v1/metrics", headers=key)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = r.text
    assert f'organisation="{mine}"' in body and "Harbour Co" not in body and "harbour" not in body
    assert 'exacarib_link_commit_mbps{organisation="' in body and 'site="site-a"' in body
    assert "# TYPE exacarib_storm_mode gauge" in body
    # The other organisation sees only itself.
    theirs = client.get("/api/v1/metrics", headers=_key(client, other["h"], ["metrics"])).text
    assert 'site="harbour"' in theirs and mine not in theirs and "site-a" not in theirs
    # A metrics-only key reaches nothing else; a Jibsy-only key can't scrape.
    assert client.get("/api/v1/sites", headers=key).status_code == 403
    assert client.get("/api/v1/metrics", headers=_key(client, L["customer"], ["commai:read"])).status_code == 403
    assert client.get("/api/v1/metrics").status_code == 401
    # OpenMetrics on request.
    om = client.get("/api/v1/metrics", headers={**key, "Accept": "application/openmetrics-text; version=1.0.0"})
    assert om.headers["content-type"].startswith("application/openmetrics-text") and om.text.endswith("# EOF\n")


def test_prometheus_for_a_carrier_shows_only_its_links(client, L):
    body = client.get("/api/v1/metrics", headers=L["carrier_a"]).text
    assert 'carrier="Carrier A"' in body and "Carrier B" not in body
    assert "exacarib_node_up" not in body and "exacarib_sla_score" not in body


def test_prometheus_reports_live_path_measurements(client, L):
    _, a_h = _enrol(client, L["tokens"], "site-a")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    body = {
        "at": now.isoformat(),
        "probes": _windows(now, "wg-a", lambda i: 0.0, 25.0, n=3),
        "tunnels": [{"name": "wg-a", "path": "carrier-a", "handshake_age_s": 3, "bfd": "up"}],
    }
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204
    text = client.get("/api/v1/metrics", headers=L["customer"]).text
    lat = next(x for x in text.splitlines() if x.startswith("exacarib_path_latency_ms{") and 'site="site-a"' in x)
    assert 'path="carrier-a"' in lat and lat.endswith(" 25.0")
    assert any(
        x.startswith("exacarib_path_up{") and 'path="carrier-a"' in x and x.endswith(" 1.0") for x in text.splitlines()
    )
    assert any(x.startswith("exacarib_node_up{") and x.endswith(" 1.0") for x in text.splitlines())


# ---- RESTCONF ------------------------------------------------------------------------------


def test_restconf_reads_the_yang_tree_for_the_organisation(client, L):
    h = L["customer"]
    root = client.get("/api/v1/restconf", headers=h)
    assert root.headers["content-type"].startswith("application/yang-data+json")
    assert "ietf-restconf:restconf" in root.json()
    meta = client.get("/.well-known/host-meta")
    assert meta.status_code == 200 and '<Link rel="restconf" href="/api/v1/restconf"/>' in meta.text
    lib = client.get("/api/v1/restconf/data/ietf-yang-library:yang-library", headers=h).json()
    mods = lib["ietf-yang-library:yang-library"]["module-set"][0]["module"]
    assert any(m["name"] == "exacarib-connect" and m["revision"] == "2026-10-07" for m in mods)
    assert "module exacarib-connect" in client.get("/api/v1/restconf/modules/exacarib-connect.yang", headers=h).text

    doc = client.get("/api/v1/restconf/data/exacarib-connect:connect", headers=h).json()
    sites = {s["name"]: s for s in doc["exacarib-connect:connect"]["site"]}
    assert {"site-a", "site-b"} <= set(sites)
    a = sites["site-a"]
    assert {lk["path"] for lk in a["link"]} >= {"carrier-a", "carrier-b"}
    assert {c["name"] for c in a["class"]} == {"voice", "business", "bulk"}
    one = client.get("/api/v1/restconf/data/exacarib-connect:connect/site=site-a/link=carrier-a", headers=h).json()
    lk = one["exacarib-connect:link"][0]
    assert lk["carrier"] == "Carrier A" and lk["underlay-type"] in ("fibre", "broadband")

    missing = client.get("/api/v1/restconf/data/exacarib-connect:connect/site=nowhere", headers=h)
    assert missing.status_code == 404
    assert missing.json()["ietf-restconf:errors"]["error"][0]["error-tag"] == "invalid-value"
    put = client.put("/api/v1/restconf/data/exacarib-connect:connect", headers=h, json={})
    assert put.status_code == 405 and put.json()["ietf-restconf:errors"]["error"][0]["error-tag"] == "access-denied"
    xml = client.get(
        "/api/v1/restconf/data/exacarib-connect:connect", headers={**h, "Accept": "application/yang-data+xml"}
    )
    assert xml.status_code == 406
    # Another organisation sees none of it, and a carrier is not served.
    other = other_org(client)
    theirs = client.get("/api/v1/restconf/data/exacarib-connect:connect", headers=other["h"]).json()
    assert theirs["exacarib-connect:connect"].get("site", []) == []
    assert client.get("/api/v1/restconf/data/exacarib-connect:connect", headers=L["carrier_a"]).status_code == 403


# ---- carrier notices -------------------------------------------------------------------------


def _maintenance(client, L, links, starts=None, ends=None, h=None, **kw):
    return client.post(
        "/api/v1/carrier/notices",
        json={
            "kind": "maintenance",
            "title": "Core router upgrade",
            "link_ids": links,
            "starts_at": starts or _in(hours=24),
            "ends_at": ends or _in(hours=26),
            **kw,
        },
        headers=h or L["carrier_a"],
    )


def test_carriers_post_notices_about_their_own_links_only(client, L):
    a_links = [lk["id"] for lk in L["links"] if lk["carrier"] == "Carrier A"]
    b_link = next(lk["id"] for lk in L["links"] if lk["carrier"] == "Carrier B")
    r = _maintenance(client, L, a_links, external_id="CHG-1001")
    assert r.status_code == 201, r.text
    n = r.json()
    assert n["kind"] == "maintenance" and n["status"] == "scheduled" and set(n["link_ids"]) == set(a_links)
    # Someone else's link, a link that does not exist, a duplicate reference, a bad window.
    assert _maintenance(client, L, [a_links[0], b_link]).status_code == 403
    assert _maintenance(client, L, ["00000000-0000-0000-0000-000000000000"]).status_code == 422
    assert _maintenance(client, L, ["not-a-uuid"]).status_code == 422
    assert _maintenance(client, L, a_links, external_id="CHG-1001").status_code == 409
    assert _maintenance(client, L, a_links, starts=_in(hours=3), ends=_in(hours=2)).status_code == 400
    assert _maintenance(client, L, a_links, starts=_in(hours=1), ends=_in(hours=200)).status_code == 400
    # Customers and other carriers can't post.
    assert _maintenance(client, L, a_links, h=L["customer"]).status_code == 403

    # Carrier B sees none of Carrier A's notices.
    assert client.get("/api/v1/carrier/notices", headers=L["carrier_b"]).json() == []
    assert client.get(f"/api/v1/carrier/notices/{n['id']}", headers=L["carrier_b"]).status_code == 404
    assert (
        client.patch(
            f"/api/v1/carrier/notices/{n['id']}", json={"status": "cancelled"}, headers=L["carrier_b"]
        ).status_code
        == 404
    )
    # The customer sees it, with only its own links; another organisation doesn't.
    mine = client.get("/api/v1/notices", headers=L["customer"]).json()
    assert [x["id"] for x in mine] == [n["id"]]
    assert client.get(f"/api/v1/notices/{n['id']}", headers=L["customer"]).json()["carrier"] == "Carrier A"
    other = other_org(client)
    assert client.get("/api/v1/notices", headers=other["h"]).json() == []
    assert client.get(f"/api/v1/notices/{n['id']}", headers=other["h"]).status_code == 404
    # One event per affected site on the timeline.
    with db.tx() as conn:
        evs = conn.execute("SELECT detail FROM events WHERE kind = 'carrier_notice'").fetchall()
    sites = {e["detail"]["site"] for e in evs}
    assert sites == {lk["site"] for lk in L["links"] if lk["carrier"] == "Carrier A"}
    for e in evs:
        assert all(x["id"] in a_links for x in e["detail"]["links"])
    # The carrier updates it.
    r = client.patch(f"/api/v1/carrier/notices/{n['id']}", json={"status": "cancelled"}, headers=L["carrier_a"])
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["resolved_at"]


def test_a_notice_reaches_subscribers_and_the_carrier_hears_back(client, L, vault):
    a = link(L, "site-a", "carrier-a")
    cust = add(
        client, L["customer"], "webhook", secrets={"url": "https://hooks.example.org/x"}, event_types=["carrier.*"]
    )
    car = add(client, L["carrier_a"], "webhook", secrets={"url": "https://noc.carrier-a.example/x"}, event_types=["*"])
    carb = add(client, L["carrier_b"], "webhook", secrets={"url": "https://noc.carrier-b.example/x"})
    r = client.post(
        "/api/v1/carrier/notices",
        json={"kind": "fault", "title": "Fibre cut", "link_ids": [a]},
        headers=L["carrier_a"],
    )
    assert r.status_code == 201
    run_jobs()
    [d] = deliveries(cust["id"])
    assert d["event_type"] == "carrier.fault" and d["status"] == "simulated"
    [cd] = deliveries(car["id"])
    assert cd["event_type"] == "carrier.fault"
    assert deliveries(carb["id"]) == []
    # The carrier's copy names its link, not the customer's organisation.
    sent = json.loads(cd["detail"]["requests"][0]["body"])
    assert "organisationid" not in sent and sent["data"]["links"][0]["id"] == a
    assert sent["source"].endswith("/carriers")
    # Path events go to the carrier for its own links too.
    _, a_h = _enrol(client, L["tokens"], "site-a")
    _bfd(client, a_h, "bfd_down")
    run_jobs()
    kinds = [x["event_type"] for x in deliveries(car["id"])]
    assert "path.down" in kinds and all(x["event_type"] != "path.down" for x in deliveries(carb["id"]))


def test_planned_maintenance_moves_traffic_before_the_window(client, L):
    tokens = L["tokens"]
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    body = {
        "at": now.isoformat(),
        "probes": _windows(now, "wg-a", lambda i: 0.0, 25.0) + _windows(now, "wg-b", lambda i: 0.0, 35.0),
        "tunnels": [
            {"name": "wg-a", "path": "carrier-a", "handshake_age_s": 3, "bfd": "up"},
            {"name": "wg-b", "path": "carrier-b", "handshake_age_s": 3, "bfd": "up"},
        ],
        "steering": [{"class": "voice", "path": "carrier-a"}],
        "steering_version": m["version"],
    }
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204
    runner.run_once(now)
    assert runner.run_once(now + dt.timedelta(seconds=1)) == []  # healthy: nothing moves

    a = link(L, "site-a", "carrier-a")
    starts = now + dt.timedelta(seconds=70)
    r = _maintenance(client, L, [a], starts=starts.isoformat(), ends=(starts + dt.timedelta(hours=2)).isoformat())
    assert r.status_code == 201
    # More than a minute out: still nothing.
    assert runner.run_once(now + dt.timedelta(seconds=2)) == []
    # Within a minute of the start: classes leave carrier A, with the reason.
    t = starts - dt.timedelta(seconds=55)
    made = runner.run_once(t)
    voice = next(d for d in made if d["class"] == "voice")
    assert voice["site"] == "site-a" and voice["kind"] == "move" and voice["to_path"] == "carrier-b"
    assert "ahead of planned maintenance by Carrier A" in voice["reason"] and "Core router upgrade" in voice["reason"]
    # Nothing at site-b moved: its link is not in the notice.
    assert all(d["site"] == "site-a" for d in made)
    # The window opening is announced.
    with db.tx() as conn:
        assert publish.tick(conn, t)["started"] == 1
        assert conn.execute("SELECT status FROM connect_notices").fetchone()["status"] == "in_progress"
        assert conn.execute("SELECT count(*) AS n FROM events WHERE kind = 'maintenance_start'").fetchone()["n"] == 1
        # And closing at the end.
        assert publish.tick(conn, starts + dt.timedelta(hours=2, seconds=1))["ended"] == 1
        assert conn.execute("SELECT status FROM connect_notices").fetchone()["status"] == "resolved"


def test_a_cancelled_window_moves_nothing(client, L):
    a = link(L, "site-a", "carrier-a")
    r = _maintenance(client, L, [a], starts=_in(seconds=10), ends=_in(hours=1), move_traffic=False)
    assert r.status_code == 201
    with db.tx() as conn:
        from exaconnect_controller.integrations import notices

        assert notices.under_maintenance(conn, [a], dt.datetime.now(dt.UTC)) == {}
    client.patch(f"/api/v1/carrier/notices/{r.json()['id']}", json={"move_traffic": True}, headers=L["carrier_a"])
    with db.tx() as conn:
        assert a in notices.under_maintenance(conn, [a], dt.datetime.now(dt.UTC))
    client.patch(f"/api/v1/carrier/notices/{r.json()['id']}", json={"status": "cancelled"}, headers=L["carrier_a"])
    with db.tx() as conn:
        assert notices.under_maintenance(conn, [a], dt.datetime.now(dt.UTC)) == {}


# ---- TMF621 Trouble Ticket --------------------------------------------------------------------


def test_tmf621_round_trip_for_a_carrier(client, L):
    a = link(L, "site-a", "carrier-a")
    b = link(L, "site-a", "carrier-b")
    start, end = _in(hours=48), _in(hours=50)
    ticket = {
        "name": "Planned fibre work, Spanish Town",
        "description": "Splicing after road works.",
        "severity": "Minor",
        "ticketType": "Maintenance",
        "@type": "MaintenanceTroubleTicket",
        "plannedStartDate": start,
        "plannedEndDate": end,
        "externalIdentifier": [{"id": "CHG-20431", "owner": "carrier"}],
        "relatedEntity": [{"id": a, "role": "affectedLink", "@referredType": "Link"}],
    }
    r = client.post(TT, json=ticket, headers=L["carrier_a"])
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["@type"] == "MaintenanceTroubleTicket" and t["@baseType"] == "TroubleTicket"
    assert t["ticketType"] == "Maintenance" and t["status"] == "pending" and t["severity"] == "Minor"
    assert t["plannedStartDate"] == start and t["plannedEndDate"] == end
    assert t["externalIdentifier"] == [{"id": "CHG-20431", "owner": "carrier"}]
    assert t["relatedEntity"][0]["id"] == a and t["href"] == f"{TT}/{t['id']}"
    assert client.get(f"{TT}/{t['id']}", headers=L["carrier_a"]).json() == t
    # The same in the native API.
    n = client.get(f"/api/v1/carrier/notices/{t['id']}", headers=L["carrier_a"]).json()
    assert n["title"] == ticket["name"] and n["source"] == "tmf621" and n["external_id"] == "CHG-20431"
    # Status changes follow TMF621's names.
    r = client.patch(
        f"{TT}/{t['id']}", json={"status": "inProgress", "description": "Started early"}, headers=L["carrier_a"]
    )
    assert r.status_code == 200 and r.json()["status"] == "inProgress" and r.json()["description"] == "Started early"
    r = client.patch(f"{TT}/{t['id']}", json={"status": "resolved"}, headers=L["carrier_a"])
    assert r.json()["status"] == "resolved" and r.json()["resolutionDate"]
    assert client.patch(f"{TT}/{t['id']}", json={"status": "sideways"}, headers=L["carrier_a"]).status_code == 422
    # Own links only, and only the owner sees it.
    bad = {**ticket, "externalIdentifier": [], "relatedEntity": [{"id": b, "@referredType": "Link"}]}
    assert client.post(TT, json=bad, headers=L["carrier_a"]).status_code == 403
    assert client.get(f"{TT}/{t['id']}", headers=L["carrier_b"]).status_code == 404
    assert client.get(TT, headers=L["carrier_b"]).json() == []
    # A fault ticket.
    fault = {
        "name": "Fibre cut",
        "severity": "Critical",
        "ticketType": "Fault",
        "relatedEntity": [{"id": a, "@referredType": "Link"}],
    }
    f = client.post(TT, json=fault, headers=L["carrier_a"]).json()
    assert f["ticketType"] == "Fault" and f["status"] == "acknowledged" and f["severity"] == "Critical"


def test_tmf621_for_a_customer(client, L):
    a = link(L, "site-a", "carrier-a")
    carrier_ticket = client.post(
        TT,
        json={"name": "Fault", "ticketType": "Fault", "relatedEntity": [{"id": a, "@referredType": "Link"}]},
        headers=L["carrier_a"],
    ).json()
    mine = client.post(
        TT,
        json={"name": "Voice quality poor at Kingston", "severity": "Major", "relatedEntity": [{"id": a}]},
        headers=L["customer"],
    )
    assert mine.status_code == 201
    m = mine.json()
    assert m["ticketType"] == "Incident" and m["@type"] == "TroubleTicket" and m["severity"] == "Critical"
    # The customer sees its own ticket and the carrier's ticket on its link.
    ids = {t["id"] for t in client.get(TT, headers=L["customer"]).json()}
    assert ids == {m["id"], carrier_ticket["id"]}
    # But changes only its own, and only to close it.
    assert (
        client.patch(f"{TT}/{carrier_ticket['id']}", json={"status": "closed"}, headers=L["customer"]).status_code
        == 403
    )
    assert client.patch(f"{TT}/{m['id']}", json={"status": "inProgress"}, headers=L["customer"]).status_code == 403
    assert (
        client.patch(f"{TT}/{m['id']}", json={"status": "closed"}, headers=L["customer"]).json()["status"] == "closed"
    )
    # Never about someone else's link; carriers don't see customers' tickets.
    other = other_org(client)
    assert client.post(TT, json={"name": "x", "relatedEntity": [{"id": a}]}, headers=other["h"]).status_code == 403
    assert client.get(f"{TT}/{m['id']}", headers=other["h"]).status_code == 404
    assert client.get(f"{TT}/{m['id']}", headers=L["carrier_a"]).status_code == 404
    assert client.post(TT, json={"relatedEntity": []}, headers=L["customer"]).status_code == 422


# ---- TMF688 Event ------------------------------------------------------------------------


def test_tmf688_inbound_events_from_a_carrier(client, L):
    a = link(L, "site-b", "carrier-a")
    create = {
        "eventId": "c-77812",
        "eventTime": _in(),
        "eventType": "TroubleTicketCreateEvent",
        "event": {
            "troubleTicket": {
                "id": "INC-77812",
                "name": "Fibre cut near Spanish Town",
                "severity": "Critical",
                "ticketType": "Fault",
                "relatedEntity": [{"id": a, "role": "affectedLink", "@referredType": "Link"}],
            }
        },
    }
    r = client.post(f"{EV}/event", json=create, headers=L["carrier_a"])
    assert r.status_code == 201, r.text
    nid = r.json()["notice"]
    assert r.json()["status"] == "open"
    n = client.get(f"/api/v1/carrier/notices/{nid}", headers=L["carrier_a"]).json()
    assert n["source"] == "tmf688" and n["external_id"] == "INC-77812" and n["kind"] == "fault"
    # A status change, then resolved, by the same reference.
    change = {
        **create,
        "eventType": "TroubleTicketStatusChangeEvent",
        "event": {"troubleTicket": {"id": "INC-77812", "status": "inProgress"}},
    }
    assert client.post(f"{EV}/event", json=change, headers=L["carrier_a"]).json()["status"] == "in_progress"
    done = {**create, "eventType": "TroubleTicketResolvedEvent", "event": {"troubleTicket": {"id": "INC-77812"}}}
    assert client.post(f"{EV}/event", json=done, headers=L["carrier_a"]).json()["status"] == "resolved"
    # Unknown references, other carriers' links, and non-carriers are refused.
    nope = {**change, "event": {"troubleTicket": {"id": "INC-0", "status": "resolved"}}}
    assert client.post(f"{EV}/event", json=nope, headers=L["carrier_a"]).status_code == 404
    assert client.post(f"{EV}/event", json=create, headers=L["carrier_b"]).status_code == 403
    assert client.post(f"{EV}/event", json=create, headers=L["customer"]).status_code == 403
    assert client.post(f"{EV}/event", json={"eventType": "x"}, headers=L["carrier_a"]).status_code == 422


def test_tmf688_hub_listener(client, L, live, fake):
    r = client.post(
        f"{EV}/hub",
        json={"callback": fake.url + "/listener", "query": "eventType=path.down,storm.on"},
        headers=L["customer"],
    )
    assert r.status_code == 201
    hub = r.json()
    assert hub["mode"] == "live"
    got = client.get(f"{EV}/hub/{hub['id']}", headers=L["customer"]).json()
    assert got["callback"] == "127.0.0.1" and got["query"] == "eventType=path.down,storm.on"  # never the full callback
    with db.tx() as conn:
        publish.emit(
            conn, "path.down", customer_id=L["customer_id"], data={"site": "site-a"}, event_id="h1", dedup_key="k"
        )
        publish.emit(conn, "node.offline", customer_id=L["customer_id"], data={}, event_id="h2")
    run_jobs()
    assert [r["json"]["eventId"] for r in fake.requests] == ["h1"]
    ev = fake.requests[0]["json"]
    assert ev["@type"] == "Event" and ev["eventType"] == "ExaCaribConnectPathDown" and ev["event"]["site"] == "site-a"
    # Event list for the organisation, as TMF Event resources.
    listed = client.get(f"{EV}/event", headers=L["customer"]).json()
    assert {e["eventId"] for e in listed} >= {"h1", "h2"}
    other = other_org(client)
    assert client.get(f"{EV}/event", headers=other["h"]).json() == []
    assert client.get(f"{EV}/hub/{hub['id']}", headers=other["h"]).status_code == 404
    assert client.delete(f"{EV}/hub/{hub['id']}", headers=L["customer"]).status_code == 204
    assert client.get(f"{EV}/hub/{hub['id']}", headers=L["customer"]).status_code == 404
    assert client.post(f"{EV}/hub", json={}, headers=L["customer"]).status_code == 400


# ---- TMF622 Product Ordering ------------------------------------------------------------------


def _breakout(site="site-a", mode="local"):
    return {
        "description": "Local breakout for site-a",
        "productOrderItem": [
            {
                "id": "1",
                "action": "add",
                "productOffering": {"id": "internet-breakout"},
                "product": {
                    "productCharacteristic": [{"name": "site", "value": site}, {"name": "mode", "value": mode}]
                },
            }
        ],
    }


def test_tmf622_round_trip(client, L):
    h = L["customer"]
    r = client.post(PO, json=_breakout(), headers=h)
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["@type"] == "ProductOrder" and o["state"] == "completed" and o["completionDate"]
    assert o["productOrderItem"][0]["state"] == "completed" and o["href"] == f"{PO}/{o['id']}"
    assert client.get(f"{PO}/{o['id']}", headers=h).json()["state"] == "completed"
    assert [x["id"] for x in client.get(PO, headers=h).json()] == [o["id"]]
    with db.tx() as conn:
        assert (
            conn.execute("SELECT internet_mode FROM sites WHERE name = 'site-a'").fetchone()["internet_mode"] == "local"
        )
    # A problem gives a rejected order, kept for the record.
    bad = client.post(PO, json=_breakout(site="nowhere"), headers=h)
    assert bad.status_code == 201 and bad.json()["state"] == "rejected" and bad.json()["note"][0]["text"]
    unknown = {"productOrderItem": [{"productOffering": {"id": "teleporter"}, "product": {}}]}
    assert client.post(PO, json=unknown, headers=h).status_code == 422
    assert client.post(PO, json={"productOrderItem": []}, headers=h).status_code == 422
    # Another organisation can't see or order for it; an admin names the buyer.
    other = other_org(client)
    assert client.get(f"{PO}/{o['id']}", headers=other["h"]).status_code == 404
    assert client.post(f"{PO}?buyerId={L['customer_id']}", json=_breakout(), headers=other["h"]).status_code == 403
    assert client.post(PO, json=_breakout(), headers=L["carrier_a"]).status_code == 403


def test_tmf622_keeps_no_pre_shared_key(client, L, admin_headers):
    psk = "Very.Secret_PreShared_Key123"
    body = {
        "productOrderItem": [
            {
                "id": "1",
                "productOffering": {"id": "cloud-circuit"},
                "product": {
                    "productCharacteristic": [
                        {"name": "provider", "value": "aws"},
                        {"name": "region", "value": "us-east-1"},
                        {"name": "site", "value": "site-a"},
                        {"name": "bandwidthMbps", "value": 50},
                        {"name": "cloudPrefixes", "value": ["10.100.0.0/16"]},
                        {"name": "peerAddress", "value": "100.64.10.2"},
                        {"name": "psk", "value": psk},
                    ]
                },
            }
        ]
    }
    r = client.post(f"{PO}?buyerId={L['customer_id']}", json=body, headers=admin_headers)
    assert r.status_code == 201 and r.json()["state"] == "completed", r.text
    assert psk not in json.dumps(r.json())
    with db.tx() as conn:
        assert psk not in json.dumps(conn.execute("SELECT body FROM connect_std_documents").fetchall())
    assert client.post(PO, json=body, headers=admin_headers).status_code == 400  # admin must name the buyer


# ---- MEF LSO Sonata ---------------------------------------------------------------------------


def _onramp_item(mbps=50, item_id="1"):
    return {
        "id": item_id,
        "action": "add",
        "product": {
            "productOffering": {"id": "cloud-onramp"},
            "productConfiguration": {
                "@type": "urn:exacarib:connect:cloud-onramp:v1",
                "provider": "aws_dx",
                "site": "site-a",
                "bandwidthMbps": mbps,
                "awsAccountId": "123456789012",
                "region": "us-east-1",
            },
        },
    }


def test_sonata_poq_quote_and_order_round_trip(client, L):
    h = L["customer"]
    q = client.post(
        POQ,
        json={
            "instantSyncQualification": True,
            "productOfferingQualificationItem": [_onramp_item(), _onramp_item(70, "2")],
        },
        headers=h,
    )
    assert q.status_code == 201
    poq = q.json()
    assert poq["state"] == "done.ready"
    ok, bad = poq["productOfferingQualificationItem"]
    assert ok["qualificationResult"] == "qualified" and ok["terminationError"] == []
    assert bad["qualificationResult"] == "unqualified" and "Mbps" in bad["terminationError"][0]["value"]
    assert client.get(f"{POQ}/{poq['id']}", headers=h).json() == poq

    quote = client.post(QUOTE, json={"instantSyncQuote": True, "quoteItem": [_onramp_item()]}, headers=h).json()
    assert quote["state"] == "approved.orderable"
    price = quote["quoteItem"][0]["quoteItemPrice"][0]
    assert price["priceType"] == "recurring" and price["recurringChargePeriod"] == "month"
    assert price["price"]["dutyFreeAmount"] == {"unit": "USD", "value": 150.0}
    assert client.get(f"{QUOTE}/{quote['id']}", headers=h).json()["id"] == quote["id"]
    worse = client.post(QUOTE, json={"quoteItem": [_onramp_item(70)]}, headers=h).json()
    assert worse["state"] == "unableToProvide" and worse["quoteItem"][0]["quoteItemPrice"] == []

    # Order by referring to the quote item.
    r = client.post(
        SPO,
        json={
            "productOrderItem": [
                {"id": "1", "action": "add", "quoteItem": {"quoteId": quote["id"], "quoteItemId": "1"}}
            ]
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["state"] == "completed" and order["productOrderItem"][0]["state"] == "completed"
    assert client.get(f"{SPO}/{order['id']}", headers=h).json()["state"] == "completed"
    assert [x["id"] for x in client.get(SPO, headers=h).json()] == [order["id"]]
    (ramp,) = client.get(f"/api/v1/customers/{L['customer_id']}/onramps", headers=h).json()
    assert ramp["provider"] == "aws_dx" and ramp["bandwidth_mbps"] == 50 and ramp["simulated"] is True
    # An unorderable quote item can't be ordered.
    r = client.post(
        SPO, json={"productOrderItem": [{"quoteItem": {"quoteId": worse["id"], "quoteItemId": "1"}}]}, headers=h
    )
    assert r.status_code == 422
    # Nobody else sees any of it.
    other = other_org(client)
    for path in (f"{POQ}/{poq['id']}", f"{QUOTE}/{quote['id']}", f"{SPO}/{order['id']}"):
        assert client.get(path, headers=other["h"]).status_code == 404
    assert (
        client.post(
            SPO,
            json={"productOrderItem": [{"quoteItem": {"quoteId": quote["id"], "quoteItemId": "1"}}]},
            headers=other["h"],
        ).status_code
        == 404
    )


def test_sonata_trouble_ticket(client, L):
    h = L["customer"]
    r = client.post(
        STT,
        json={
            "description": "Voice quality poor at Kingston",
            "severity": "extensive",
            "relatedEntity": [{"id": link(L, "site-a", "carrier-a")}],
        },
        headers=h,
    )
    assert r.status_code == 201
    t = r.json()
    assert t["severity"] == "extensive" and t["priority"] == "high" and t["issueType"] == "unavailable"
    assert t["status"] == "acknowledged" and t["href"].endswith(f"/troubleTicket/{t['id']}")
    assert client.get(f"{STT}/{t['id']}", headers=h).json()["id"] == t["id"]
    assert client.post(STT, json={"description": "x"}, headers=L["carrier_a"]).status_code == 403
    assert client.get(f"{STT}/{t['id']}", headers=other_org(client)["h"]).status_code == 404


# ---- REST hooks (Zapier, Make, n8n) ----------------------------------------------------------


def test_rest_hooks_subscribe_and_unsubscribe(client, L, vault):
    h = L["customer"]
    me = client.get("/api/v1/hooks/me", headers=h).json()
    assert me["role"] == "customer" and me["organisation"] == _org_name(L["customer_id"])
    sample = client.get("/api/v1/hooks/sample?event=sla.breach_forecast", headers=h).json()
    assert sample[0]["type"] == "com.exacarib.connect.sla.breach_forecast" and sample[0]["data"]["example"] is True
    assert client.get("/api/v1/hooks/sample?event=nope", headers=h).status_code == 404
    r = client.post(
        "/api/v1/hooks",
        json={"hookUrl": "https://hooks.zapier.com/hooks/standard/1/abc/", "event": "path.down", "platform": "zapier"},
        headers=h,
    )
    assert r.status_code == 201
    hook = r.json()
    assert (
        hook["events"] == ["path.down"] and hook["signing_secret"].startswith("whsec_") and hook["mode"] == "simulated"
    )
    listed = client.get(f"/api/v1/integrations/{hook['id']}", headers=h).json()
    assert listed["origin"] == "resthook" and listed["platform"] == "zapier" and listed["name"] == "Zapier hook"
    with db.tx() as conn:
        publish.emit(conn, "path.down", customer_id=L["customer_id"], data={}, event_id="z1")
    run_jobs()
    assert [d["status"] for d in deliveries(hook["id"])] == ["simulated"]
    assert client.post("/api/v1/hooks", json={"event": "path.down"}, headers=h).status_code == 400
    assert (
        client.post("/api/v1/hooks", json={"target_url": "https://x.example/", "event": "nope"}, headers=h).status_code
        == 400
    )
    # Only REST hooks are removed through /hooks, and only by their owner.
    plain = add(client, h, "webhook", secrets={"url": "https://x.example/"})
    assert client.delete(f"/api/v1/hooks/{plain['id']}", headers=h).status_code == 404
    assert client.delete(f"/api/v1/hooks/{hook['id']}", headers=other_org(client)["h"]).status_code == 404
    assert client.delete(f"/api/v1/hooks/{hook['id']}", headers=h).status_code == 204
    assert client.get(f"/api/v1/integrations/{hook['id']}", headers=h).status_code == 404


# ---- integrations CRUD, secrets and the catalogue ----------------------------------------------


def test_integration_crud_never_returns_secrets(client, L, vault):
    h = L["customer"]
    cat = client.get("/api/v1/integrations/catalogue", headers=h).json()
    keys = {p["key"] for p in cat["providers"]}
    assert {
        "webhook",
        "slack",
        "teams",
        "pagerduty",
        "opsgenie",
        "servicenow",
        "jira",
        "datadog",
        "splunk",
        "elastic",
        "sentinel",
        "otlp",
        "grafana_cloud",
        "syslog",
        "snmp",
        "netbox",
        "tmf688",
    } <= keys
    assert {a["key"] for a in cat["onramp_adapters"]} == {"aws_dx", "azure_er", "gcp_pi", "megaport"}
    assert any(e["name"] == "path.down" for e in cat["events"])
    asyncapi = client.get("/api/v1/integrations/asyncapi.json").json()
    assert asyncapi["asyncapi"] == "3.0.0"

    pd = add(client, h, "pagerduty", secrets={"routing_key": "SECRET-ROUTING-KEY"}, event_types=["path.*"])
    assert pd["secrets_set"] == ["routing_key"] and "SECRET-ROUTING-KEY" not in json.dumps(pd)
    r = client.patch(
        f"/api/v1/integrations/{pd['id']}",
        json={"name": "NOC", "min_severity": "critical", "secrets": {"routing_key": "NEW-KEY"}},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["name"] == "NOC" and "NEW-KEY" not in r.text
    assert (
        client.patch(f"/api/v1/integrations/{pd['id']}", json={"event_types": ["bogus"]}, headers=h).status_code == 400
    )
    with db.tx() as conn:
        row = conn.execute("SELECT secret_ciphertext FROM connect_integrations WHERE id = %s", (pd["id"],)).fetchone()
        audit = conn.execute("SELECT detail::text AS d FROM audit_log").fetchall()
    assert "NEW-KEY" not in row["secret_ciphertext"] and all("NEW-KEY" not in a["d"] for a in audit)
    # Validation.
    assert client.post("/api/v1/integrations", json={"provider": "nope", "name": "x"}, headers=h).status_code == 400
    # Without its credentials a connector is kept, simulated, until they're added.
    bare = client.post("/api/v1/integrations", json={"provider": "pagerduty", "name": "x"}, headers=h).json()
    assert bare["mode"] == "simulated" and bare["secrets_set"] == []
    assert (
        client.post(
            "/api/v1/integrations", json={"provider": "webhook", "name": "x", "secrets": {"url": "ftp://x"}}, headers=h
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/integrations", json={"provider": "syslog", "name": "x", "config": {"host": "a b"}}, headers=h
        ).status_code
        == 400
    )
    # Carriers may only add carrier-capable connectors.
    assert (
        client.post(
            "/api/v1/integrations",
            json={"provider": "syslog", "name": "x", "config": {"host": "h"}},
            headers=L["carrier_a"],
        ).status_code
        == 400
    )
    # Delete.
    assert client.delete(f"/api/v1/integrations/{pd['id']}", headers=h).status_code == 204
    assert client.get(f"/api/v1/integrations/{pd['id']}", headers=h).status_code == 404


def test_secrets_need_secure_storage(client, L, monkeypatch):
    monkeypatch.delenv("EXA_SECRETS_KEY", raising=False)
    r = client.post(
        "/api/v1/integrations",
        json={"provider": "pagerduty", "name": "x", "secrets": {"routing_key": "k"}},
        headers=L["customer"],
    )
    assert r.status_code == 400 and "EXA_SECRETS_KEY" in r.text


def test_admin_status_shows_configured_never_values(client, L, admin_headers, vault, monkeypatch):
    monkeypatch.setenv("EXA_AWS_DX_ACCESS_KEY_ID", "AKIAEXAMPLESECRETID")
    monkeypatch.setenv("EXA_AWS_DX_SECRET_ACCESS_KEY", "very/secret/value")
    monkeypatch.setenv("EXA_AWS_DX_INTERCONNECT_ID", "dxcon-abc")
    add(client, L["customer"], "slack", secrets={"webhook_url": "https://hooks.slack.com/services/T/B/SECRETPART"})
    _maintenance(client, L, [link(L, "site-a", "carrier-a")])
    r = client.get("/api/v1/admin/integrations/status", headers=admin_headers)
    assert r.status_code == 200
    s = r.json()
    text = r.text
    for secret in ("AKIAEXAMPLESECRETID", "very/secret/value", "dxcon-abc", "SECRETPART", "hooks.slack.com"):
        assert secret not in text
    aws = next(a for a in s["onramp_adapters"] if a["key"] == "aws_dx")
    assert aws["configured"] is True and aws["mode"] == "simulated"  # configured, but the live switch is off
    assert next(a for a in s["onramp_adapters"] if a["key"] == "megaport")["configured"] is False
    slack = next(p for p in s["providers"] if p["key"] == "slack")
    assert slack["in_use"] == 1 and slack["live"] == 0
    assert s["secure_storage"] is True and s["live"] is False
    assert {p["name"]: p["configured"] for p in s["platform"]}["EXA_SECRETS_KEY"] is True
    feeds = {f["name"]: f for f in s["carrier_feeds"]}
    assert feeds["Carrier A"]["notices"] == 1 and feeds["Carrier A"]["accounts"] == 1
    assert client.get("/api/v1/admin/integrations/status", headers=L["customer"]).status_code == 403


# ---- NetBox -------------------------------------------------------------------------------


def _netbox_fake(fake, existing_sites=()):
    made: dict[str, list[dict]] = {}

    def handler(req):
        path = req["path"].split("?")[0]
        if not path.startswith("/api/"):
            return None
        kind = path[len("/api/") :].strip("/")
        if req["method"] == "GET":
            if kind == "dcim/sites":
                q = req["path"].split("slug=")[-1].split("&")[0] if "slug=" in req["path"] else None
                rows = [s for s in existing_sites if q is None or s["slug"] == q]
                return 200, {"count": len(rows), "next": None, "results": rows}
            if kind == "ipam/prefixes" and "site_id=" in req["path"]:
                rows = [{"id": 99, "prefix": "192.168.99.0/24", "status": {"value": "active"}}]
                return 200, {"count": 1, "next": None, "results": rows}
            return 200, {"count": 0, "next": None, "results": []}
        if req["method"] in ("POST", "PATCH"):
            made.setdefault(kind.split("/")[0] + "/" + kind.split("/")[1], []).append(req["json"])
            return 201, {"id": len(made), **(req["json"] or {})}
        return None

    fake.handler = handler
    return made


def test_netbox_push(client, L, live, fake):
    made = _netbox_fake(fake)
    nb = add(
        client,
        L["customer"],
        "netbox",
        config={"url": fake.url, "direction": "push"},
        secrets={"token": "nb-token-123"},
    )
    assert nb["event_types"] == []  # takes no events
    r = client.post(f"/api/v1/integrations/{nb['id']}/sync", headers=L["customer"])
    assert r.status_code == 200, r.text
    assert all(x["headers"]["authorization"] == "Token nb-token-123" for x in fake.requests)
    sites = {s["slug"]: s for s in made["dcim/sites"]}
    assert {"site-a", "site-b"} <= set(sites)
    types = made["circuits/circuit-types"]
    assert types[0]["slug"] == "exacarib-underlay"
    providers = {p["name"] for p in made["circuits/providers"]}
    assert {"Carrier A", "Carrier B"} <= providers
    cids = {c["cid"]: c for c in made["circuits/circuits"]}
    assert "site-a-carrier-a" in cids and cids["site-a-carrier-a"]["commit_rate"] > 0
    prefixes = {p["prefix"]: p for p in made["ipam/prefixes"]}
    assert prefixes["192.168.10.0/24"]["scope_type"] == "dcim.site"
    assert client.post(f"/api/v1/integrations/{nb['id']}/test", headers=L["customer"]).status_code == 400
    assert client.post(f"/api/v1/integrations/{nb['id']}/sync", headers=other_org(client)["h"]).status_code == 404


def test_netbox_pull(client, L, live, fake):
    _netbox_fake(
        fake,
        [
            {
                "id": 7,
                "slug": "site-a",
                "name": "site-a",
                "description": "Kingston, Jamaica",
                "latitude": 17.99,
                "longitude": -76.79,
            }
        ],
    )
    nb = add(client, L["customer"], "netbox", config={"url": fake.url, "direction": "pull"}, secrets={"token": "t"})
    r = client.post(f"/api/v1/integrations/{nb['id']}/sync", headers=L["customer"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert "site-b" in json.dumps(out)  # reported as not found in NetBox
    with db.tx() as conn:
        s = conn.execute("SELECT location, lan_prefixes::text[] AS p FROM sites WHERE name = 'site-a'").fetchone()
    assert s["location"] == "Kingston, Jamaica" and s["p"] == ["192.168.99.0/24"]
    assert all(x["method"] == "GET" for x in fake.requests)  # pull never writes to NetBox


def test_netbox_simulated(client, L, vault):
    nb = add(client, L["customer"], "netbox", config={"url": "https://netbox.example.org"}, secrets={"token": "t"})
    r = client.post(f"/api/v1/integrations/{nb['id']}/sync", headers=L["customer"])
    assert r.status_code == 200
    out = client.get(f"/api/v1/integrations/{nb['id']}/outbox", headers=L["customer"]).json()
    assert out and all(o["target"].startswith("https://netbox.example.org/api/") for o in out)


# ---- cloud on-ramps ---------------------------------------------------------------------------


def test_onramps_simulated_by_default(client, L):
    h, cid = L["customer"], L["customer_id"]
    base = f"/api/v1/customers/{cid}/onramps"
    for body in (
        {
            "provider": "aws_dx",
            "site": "site-a",
            "bandwidth_mbps": 50,
            "aws_account_id": "123456789012",
            "region": "us-east-1",
        },
        {
            "provider": "azure_er",
            "site": "site-a",
            "bandwidth_mbps": 50,
            "circuit_id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/"
            "Microsoft.Network/expressRouteCircuits/er1",
            "service_key": "11111111-2222-3333-4444-555555555555",
        },
        {
            "provider": "gcp_pi",
            "site": "site-a",
            "bandwidth_mbps": 100,
            "pairing_key": "7e51371e-72a3-40b5-b844-2e3efefaee59/us-central1/1",
        },
        {
            "provider": "megaport",
            "site": "site-a",
            "bandwidth_mbps": 100,
            "b_end_product_uid": "0b6ee4d1-4f5c-4b5e-9d4b-6a7e2f6f1a11",
        },
    ):
        r = client.post(base, json=body, headers=h)
        assert r.status_code == 201, (body["provider"], r.text)
        o = r.json()
        assert o["simulated"] is True and o["status"] in ("pending", "ordering", "available") and o["external_id"]
        got = client.get(f"{base}/{o['id']}", headers=h).json()
        assert got["id"] == o["id"]
        assert client.post(f"{base}/{o['id']}/refresh", headers=h).status_code == 200
        assert client.delete(f"{base}/{o['id']}", headers=h).json()["status"] == "deleted"
    assert (
        client.post(
            base, json={"provider": "aws_dx", "bandwidth_mbps": 70, "aws_account_id": "123456789012"}, headers=h
        ).status_code
        == 400
    )
    other = other_org(client)
    assert client.get(base, headers=other["h"]).status_code in (403, 404)
    assert len(client.get(base, headers=h).json()) == 4
    assert {a["key"] for a in client.get("/api/v1/onramps/adapters", headers=h).json()} == {
        "aws_dx",
        "azure_er",
        "gcp_pi",
        "megaport",
    }


def test_aws_direct_connect_live_against_a_fake(client, L, live, fake, monkeypatch):
    monkeypatch.setenv("EXA_AWS_DX_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("EXA_AWS_DX_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("EXA_AWS_DX_INTERCONNECT_ID", "dxcon-fgabc123")
    monkeypatch.setenv("EXA_AWS_DX_ENDPOINT", fake.url + "/")

    def handler(req):
        target = req["headers"].get("x-amz-target", "")
        if target.endswith("AllocateHostedConnection"):
            return 200, {
                "connectionId": "dxcon-hosted1",
                "connectionState": "ordering",
                "vlan": req["json"]["vlan"],
                "location": "EqMI1",
            }
        if target.endswith("DescribeConnections"):
            return 200, {"connections": [{"connectionId": "dxcon-hosted1", "connectionState": "available"}]}
        if target.endswith("DeleteConnection"):
            return 200, {"connectionId": "dxcon-hosted1", "connectionState": "deleted"}
        return None

    fake.handler = handler
    base = f"/api/v1/customers/{L['customer_id']}/onramps"
    r = client.post(
        base,
        json={
            "provider": "aws_dx",
            "site": "site-a",
            "bandwidth_mbps": 50,
            "aws_account_id": "123456789012",
            "region": "us-east-1",
        },
        headers=L["customer"],
    )
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["simulated"] is False and o["external_id"] == "dxcon-hosted1" and o["status"] == "pending"
    req = fake.requests[0]
    assert req["headers"]["x-amz-target"] == "OvertureService.AllocateHostedConnection"
    assert req["headers"]["content-type"] == "application/x-amz-json-1.1"
    assert req["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/directconnect/aws4_request" in req["headers"]["authorization"]
    assert req["json"]["ownerAccount"] == "123456789012" and req["json"]["bandwidth"] == "50Mbps"
    assert req["json"]["connectionId"] == "dxcon-fgabc123"
    r = client.post(f"{base}/{o['id']}/refresh", headers=L["customer"])
    assert r.json()["status"] == "available" and r.json()["provider_state"] == "available"
    assert client.delete(f"{base}/{o['id']}", headers=L["customer"]).json()["status"] == "deleted"
    assert fake.requests[-1]["headers"]["x-amz-target"] == "OvertureService.DeleteConnection"
    # Secrets never land in the on-ramp record.
    with db.tx() as conn:
        assert "wJalrX" not in json.dumps(conn.execute("SELECT * FROM connect_onramps").fetchall(), default=str)


def test_megaport_live_against_a_fake(client, L, live, fake, monkeypatch):
    monkeypatch.setenv("EXA_MEGAPORT_CLIENT_ID", "mp-id")
    monkeypatch.setenv("EXA_MEGAPORT_CLIENT_SECRET", "mp-secret")
    monkeypatch.setenv("EXA_MEGAPORT_PORT_UID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("EXA_MEGAPORT_API_URL", fake.url)
    monkeypatch.setenv("EXA_MEGAPORT_AUTH_URL", fake.url + "/oauth2/token")
    fake.reply("POST", "/oauth2/token", 200, {"access_token": "mp-access", "expires_in": 3600})
    fake.reply(
        "POST",
        "/v3/networkdesign/buy",
        200,
        {"data": [{"technicalServiceUid": "vxc-1", "provisioningStatus": "DEPLOYABLE"}]},
    )
    base = f"/api/v1/customers/{L['customer_id']}/onramps"
    r = client.post(
        base,
        json={
            "provider": "megaport",
            "site": "site-a",
            "bandwidth_mbps": 100,
            "b_end_product_uid": "0b6ee4d1-4f5c-4b5e-9d4b-6a7e2f6f1a11",
            "service_key": "pair-1",
        },
        headers=L["customer"],
    )
    assert r.status_code == 201, r.text
    tok, buy = fake.requests
    assert tok["raw"] == b"grant_type=client_credentials"
    assert buy["headers"]["authorization"] == "Bearer mp-access"
    vxc = buy["json"][0]["associatedVxcs"][0]
    assert buy["json"][0]["productUid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" and vxc["rateLimit"] == 100
    assert vxc["bEnd"] == {
        "productUid": "0b6ee4d1-4f5c-4b5e-9d4b-6a7e2f6f1a11",
        "partnerConfig": {"connectType": "PAIRING", "pairingKey": "pair-1"},
    }
    assert r.json()["external_id"] == "vxc-1"


# ---- the publish point, fed by the rest of Connect --------------------------------------------


def test_events_across_connect_reach_subscribers(client, L, admin_headers, vault):
    w = add(
        client,
        L["customer"],
        "webhook",
        secrets={"url": "https://hooks.example.org/x"},
        event_types=["*", "audit.recorded"],
    )
    node, a_h = _enrol(client, L["tokens"], "site-a")
    run_jobs()
    # BFD down and up.
    _bfd(client, a_h, "bfd_down")
    _bfd(client, a_h, "bfd_up", 5)
    # A failed apply, reported twice: one event.
    for _ in range(2):
        client.post(
            "/api/v1/agent/status", headers=a_h, json={"applied_version": 1, "ok": False, "error": "nft: syntax error"}
        )
    # Storm Mode.
    site = next(s for s in client.get("/api/v1/sites", headers=admin_headers).json() if s["name"] == "site-a")
    assert client.post(f"/api/v1/sites/{site['id']}/storm", json={"on": True}, headers=admin_headers).status_code in (
        200,
        204,
    )
    run_jobs()
    with db.tx() as conn:
        evs = conn.execute(
            "SELECT type, dedup_key, action, data FROM connect_events WHERE customer_id = %s ORDER BY time",
            (L["customer_id"],),
        ).fetchall()
    names = [e["type"].rsplit(".", 2)[-2] + "." + e["type"].rsplit(".", 1)[-1] for e in evs]
    for needed in ("node.enrolled", "path.down", "path.up", "config.apply_failed", "storm.on", "audit.recorded"):
        assert needed in names, (needed, names)
    assert names.count("config.apply_failed") == 1
    down = next(e for e in evs if e["type"].endswith("path.down"))
    up = next(e for e in evs if e["type"].endswith("path.up"))
    assert down["dedup_key"] == up["dedup_key"] == f"path:{site['id']}:carrier-a"
    assert down["action"] == "trigger" and up["action"] == "resolve"
    assert down["data"]["carrier"] == "Carrier A" and down["data"]["link_id"] == link(L, "site-a", "carrier-a")
    assert all(d["status"] == "simulated" for d in deliveries(w["id"]))

    # Node offline after 90 s of silence, and back.
    with db.tx() as conn:
        conn.execute("UPDATE nodes SET last_seen = now() - interval '5 minutes' WHERE name = 'site-a'")
        assert publish.tick(conn)["offline"] == 1
        assert publish.tick(conn)["offline"] == 0  # once
        conn.execute("UPDATE nodes SET last_seen = clock_timestamp() + interval '1 second' WHERE name = 'site-a'")
        assert publish.tick(conn)["online"] == 1
    # Revoked.
    assert client.post(f"/api/v1/nodes/{node['node_id']}/revoke", headers=admin_headers).status_code == 200
    run_jobs()
    with db.tx() as conn:
        types = {r["type"].split("connect.")[1] for r in conn.execute("SELECT type FROM connect_events")}
    assert {"node.offline", "node.online", "node.revoked"} <= types


def test_routing_decisions_become_events(client, L, vault):
    w = add(
        client,
        L["customer"],
        "webhook",
        secrets={"url": "https://hooks.example.org/x"},
        event_types=["sla.*", "routing.*"],
    )
    tokens = L["tokens"]
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    ramp = lambda i: max(0.0, (i - 12) * 0.12)  # noqa: E731
    body = {
        "at": now.isoformat(),
        "probes": _windows(now, "wg-a", ramp, 25.0) + _windows(now, "wg-b", lambda i: 0.0, 35.0),
        "tunnels": [
            {"name": "wg-a", "path": "carrier-a", "handshake_age_s": 3, "bfd": "up"},
            {"name": "wg-b", "path": "carrier-b", "handshake_age_s": 3, "bfd": "up"},
        ],
        "steering": [{"class": "voice", "path": "carrier-a"}],
        "steering_version": m["version"],
    }
    client.post("/api/v1/agent/telemetry", headers=a_h, json=body)
    runner.run_once(now)
    made = runner.run_once(now + dt.timedelta(seconds=1))
    assert made and made[0]["class"] == "voice"
    run_jobs()
    kinds = [d["event_type"] for d in deliveries(w["id"])]
    assert "routing.moved" in kinds and ("sla.breach_forecast" in kinds or "sla.breach" in kinds)
    with db.tx() as conn:
        moved = conn.execute("SELECT * FROM connect_events WHERE type LIKE '%routing.moved'").fetchone()
    assert moved["data"]["reason"].startswith("Moved voice from Carrier A to Carrier B")
    assert moved["dedup_key"].startswith("sla:") and moved["action"] == "resolve"


def test_nothing_is_queued_without_a_subscriber(client, L):
    _, a_h = _enrol(client, L["tokens"], "site-a")
    _bfd(client, a_h, "bfd_down")
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs WHERE kind LIKE 'integrations.%'").fetchone()["n"] == 0


# ---- single-item reads and who may read them --------------------------------------------------


def test_single_item_reads_and_authorisation(client, L, admin_headers):
    h, cid = L["customer"], L["customer_id"]
    other = other_org(client)
    a = link(L, "site-a", "carrier-a")
    node, _ = _enrol(client, L["tokens"], "site-a")

    assert client.get(f"/api/v1/customers/{cid}", headers=h).json()["id"] == cid
    assert client.get(f"/api/v1/customers/{cid}", headers=other["h"]).status_code in (403, 404)
    r = client.patch(f"/api/v1/customers/{cid}", json={"name": "Renamed Bank"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["name"] == "Renamed Bank"
    assert client.patch(f"/api/v1/customers/{cid}", json={"name": "x"}, headers=h).status_code == 403

    lk = client.get(f"/api/v1/links/{a}", headers=h).json()
    assert lk["id"] == a and lk["path"] == "carrier-a"
    assert client.get(f"/api/v1/links/{a}", headers=other["h"]).status_code == 404
    assert client.get(f"/api/v1/links/{a}", headers=L["carrier_a"]).status_code == 200
    assert client.get(f"/api/v1/links/{a}", headers=L["carrier_b"]).status_code == 404
    assert client.get("/api/v1/links/not-a-uuid", headers=h).status_code in (404, 422)
    assert {x["carrier"] for x in client.get("/api/v1/links", headers=L["carrier_a"]).json()} == {"Carrier A"}
    assert client.get("/api/v1/links", headers=other["h"]).json() == []
    assert len(client.get("/api/v1/links", headers=h).json()) == len(L["links"])
    site_a = L["sites"]["site-a"]
    assert {x["path"] for x in client.get(f"/api/v1/sites/{site_a}/links", headers=h).json()} >= {
        "carrier-a",
        "carrier-b",
    }
    assert client.get(f"/api/v1/sites/{site_a}/links", headers=other["h"]).status_code == 404

    nid = node["node_id"]
    assert client.get(f"/api/v1/nodes/{nid}", headers=h).json()["id"] == nid
    assert client.get(f"/api/v1/nodes/{nid}", headers=other["h"]).status_code == 404

    ca = L["carriers"]["Carrier A"]
    assert client.get(f"/api/v1/carriers/{ca}", headers=L["carrier_a"]).json()["name"] == "Carrier A"
    assert client.get(f"/api/v1/carriers/{ca}", headers=L["carrier_b"]).status_code in (403, 404)
    assert client.get(f"/api/v1/carriers/{ca}", headers=admin_headers).status_code == 200

    assert client.get(f"/api/v1/customers/{cid}/classes/voice", headers=h).json()["name"] == "voice"
    assert client.get(f"/api/v1/customers/{cid}/classes/nope", headers=h).status_code == 404
    assert client.get(f"/api/v1/customers/{cid}/classes/voice", headers=other["h"]).status_code in (403, 404)

    with db.tx() as conn:
        me = conn.execute("SELECT id::text AS id, email FROM users WHERE customer_id = %s", (cid,)).fetchone()
    assert client.get(f"/api/v1/users/{me['id']}", headers=admin_headers).json()["email"] == me["email"]
    assert client.get(f"/api/v1/users/{me['id']}", headers=other["h"]).status_code in (403, 404)

    k = client.post("/api/v1/auth/api-keys", json={"name": "ci", "days": 7}, headers=h).json()
    got = client.get(f"/api/v1/auth/api-keys/{k['id']}", headers=h).json()
    assert got["name"] == "ci" and "token" not in got
    assert client.get(f"/api/v1/auth/api-keys/{k['id']}", headers=other["h"]).status_code == 404

    for path in ("/api/v1/decisions/999999", "/api/v1/insights/999999", "/api/v1/releases/999999"):
        assert client.get(path, headers=h).status_code in (403, 404)


def test_a_decision_is_readable_by_its_owner_only(client, L):
    with db.tx() as conn:
        site = L["sites"]["site-a"]
        d = conn.execute(
            """INSERT INTO decisions (time, customer_id, site_id, class_name, kind, from_path, to_path, reason, inputs,
                                     engine)
               VALUES (now(), %s, %s, 'voice', 'move', 'carrier-a', 'carrier-b', 'test', '{}', 'rules') RETURNING id""",
            (L["customer_id"], site),
        ).fetchone()
    other = other_org(client)
    got = client.get(f"/api/v1/decisions/{d['id']}", headers=L["customer"])
    assert got.status_code == 200 and got.json()["reason"] == "test"
    assert client.get(f"/api/v1/decisions/{d['id']}", headers=other["h"]).status_code == 404


# ---- IPFIX export through desired state ---------------------------------------------------


def test_ipfix_export_reaches_sites_through_desired_state(client, L):
    tokens = L["tokens"]
    _, pop_h = _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    _, b_h = _enrol(client, tokens, "site-b")
    before = client.get("/api/v1/agent/desired-state", headers=a_h).json()
    assert "ipfix" not in before
    h = L["customer"]
    assert (
        client.post(
            "/api/v1/integrations",
            json={"provider": "ipfix", "name": "x", "config": {"collector_host": "a b"}},
            headers=h,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/integrations",
            json={"provider": "ipfix", "name": "x", "config": {"collector_host": "c.example", "collector_port": 0}},
            headers=h,
        ).status_code
        == 400
    )
    ix = add(
        client,
        h,
        "ipfix",
        config={"collector_host": "flows.bank.example", "collector_port": 2055},
        site_ids=[L["sites"]["site-a"]],
    )
    assert ix["event_types"] == [] and ix["mode"] == "simulated"  # no events; the live switch is off
    a = client.get("/api/v1/agent/desired-state", headers=a_h).json()
    assert a["version"] > before["version"]
    assert a["ipfix"]["collector"] == "flows.bank.example:2055" and a["ipfix"]["observation_domain"] > 0
    assert "ipfix" not in client.get("/api/v1/agent/desired-state", headers=b_h).json()  # not chosen
    assert "ipfix" not in client.get("/api/v1/agent/desired-state", headers=pop_h).json()  # never the PoP
    # Every site, an IPv6 collector and a fixed observation domain.
    client.patch(
        f"/api/v1/integrations/{ix['id']}",
        json={"site_ids": [], "config": {"collector_host": "2001:db8::10", "observation_domain": 9}},
        headers=h,
    )
    b = client.get("/api/v1/agent/desired-state", headers=b_h).json()
    assert b["ipfix"] == {"collector": "[2001:db8::10]:2055", "observation_domain": 9}
    assert client.post(f"/api/v1/integrations/{ix['id']}/test", headers=h).status_code == 400
    # Switched off, then deleted: gone from desired state.
    client.patch(f"/api/v1/integrations/{ix['id']}", json={"enabled": False}, headers=h)
    assert "ipfix" not in client.get("/api/v1/agent/desired-state", headers=b_h).json()
    client.patch(f"/api/v1/integrations/{ix['id']}", json={"enabled": True}, headers=h)
    assert client.delete(f"/api/v1/integrations/{ix['id']}", headers=h).status_code == 204
    assert "ipfix" not in client.get("/api/v1/agent/desired-state", headers=a_h).json()
