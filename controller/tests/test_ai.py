"""AI features: hurricane watch, bill-shock forecast, carrier anomalies and
"Ask your network" (against a local stand-in for the LLM API)."""

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from exaconnect_controller import db
from exaconnect_controller.ai import anomaly, billshock, hazards, insights, storms
from exaconnect_controller.ai import ask as ask_mod
from exaconnect_controller.security import hash_password

from .test_flow import _enrol, _seed

KINGSTON = (17.97, -76.79)
PORT_OF_SPAIN = (10.66, -61.51)


# ---- Hurricane watch ----


def test_coordinates_and_distance():
    assert storms.coord("33.7N") == 33.7 and storms.coord("43.6W") == -43.6 and storms.coord(-61.5) == -61.5
    # Kingston to Port of Spain is about 1,835 km great-circle.
    assert storms.distance_km(*KINGSTON, *PORT_OF_SPAIN) == pytest.approx(1835, rel=0.01)
    lat, lon = storms.move(0.0, 0.0, 90.0, 111.195)  # one degree east along the equator
    assert lat == pytest.approx(0.0, abs=1e-6) and lon == pytest.approx(1.0, abs=1e-3)


def test_example_hurricane_warns_kingston_only():
    hurricane, far = storms.parse(storms.example_feed())
    assert hurricane.label == "Hurricane Example" and hurricane.speed_kmh == pytest.approx(22.5, rel=0.01)
    km, hours = storms.closest_approach(hurricane, *KINGSTON)
    assert km < 150 and 20 <= hours <= 30
    w = storms.assess(hurricane, "site-a", *KINGSTON)
    assert w["severity"] == "critical" and w["data"]["suggest_storm_mode"] is True
    assert "Consider switching Storm Mode on" in w["detail"] and "straight-line projection" in w["detail"]
    assert storms.assess(hurricane, "site-b", *PORT_OF_SPAIN) is None
    assert storms.assess(far, "site-a", *KINGSTON) is None


def test_unexpected_entries_are_skipped():
    assert storms.parse({"activeStorms": [{"name": "no id"}]}) == []
    assert storms.parse({}) == []


def test_storm_watch_insights_and_example_api(client, admin_headers):
    seed = _seed()
    cid = seed["customer_id"]
    with db.tx() as conn:
        assert storms.run_once(conn, storms.example_feed()) == 1
        assert storms.run_once(conn, storms.example_feed()) == 1  # refreshed, not duplicated
    ins = client.get("/api/v1/insights", headers=admin_headers).json()
    assert len(ins) == 1 and ins[0]["site"] == "site-a" and ins[0]["severity"] == "critical"
    with db.tx() as conn:
        storms.run_once(conn, {"activeStorms": []})  # the storm has gone
    assert client.get("/api/v1/insights", headers=admin_headers).json() == []
    resolved = client.get("/api/v1/insights?include_resolved=true", headers=admin_headers).json()
    assert resolved[0]["resolved_at"] is not None

    # The demo button raises the same warning, labelled as example data.
    r = client.post("/api/v1/ai/storm-watch/example", headers=admin_headers)
    assert r.status_code == 200 and r.json()["open_warnings"] == 1 and r.json()["open_hazards"] == 1
    ex = client.get(f"/api/v1/insights?customer_id={cid}&kind=storm_warning", headers=admin_headers).json()[0]
    assert ex["example"] is True and ex["title"].startswith("Example data: ")
    r = client.post(f"/api/v1/insights/{ex['id']}/acknowledge", headers=admin_headers)
    assert r.status_code == 200
    client.post("/api/v1/ai/storm-watch/example?on=false", headers=admin_headers)
    assert client.get("/api/v1/insights", headers=admin_headers).json() == []


