"""Controller gaps cannot count as observed health or consecutive breaches."""

import datetime as dt

import pytest

from exaconnect_controller import db
from exaconnect_controller.routing import maps, runner
from exaconnect_controller.routing.engine import State, decide

from .test_flow import _enrol, _seed
from .test_geo import BUSINESS, CUSTOMER
from .test_geo import VOICE as VOICE_CLASS
from .test_geo import _paths as satellite_paths
from .test_routing import POLICY, VOICE, _eval, _paths
from .test_steering import _windows


@pytest.mark.parametrize("gap", [46, 600, -10])
def test_unobserved_time_restarts_the_return_clock(gap):
    now = 2000.0
    prior = State("carrier-b", since=0, return_path="carrier-a", return_since=1000, last_evaluated=now - gap)

    state, decision = decide("voice", _eval(_paths(now), VOICE, now), ["carrier-a", "carrier-b"], prior, POLICY, now)

    assert decision is None
    assert state.path == "carrier-b"
    assert state.return_since == now
    assert state.last_evaluated == now


def test_return_requires_five_observed_minutes_after_restart():
    now = 2000.0
    state = State("carrier-b", since=0, return_path="carrier-a", return_since=1000, last_evaluated=1100)
    for elapsed in range(0, 301, 10):
        at = now + elapsed
        state, decision = decide("voice", _eval(_paths(at), VOICE, at), ["carrier-a", "carrier-b"], state, POLICY, at)
        if elapsed < 300:
            assert decision is None
        else:
            assert decision.kind == "move_back" and state.path == "carrier-a"


def test_controller_gap_resets_breach_persistence_but_not_emergency_failover():
    now = 2000.0
    prior = State("carrier-a", since=0, breach_streak=1, last_evaluated=1000)
    state, decision = decide(
        "voice", _eval(_paths(now, a_loss=2), VOICE, now), ["carrier-a", "carrier-b"], prior, POLICY, now
    )
    assert decision is None and state.breach_streak == 1
    _, decision = decide(
        "voice", _eval(_paths(now + 10, a_loss=2), VOICE, now + 10), ["carrier-a", "carrier-b"], state, POLICY, now + 10
    )
    assert decision.kind == "move"

    state, decision = decide(
        "voice", _eval(_paths(now, a_down=True), VOICE, now), ["carrier-a", "carrier-b"], prior, POLICY, now
    )
    assert decision.kind == "failover" and state.path == "carrier-b"


def test_persisted_evaluation_time_is_the_decision_time():
    class Connection:
        def execute(self, sql, args):
            self.sql, self.args = sql, args

    conn = Connection()
    state = State("carrier-a", since=1000, last_evaluated=2000)
    runner._save_state(conn, {"id": "site"}, "voice", "customer", state)
    assert conn.args[-1].timestamp() == 2000
    assert "updated_at = EXCLUDED.updated_at" in conn.sql


@pytest.mark.parametrize("mode", ["local", "off"])
def test_public_destinations_cannot_bypass_the_sites_internet_policy(mode):
    site = {
        "id": "a",
        "kind": "site",
        "node_id": "node",
        "storm_mode": False,
        "internet_mode": mode,
        "lan_prefixes": ["192.168.10.0/24"],
    }
    inv = {
        "customer": {"shadow_mode": False, "storm_allow_bulk_sat": False},
        "sites": [site],
        "corporate": ["10.0.0.0/8", "192.168.10.0/24"],
        "links": {},
        "classes": [],
        "steering": {},
        "rules": [],
    }

    assert maps.build(inv, site)["corporate_prefixes"] == inv["corporate"]
    assert "corporate_prefixes" not in maps.build(inv, {**site, "internet_mode": "pop"})


@pytest.mark.parametrize("cls", [VOICE_CLASS, BUSINESS])
def test_excluded_satellite_cannot_be_reached_through_bgp_fallback(cls):
    site = {"storm_mode": False}
    paths = satellite_paths("leo")
    assert maps.candidates(cls, CUSTOMER, site, paths) == ["carrier-a", "carrier-b"]
    assert maps.pause_if_none(cls, CUSTOMER, site, paths)
    assert not maps.pause_if_none(cls, CUSTOMER, {"storm_mode": True}, paths)


def test_mixed_satellite_types_do_not_allow_realtime_bgp_fallback_onto_geo():
    paths = satellite_paths("leo") + [{"name": "geo", "ordinal": 4, "satellite": True, "underlay_type": "geo"}]
    site = {"storm_mode": True}
    assert maps.candidates(VOICE_CLASS, CUSTOMER, site, paths) == ["carrier-a", "carrier-b", "sat"]
    assert maps.pause_if_none(VOICE_CLASS, CUSTOMER, site, paths)


def test_runner_restarts_persisted_return_clock_after_controller_gap(client):
    seed = _seed()
    _, headers = _enrol(client, seed["tokens"], "site-a")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    assert (
        client.post(
            "/api/v1/agent/telemetry",
            headers=headers,
            json={
                "at": now.isoformat(),
                "probes": _windows(now, "wg-a", lambda _: 0, 25) + _windows(now, "wg-b", lambda _: 0, 35),
            },
        ).status_code
        == 204
    )
    runner.run_once(now)
    with db.tx() as conn:
        conn.execute(
            "UPDATE steering SET path = 'carrier-b', since = %s, return_path = 'carrier-a',"
            " return_since = %s, updated_at = %s WHERE customer_id = %s AND class_name = 'voice'",
            (
                now - dt.timedelta(hours=1),
                now - dt.timedelta(minutes=10),
                now - dt.timedelta(minutes=6),
                seed["customer_id"],
            ),
        )

    at = now + dt.timedelta(seconds=10)
    made = runner.run_once(at)
    assert not any(d["class"] == "voice" and d["kind"] == "move_back" for d in made)
    with db.tx() as conn:
        state = conn.execute(
            "SELECT path, return_since, updated_at FROM steering WHERE customer_id = %s AND class_name = 'voice'",
            (seed["customer_id"],),
        ).fetchone()
    assert state["path"] == "carrier-b"
    assert state["return_since"] == at and state["updated_at"] == at
