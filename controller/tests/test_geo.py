"""GEO satellite end to end: the lab seed honours SAT_PROFILE, and voice never
uses GEO (its round trip is past what a call can bear) while business may in
Storm Mode."""

from __future__ import annotations

import datetime as dt

import pytest

from exaconnect_controller import db, seed
from exaconnect_controller.routing import maps, runner

from .test_flow import _enrol, _seed
from .test_steering import _windows

VOICE = {"name": "voice", "priority": "realtime", "allow_satellite": True, "preferred_path": None}
BUSINESS = {"name": "business", "priority": "interactive", "allow_satellite": True, "preferred_path": None}
BULK = {"name": "bulk", "priority": "bulk", "allow_satellite": False, "preferred_path": None}
CUSTOMER = {"storm_allow_bulk_sat": False}


def _paths(sat_type: str) -> list[dict]:
    return [
        {"name": "carrier-a", "ordinal": 1, "satellite": False, "underlay_type": "fibre"},
        {"name": "carrier-b", "ordinal": 2, "satellite": False, "underlay_type": "broadband"},
        {"name": "sat", "ordinal": 3, "satellite": True, "underlay_type": sat_type},
    ]


@pytest.mark.parametrize("storm", [False, True])
def test_candidates_keep_voice_off_geo(storm):
    site = {"storm_mode": storm}
    geo, leo = _paths("geo"), _paths("leo")
    terrestrial = ["carrier-a", "carrier-b"]
    assert maps.candidates(VOICE, CUSTOMER, site, geo) == terrestrial
    assert maps.candidates(BUSINESS, CUSTOMER, site, geo) == terrestrial + (["sat"] if storm else [])
    assert maps.candidates(VOICE, CUSTOMER, site, leo) == terrestrial + (["sat"] if storm else [])
    assert maps.candidates(BULK, CUSTOMER, site, geo) == terrestrial
    # With nowhere but GEO left, voice pauses rather than follow BGP onto it.
    assert maps.pause_if_none(VOICE, CUSTOMER, site, geo) is True
    assert maps.pause_if_none(VOICE, CUSTOMER, site, leo) is False
    assert maps.pause_if_none(BUSINESS, CUSTOMER, site, geo) is False
    assert maps.pause_if_none(BULK, CUSTOMER, site, geo) is True
    # Even when the admin lets bulk onto satellite, voice stays off GEO.
    allow = {"storm_allow_bulk_sat": True}
    assert "sat" not in maps.candidates(VOICE, allow, site, geo)
    assert ("sat" in maps.candidates(BULK, allow, site, geo)) is storm


def test_seed_honours_the_satellite_profile(client, monkeypatch):
    def sat_type():
        with db.tx() as conn:
            rows = conn.execute("SELECT DISTINCT underlay_type FROM links WHERE path = 'sat'").fetchall()
        return {r["underlay_type"] for r in rows}

    _seed()
    assert sat_type() == {"leo"}
    _seed("geo")
    assert sat_type() == {"geo"}  # re-seeding switches it
    _seed("leo")
    assert sat_type() == {"leo"}
    with pytest.raises(ValueError):
        _seed("meo")

    # The command line: --sat, else SAT_PROFILE, else leo.
    for argv, env, want in [
        (["--lab"], None, "leo"),
        (["--lab"], "geo", "geo"),
        (["--lab", "--sat", "leo"], "geo", "leo"),
        (["--lab", "--sat", "geo"], None, "geo"),
    ]:
        if env is None:
            monkeypatch.delenv("SAT_PROFILE", raising=False)
        else:
            monkeypatch.setenv("SAT_PROFILE", env)
        assert seed.parse_args(argv).sat == want
    for argv, env in [(["--lab", "--sat", "meo"], None), (["--lab"], "meo"), ([], None)]:
        monkeypatch.setenv("SAT_PROFILE", env or "leo")
        with pytest.raises(SystemExit):
            seed.parse_args(argv)


def test_storm_mode_over_geo(client, admin_headers):
    s = _seed("geo")
    tokens, cid = s["tokens"], s["customer_id"]
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")

    assert client.post(f"/api/v1/customers/{cid}/storm", headers=admin_headers, json={"on": True}).status_code == 200
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    rules = {r["class"]: r for r in m["rules"]}
    assert m["storm"] is True
    # Only business may use GEO; voice and bulk pause if the terrestrial paths fail.
    assert rules["business"]["paths"] == ["carrier-a", "carrier-b", "sat"]
    assert rules["voice"]["paths"] == ["carrier-a", "carrier-b"] and rules["voice"]["pause_if_none"] is True
    assert rules["bulk"]["paths"] == ["carrier-a", "carrier-b"] and rules["bulk"]["pause_if_none"] is True
    # The satellite tunnel is still warm: probed and in BGP.
    ds = client.get("/api/v1/agent/desired-state", headers=a_h).json()
    assert next(t for t in ds["tunnels"] if t["name"] == "wg-sat")["probe"] is not None

    # Both terrestrial paths down, GEO up at 600 ms and 1% loss.
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    body = {
        "at": now.isoformat(),
        "probes": _windows(now, "wg-a", lambda i: 0.0, 25.0)
        + _windows(now, "wg-b", lambda i: 0.0, 35.0)
        + _windows(now, "wg-sat", lambda i: 1.0, 600.0),
        "tunnels": [
            {"name": "wg-a", "path": "carrier-a", "handshake_age_s": 3, "bfd": "down"},
            {"name": "wg-b", "path": "carrier-b", "handshake_age_s": 3, "bfd": "down"},
            {"name": "wg-sat", "path": "sat", "handshake_age_s": 3, "bfd": "up"},
        ],
    }
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204
    made = {d["class"]: d for d in runner.run_once(now)}
    assert (made["business"]["kind"], made["business"]["to_path"]) == ("failover", "sat")
    assert made["voice"]["kind"] == "hold" and made["voice"]["to_path"] != "sat"
    assert made["bulk"]["kind"] == "hold" and made["bulk"]["to_path"] != "sat"

    # The agents are told the same: business is on GEO, voice is not.
    with db.tx() as conn:
        steering = {
            r["class_name"]: r["path"]
            for r in conn.execute(
                "SELECT class_name, path FROM steering st JOIN sites s ON s.id = st.site_id WHERE s.name = 'site-a'"
            ).fetchall()
        }
    assert steering["business"] == "sat" and steering["voice"] != "sat"
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    rules = {r["class"]: r for r in m["rules"]}
    assert rules["business"]["paths"][0] == "sat" and "sat" not in rules["voice"]["paths"]