def test_storm_warnings_follow_storm_mode_and_lost_coordinates(client, admin_headers):
    _seed()
    with db.tx() as conn:
        storms.run_once(conn, storms.example_feed())
    w = client.get("/api/v1/insights", headers=admin_headers).json()[0]
    assert "Consider switching Storm Mode on" in w["detail"]

    # Storm Mode switched on elsewhere: the next pass stops advising it.
    with db.tx() as conn:
        conn.execute("UPDATE sites SET storm_mode = true WHERE name = 'site-a'")
        storms.run_once(conn, storms.example_feed())
    w = client.get("/api/v1/insights", headers=admin_headers).json()[0]
    assert "Consider switching" not in w["detail"] and "Storm Mode is already on at site-a." in w["detail"]
    assert w["data"]["storm_mode_on"] is True

    # The only geolocated site loses its coordinates: the warning still resolves.
    with db.tx() as conn:
        conn.execute("UPDATE sites SET latitude = NULL, longitude = NULL")
        storms.run_once(conn, storms.example_feed())
    assert client.get("/api/v1/insights", headers=admin_headers).json() == []

    # A watch switched off closes what it left open.
    with db.tx() as conn:
        conn.execute("UPDATE sites SET latitude = 18.0, longitude = -76.8 WHERE name = 'site-a'")
        storms.run_once(conn, storms.example_feed())
        assert insights.resolve_kind(conn, "storm_warning") == 1


def test_one_storm_warning_per_customer_for_several_sites(client, admin_headers):
    _seed()
    with db.tx() as conn:
        # Move site-b near Kingston: the hurricane now threatens both sites.
        conn.execute("UPDATE sites SET latitude = 18.2, longitude = -77.5 WHERE name = 'site-b'")
        before = _insight_events(conn)
        assert storms.run_once(conn, storms.example_feed()) == 1
        assert storms.run_once(conn, storms.example_feed()) == 1
        assert _insight_events(conn) == before + 1
    (w,) = client.get("/api/v1/insights?kind=storm_warning", headers=admin_headers).json()
    assert sorted(s["name"] for s in w["data"]["sites"]) == ["site-a", "site-b"]
    assert w["site"] == w["data"]["sites"][0]["name"] and "and near 1 more site" in w["title"]


