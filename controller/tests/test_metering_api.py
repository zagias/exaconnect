"""Metering end to end against Postgres: counters -> 5-minute samples ->
settlement, carrier scoping, and the CSV matching the screen."""

import csv
import datetime as dt
import io

from exaconnect_controller import db
from exaconnect_controller.metering import rollup
from exaconnect_controller.metering.core import percentile95
from exaconnect_controller.security import hash_password

from .test_flow import _enrol, _seed


def _login(client, email, password):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_metering_settlement_carrier_view_and_csv(client, admin_headers):
    tokens = _seed()["tokens"]
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")

    # 60 minutes of counters on site-a's carrier A underlay (eth1), one a minute.
    # Rate steps up each 5 minutes: 20, 40, ... 240 Mbps in; out is half.
    end = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
    end = end - dt.timedelta(minutes=end.minute % 5)
    start = end - dt.timedelta(minutes=60)
    counters, rx, tx = [], 0, 0
    for m in range(61):
        t = start + dt.timedelta(minutes=m)
        counters.append({"at": t.isoformat(), "ifname": "eth1", "rx_bytes": rx, "tx_bytes": tx})
        mbps = 20 * (m // 5 + 1)
        rx += int(mbps * 1e6 / 8 * 60)
        tx += int(mbps * 1e6 / 8 * 60 / 2)
    r = client.post("/api/v1/agent/telemetry", headers=a_h, json={"at": end.isoformat(), "counters": counters})
    assert r.status_code == 204
    assert rollup.run_once(end + dt.timedelta(seconds=30)) == 12
    assert rollup.run_once(end + dt.timedelta(seconds=30)) == 12  # idempotent

    q = f"start={start.isoformat().replace('+00:00', 'Z')}&end={end.isoformat().replace('+00:00', 'Z')}"
    out = client.get(f"/api/v1/metering/links?{q}", headers=admin_headers).json()
    a = next(link for link in out["links"] if link["site"] == "site-a" and link["path"] == "carrier-a")
    # 12 samples, 20..240. 5% of 12 rounds down to 0, so the 95th percentile is the max: 240.
    assert a["samples"] == 12 and a["discarded"] == 0
    assert round(a["p95_in_mbps"], 3) == 240.0 and round(a["p95_out_mbps"], 3) == 120.0
    # Commit 100 at 4.00 = 400.00; burst 140 Mbps at 6.00 = 840.00.
    assert (a["commit_charge"], a["burst_mbps"], a["burst_charge"], a["total"]) == (
        "400.00",
        "140.000",
        "840.00",
        "1240.00",
    )

    detail = client.get(f"/api/v1/metering/links/{a['id']}/samples?{q}", headers=admin_headers).json()
    assert len(detail["points"]) == 12 and round(detail["points"][0]["in_mbps"], 3) == 20.0

    # The CSV carries exactly the samples on screen and the same settlement.
    text = client.get(f"/api/v1/metering/settlement.csv?{q}&link_id={a['id']}", headers=admin_headers).text
    rows = list(csv.reader(io.StringIO(text)))
    data = [r for r in rows[1:] if r and r[0] != "settlement"]
    assert rows[0][0] == "carrier" and len(data) == 12
    assert round(percentile95([max(float(r[6]), float(r[7])) for r in data]), 3) == round(a["billable_mbps"], 3)
    summary = next(r for r in rows if r and r[0] == "settlement")
    assert "total=1240.00" in summary

    # A carrier user sees only its own carrier's links, read only.
    with db.tx() as conn:
        carrier_b = conn.execute("SELECT id FROM carriers WHERE name = 'Carrier B'").fetchone()["id"]
        conn.execute(
            "INSERT INTO users (email, password_hash, role, carrier_id)"
            " VALUES ('noc@carrier-b.example', %s, 'carrier', %s)",
            (hash_password("carrier b password"), carrier_b),
        )
    c_h = _login(client, "noc@carrier-b.example", "carrier b password")
    mine = client.get(f"/api/v1/metering/links?{q}", headers=c_h).json()["links"]
    assert mine and {link["carrier"] for link in mine} == {"Carrier B"}
    assert client.get(f"/api/v1/metering/links/{a['id']}/samples?{q}", headers=c_h).status_code == 404
    text = client.get(f"/api/v1/metering/settlement.csv?{q}", headers=c_h).text
    assert "Carrier A" not in text
    assert client.get("/api/v1/overview", headers=c_h).status_code == 403
    assert client.get("/api/v1/metering/usage", headers=c_h).status_code == 403

    # Customer usage view: data per path and the share above commit.
    u = client.get(f"/api/v1/metering/usage?{q}", headers=admin_headers).json()
    pa = next(p for p in u["paths"] if p["site"] == "site-a" and p["path"] == "carrier-a")
    assert pa["gb"] > 0 and pa["over_commit_gb"] > 0 and u["totals"]["satellite_gb"] == 0