def _insight_events(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM events WHERE kind = 'insight'").fetchone()["n"]


# ---- Disaster watch ----

NOW = dt.datetime(2026, 9, 21, 15, 0, tzinfo=dt.UTC)


def _quake(source="usgs", id_="q1", mag=6.4, lat=10.95, lon=-61.2, minutes=0, alert="", severity=None):
    return hazards.Report(
        id=f"{source}:{id_}",
        source=source,
        kind="earthquake",
        title=f"Magnitude {mag} earthquake",
        lat=lat,
        lon=lon,
        time=NOW + dt.timedelta(minutes=minutes),
        magnitude=mag,
        alert=alert,
        severity=severity,
    )


def _tsunami(id_, title, minutes, lat=10.9, lon=-61.3):
    return hazards.Report(
        id=f"ptwc:{id_}",
        source="ptwc",
        kind="tsunami",
        title=title,
        lat=lat,
        lon=lon,
        time=NOW + dt.timedelta(minutes=minutes),
        severity=hazards.tsunami_severity(title),
    )


def _trinidad_reports(minutes=0):
    return [
        _quake("usgs", "q1", minutes=minutes),
        _quake("gdacs", "EQ1", mag=6.3, lat=10.93, lon=-61.18, minutes=minutes + 4, severity="warning"),
        _tsunami("m1", "Tsunami Information Statement Number 1", minutes + 10),
        _tsunami("m2", "Tsunami Information Statement Number 2", minutes + 40),
    ]


def test_feed_parsers_read_the_examples():
    rs = {r.id: r for r in hazards.example_reports(NOW)}
    q = rs["usgs:us7000exmp"]
    assert q.magnitude == 6.4 and (q.lat, q.lon) == (10.95, -61.2) and q.tsunami_flag and q.alert == "yellow"
    # GDACS lists the quake twice (point and impact shape): one report, from the point.
    eq = rs["gdacs:EQ1000001"]
    assert (eq.lat, eq.lon) == (10.93, -61.18) and eq.severity == "warning" and eq.magnitude == 6.3
    assert rs["gdacs:FL1000003"].kind == "flood" and rs["gdacs:TC1000002"].kind == "cyclone"
    msgs = [r for r in rs.values() if r.kind == "tsunami"]
    assert len(msgs) == 2 and all(m.severity == "info" for m in msgs)
    assert all(m.url == "https://www.tsunami.gov/" for m in msgs)
    assert hazards.parse_usgs({"features": [{"id": "x"}]}) == [] and hazards.parse_gdacs({}) == []
    with pytest.raises(ValueError):
        hazards.parse_tsunami('<!DOCTYPE x [<!ENTITY a "b">]><feed/>')


def test_gdacs_search_url():
    u = hazards.gdacs_url("https://gdacs.example/SEARCH", 2, NOW)
    assert u == (
        "https://gdacs.example/SEARCH?eventlist=EQ;TC;FL;VO;WF;TS&alertlevel=Green;Orange;Red"
        "&fromDate=2026-09-07&toDate=2026-09-22&pageSize=100&pageNumber=2"
    )
    assert hazards.gdacs_url("https://gdacs.example/x?a=1", 1, NOW) == "https://gdacs.example/x?a=1"


def test_gdacs_shape_centre():
    square = {"type": "Polygon", "coordinates": [[[-62.0, 10.0], [-60.0, 10.0], [-60.0, 12.0], [-62.0, 12.0]]]}
    assert hazards._centre(square) == (11.0, -61.0)
    assert hazards._centre({"type": "Point", "coordinates": [-61.5, 10.5]}) == (10.5, -61.5)


def test_tsunami_message_types():
    assert hazards.tsunami_severity("Tsunami Threat Message Number 1") == "critical"
    assert hazards.tsunami_severity("Tsunami Warning") == "critical"
    assert hazards.tsunami_severity("Tsunami Advisory") == "warning"
    assert hazards.tsunami_severity("Tsunami Information Statement Number 1") == "info"
    assert hazards.tsunami_severity("Final Tsunami Threat Message Number 3") == "info"


def test_felt_radius():
    assert hazards.felt_radius_km(4.5) == pytest.approx(61, rel=0.05)
    assert hazards.felt_radius_km(6.0) == pytest.approx(269, rel=0.05)
    assert hazards.felt_radius_km(7.0) == pytest.approx(724, rel=0.05)


def test_one_event_from_three_feeds():
    far = _quake("usgs", "far", mag=5.0, lat=61.2, lon=-150.1)
    events = hazards.cluster([*_trinidad_reports(), far])
    assert sorted(len(e.reports) for e in events) == [1, 4]
    big = next(e for e in events if len(e.reports) == 4)
    assert big.lead.id == "usgs:q1" and big.latest_tsunami().id == "ptwc:m2"
    assert hazards.site_impact(big, *PORT_OF_SPAIN)[0] == "critical"
    assert hazards.site_impact(big, *KINGSTON) is None


def test_clustering_keeps_separate_events_apart():
    # Same place an hour later: an aftershock is its own event.
    assert len(hazards.cluster([_quake("usgs", "a"), _quake("gdacs", "b", minutes=60)])) == 2
    # One feed never merges two of its own ids.
    assert len(hazards.cluster([_quake("usgs", "a"), _quake("usgs", "b", minutes=2)])) == 2
    # 200 km apart in the same minute: two quakes.
    assert len(hazards.cluster([_quake("usgs", "a"), _quake("gdacs", "b", lat=12.75)])) == 2


def test_latest_tsunami_message_wins():
    threat = _tsunami("m1", "Tsunami Threat Message Number 1", 10)
    final = _tsunami("m2", "Final Tsunami Threat Message Number 2", 90)
    assert hazards.site_impact(hazards.Event([threat]), *PORT_OF_SPAIN)[0] == "critical"
    assert hazards.site_impact(hazards.Event([threat, final]), *PORT_OF_SPAIN)[0] == "info"


def test_severity_by_distance_and_magnitude():
    near = (10.95, -61.0)  # about 22 km from the epicentre
    assert hazards.site_impact(hazards.Event([_quake(mag=4.6)]), *near)[0] == "info"
    assert hazards.site_impact(hazards.Event([_quake(mag=5.6)]), *near)[0] == "warning"
    assert hazards.site_impact(hazards.Event([_quake(mag=6.4)]), *near)[0] == "critical"
    # A magnitude 6.4 reaches about 400 km: Kingston, 1,800 km away, is clear.
    assert hazards.site_impact(hazards.Event([_quake(mag=6.4)]), *KINGSTON) is None
    # USGS's PAGER orange means significant damage is likely: critical anywhere in range.
    assert hazards.site_impact(hazards.Event([_quake(mag=5.0, alert="orange")]), *near)[0] == "critical"


def test_cyclones_in_nhc_waters_are_left_to_the_hurricane_watch():
    tc = next(r for r in hazards.example_reports(NOW) if r.kind == "cyclone")
    assert hazards.nhc_covers(tc)
    assert hazards.current([tc], tc.time, nhc_on=True) == []
    assert hazards.current([tc], tc.time, nhc_on=False) == [tc]
    old = _quake(minutes=-49 * 60)
    assert hazards.current([old], NOW, nhc_on=True) == []


def test_disaster_watch_raises_one_insight_per_event(client, admin_headers):
    _seed()
    with db.tx() as conn:
        before = _insight_events(conn)
        # USGS reports first; GDACS and the tsunami messages arrive on later passes.
        assert hazards.run_once(conn, _trinidad_reports()[:1], now=NOW + dt.timedelta(minutes=5)) == 1
        assert hazards.run_once(conn, _trinidad_reports()[:2], now=NOW + dt.timedelta(minutes=15)) == 1
        assert hazards.run_once(conn, _trinidad_reports(), now=NOW + dt.timedelta(minutes=45)) == 1
        # USGS drops it from its feed; GDACS still has it: the same insight carries on.
        assert hazards.run_once(conn, _trinidad_reports()[1:], now=NOW + dt.timedelta(minutes=60)) == 1
        assert _insight_events(conn) == before + 1  # one notification in all
    (i,) = client.get("/api/v1/insights?kind=hazard", headers=admin_headers).json()
    assert i["site"] == "site-b" and i["severity"] == "critical"
    assert i["data"]["sources"] == ["gdacs", "ptwc"] and i["data"]["sites"][0]["suggest_storm_mode"] is True
    assert "Latest tsunami message: Tsunami Information Statement Number 2" in i["detail"]

    with db.tx() as conn:
        # A feed that cannot be read keeps its insight open rather than clearing it.
        assert hazards.run_once(conn, [], failed={"gdacs", "ptwc"}, now=NOW + dt.timedelta(minutes=70)) == 1
        # Every feed read and the event gone: cleared, without a new notification...
        assert hazards.run_once(conn, [], now=NOW + dt.timedelta(minutes=80)) == 0
        # ...and if it comes back within a day, the same insight reopens.
        hazards.run_once(conn, _trinidad_reports()[1:2], now=NOW + dt.timedelta(minutes=90))
        assert _insight_events(conn) == before + 1
    ins = client.get("/api/v1/insights?kind=hazard&include_resolved=true", headers=admin_headers).json()
    assert len(ins) == 1 and ins[0]["id"] == i["id"] and ins[0]["resolved_at"] is None


def test_escalation_notifies_again_and_clears_the_acknowledgement(client):
    seed = _seed()
    cid = seed["customer_id"]
    with db.tx() as conn:
        before = _insight_events(conn)
        iid, new = insights.raise_insight(conn, cid, "hazard", "k", "warning", "t", "d")
        conn.execute("UPDATE insights SET acknowledged_by = 'x' WHERE id = %s", (iid,))
        assert insights.raise_insight(conn, cid, "hazard", "k", "warning", "t", "d") == (iid, False)
        assert insights.raise_insight(conn, cid, "hazard", "k", "info", "t", "d") == (iid, False)
        ack = conn.execute("SELECT acknowledged_by FROM insights WHERE id = %s", (iid,)).fetchone()
        assert new and ack["acknowledged_by"] == "x"
        assert insights.raise_insight(conn, cid, "hazard", "k", "critical", "t", "d") == (iid, True)
        ack = conn.execute("SELECT acknowledged_by FROM insights WHERE id = %s", (iid,)).fetchone()
        assert ack["acknowledged_by"] is None and _insight_events(conn) == before + 2


# ---- Bill shock ----

OCT = (dt.datetime(2026, 10, 1, tzinfo=dt.UTC), dt.datetime(2026, 11, 1, tzinfo=dt.UTC))


def _samples(values, start=OCT[0]):
    return [(start + dt.timedelta(minutes=5 * i), v) for i, v in enumerate(values)]


def test_bill_shock_hand_worked_floor():
    # October has 31 days = 8,928 five-minute slots; the 95th percentile discards 446.
    # 500 samples so far, all at 130 Mbps against a 100 Mbps commit: more samples are
    # above commit than the month can ever discard, so at least 130 is locked in.
    f = billshock.forecast(_samples([130.0] * 500), 100, OCT)
    assert (f.month_slots, f.budget, f.over_commit) == (8928, 446, 500)
    assert f.floor_mbps == 130.0 and f.forecast_mbps == 130.0
    a = billshock.assess(f, "Carrier A at site-a", 100, 6.0)
    assert a["severity"] == "critical" and "will bill at least 130.0 Mbps" in a["title"]
    assert a["data"]["burst_charge"] == "180.00"  # 30 Mbps x 6.00
    assert "locked in" in a["detail"]


def test_bill_shock_forecast_from_the_95th_percentile():
    # A day at 60 Mbps with a busy 10%: the observed 95th percentile is 140,
    # nothing is locked in yet (29 samples above commit, budget 446).
    vals = [60.0] * 259 + [140.0] * 29
    f = billshock.forecast(_samples(vals), 100, OCT)
    assert f.floor_mbps == 0.0 and f.over_commit == 29
    assert f.observed_p95 == 140.0 and f.forecast_mbps == pytest.approx(140.0 * f.trend)
    a = billshock.assess(f, "Carrier A at site-a", 100, 6.0)
    assert a["severity"] == "warning" and "forecast to bill" in a["title"]


def test_bill_shock_quiet_month_and_short_history():
    assert billshock.forecast(_samples([50.0] * 11), 100, OCT) is None  # under an hour
    f = billshock.forecast(_samples([40.0] * 300), 100, OCT)
    assert billshock.assess(f, "x", 100, 6.0) is None


def test_trend_factor():
    flat = [(float(h), 50.0) for h in range(48)]
    assert billshock.trend_factor(flat, 744) == 1.0
    rising = [(float(h), 10.0 + h) for h in range(48)]
    assert billshock.trend_factor(rising, 744) == 1.5  # clipped
    assert billshock.trend_factor(rising[:10], 744) == 1.0  # too little data


def test_bill_shock_run_once_raises_insights(client, admin_headers):
    seed = _seed()
    now = dt.datetime.now(dt.UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with db.tx() as conn:
        link = conn.execute(
            "SELECT l.id, l.customer_id, l.carrier_id FROM links l JOIN sites s ON s.id = l.site_id"
            " WHERE s.name = 'site-a' AND l.path = 'carrier-a'"
        ).fetchone()
        # Half an hour into the month is too little; give it the month so far at 130 Mbps.
        n = max(24, int((now - start).total_seconds() // 300))
        for i in range(n):
            b = start + dt.timedelta(minutes=5 * i)
            conn.execute(
                "INSERT INTO usage_5m (link_id, bucket, customer_id, carrier_id, in_mbps, out_mbps, seconds)"
                " VALUES (%s, %s, %s, %s, 130, 20, 300) ON CONFLICT DO NOTHING",
                (link["id"], b, link["customer_id"], link["carrier_id"]),
            )
        assert billshock.run_once(conn, now) == 1
    ins = client.get(f"/api/v1/insights?customer_id={seed['customer_id']}&kind=bill_shock", headers=admin_headers)
    row = ins.json()[0]
    assert row["path"] == "carrier-a" and row["data"]["forecast_mbps"] >= 130


# ---- Carrier anomalies ----


def test_anomaly_rules():
    b = anomaly.baseline([25.0, 26.0, 24.0, 25.0, 25.5, 24.5], "same hour")
    assert b.median == 25.0
    # The margin is the largest of 4 robust sigmas, 10 ms and 20%: here 10 ms.
    assert anomaly.limit("latency", b) == 35.0
    assert anomaly.check("latency", 34.0, b) is None
    assert anomaly.check("latency", 40.0, b) == "warning"
    assert anomaly.check("latency", 50.0, b) == "critical"
    lb = anomaly.baseline([0.0, 0.1, 0.0, 0.05], "last 24 h")
    assert anomaly.check("loss", 0.4, lb) is None and anomaly.check("loss", 0.8, lb) == "warning"


def test_anomaly_flags_carrier_b_and_carriers_see_their_own(client, admin_headers):
    seed = _seed()
    _enrol(client, seed["tokens"], "site-a")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    with db.tx() as conn:
        node = conn.execute("SELECT id, customer_id FROM nodes WHERE name = 'site-a'").fetchone()
        rows = []
        # 6 hours of history, one row a minute: carrier B at 35 ms, carrier A at 25 ms.
        for m in range(6 * 60, 0, -1):
            t = now - dt.timedelta(minutes=m)
            recent = m <= 15
            for path, rtt in (("carrier-a", 25.0), ("carrier-b", 70.0 if recent else 35.0)):
                rows.append((t, node["customer_id"], node["id"], path, 600, 600, 0.0, rtt, rtt, rtt, 3.0))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO path_metrics (time, customer_id, node_id, path, sent, received, loss_pct,
                     rtt_avg_ms, rtt_min_ms, rtt_max_ms, jitter_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
        assert anomaly.run_once(conn, now) == 1
        carrier_b = conn.execute("SELECT id FROM carriers WHERE name = 'Carrier B'").fetchone()["id"]
        carrier_a = conn.execute("SELECT id FROM carriers WHERE name = 'Carrier A'").fetchone()["id"]
        for email, cid in (("noc@b.example", carrier_b), ("noc@a.example", carrier_a)):
            conn.execute(
                "INSERT INTO users (email, password_hash, role, carrier_id) VALUES (%s, %s, 'carrier', %s)",
                (email, hash_password("carrier password 1"), cid),
            )
    ins = client.get("/api/v1/insights?kind=anomaly", headers=admin_headers).json()
    assert len(ins) == 1 and ins[0]["carrier"] == "Carrier B" and ins[0]["data"]["metric"] == "latency"
    assert "usually 35.0 ms" in ins[0]["title"]

    def as_carrier(email):
        tok = client.post("/api/v1/auth/login", json={"email": email, "password": "carrier password 1"}).json()
        return {"Authorization": f"Bearer {tok['token']}"}

    assert len(client.get("/api/v1/insights", headers=as_carrier("noc@b.example")).json()) == 1
    assert client.get("/api/v1/insights", headers=as_carrier("noc@a.example")).json() == []


# ---- Ask your network ----


class _FakeLLM(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeLLM.seen.append({"auth": self.headers.get("Authorization"), "body": body})
        out = {"choices": [{"message": {"content": "<think>hmm</think>Voice moved to Carrier B at 04:31 UTC."}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_llm():
    srv = HTTPServer(("127.0.0.1", 0), _FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _FakeLLM.seen = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_ask_your_network(client, admin_headers, fake_llm):
    seed = _seed()
    cid = seed["customer_id"]
    assert client.get("/api/v1/ai/status", headers=admin_headers).json()["ask_enabled"] is False
    r = client.post(
        "/api/v1/ai/ask", headers=admin_headers, json={"question": "Why did voice move?", "customer_id": cid}
    )
    assert r.status_code == 503

    from dataclasses import replace

    client.app.state.settings = replace(
        client.app.state.settings, llm_api_key="test-key-not-real", llm_base_url=fake_llm, llm_model="test-model"
    )
    assert client.get("/api/v1/ai/status", headers=admin_headers).json() == {
        "ask_enabled": True,
        "model": "test-model",
        "storm_watch": True,
        "hazard_watch": True,
    }
    r = client.post(
        "/api/v1/ai/ask", headers=admin_headers, json={"question": "Why did voice move?", "customer_id": cid}
    )
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "Voice moved to Carrier B at 04:31 UTC."
    assert "test-key-not-real" not in r.text
    sent = _FakeLLM.seen[0]
    assert sent["auth"] == "Bearer test-key-not-real" and sent["body"]["model"] == "test-model"
    user_msg = sent["body"]["messages"][1]["content"]
    assert "Why did voice move?" in user_msg and "Demo Organisation" in user_msg and "site-a" in user_msg
    # The snapshot is plain JSON.
    snap = json.loads(user_msg.split("(JSON):\n", 1)[1].split("\n\nPerson's message:", 1)[0])
    assert {"customer", "sites", "routing_decisions_last_7_days_newest_first", "metering_this_month"} <= set(snap)
    assert r.json()["plan"] is None
    actions = [a["action"] for a in client.get("/api/v1/audit", headers=admin_headers).json()]
    assert "ai.ask" in actions


def test_ask_error_is_safe(fake_llm):
    with pytest.raises(ask_mod.AskError) as e:
        ask_mod.ask("q", {}, api_key="k", base_url="http://127.0.0.1:9", model="m", timeout_s=2)
    assert "could not be reached" in str(e.value)
