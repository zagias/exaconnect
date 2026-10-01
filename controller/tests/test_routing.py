"""Decision engine tests: hand-worked forecasts, scoring, hysteresis, and a
simulated brownout where the predictive engine moves voice before the SLA is
breached and the rules-only baseline moves only after."""

import random

import pytest

from exaconnect_controller.routing.engine import (
    STORM_POLICY,
    PathInput,
    Policy,
    Sla,
    State,
    Window,
    decide,
    evaluate,
    score,
)
from exaconnect_controller.routing.forecast import Holt, LastValue, Series, TrendForecaster

VOICE = Sla(latency_ms=150, jitter_ms=30, loss_pct=1)
BULK = Sla(loss_pct=5)
POLICY = Policy()


def test_score():
    assert score(0, 1) == 1
    assert score(0.5, 1) == 0.5
    assert score(1, 1) == 0
    assert score(2, 1) == 0
    assert score(75, 150) == 0.5


def _series(values, step=10.0, weights=None, binomial=False):
    return Series([i * step for i in range(1, len(values) + 1)], values, weights or [1.0] * len(values), binomial, step)


def test_trend_follows_a_line_exactly():
    # Latency rising 1 ms per 10 s window. With a 60 s SLA window and 60 s
    # horizon, the forecast window is all future: the mean of the line over
    # the next minute is the last value + 3.5 steps.
    s = _series([20.0 + i for i in range(18)])
    f = TrendForecaster().forecast(s, 60, 60)
    assert f.now == pytest.approx(sum(range(32, 38)) / 6)
    assert f.ahead == pytest.approx(37 + 3.5)
    assert f.slope_per_min == pytest.approx(6)
    assert f.se == pytest.approx(0, abs=1e-9)


def test_trend_projects_into_a_partly_measured_window():
    # Loss flat at 1% on 200 probes per window; 180 s window, 60 s horizon:
    # 12 windows already measured plus 60 s of forecast, all at 1%.
    s = _series([1.0] * 18, weights=[200.0] * 18, binomial=True)
    f = TrendForecaster().forecast(s, 60, 180)
    assert f.now == pytest.approx(1.0)
    assert f.ahead == pytest.approx(1.0)
    assert f.slope_per_min == pytest.approx(0)
    assert 0 < f.se < 0.3


def test_holt_hand_worked():
    # alpha 0.5, beta 0.5 on 1, 2, 4 (step 10 s); initial trend (4-1)/2 = 1.5.
    # t1: level = .5*2 + .5*(1+1.5) = 2.25; trend = .5*(1.25) + .5*1.5 = 1.375
    # t2: level = .5*4 + .5*(2.25+1.375) = 3.8125; trend = .5*1.5625 + .5*1.375 = 1.46875
    # Horizon 20 s = 2 steps, window 20 s: the forecast window is all future.
    f = Holt(alpha=0.5, beta=0.5, min_points=3).forecast(_series([1, 2, 4]), 20, 20)
    assert f.ahead == pytest.approx(3.8125 + 2 * 1.46875)
    assert f.slope_per_min == pytest.approx(1.46875 * 6)
    assert f.now == pytest.approx(3.0)  # mean of the last 20 s: (2 + 4) / 2


def test_forecasters_need_enough_points():
    assert TrendForecaster().forecast(_series([1, 2]), 60, 60) is None
    assert Holt().forecast(_series([1, 2]), 60, 60) is None
    f = LastValue().forecast(_series([1, 2]), 60, 60)
    assert f.ahead == f.now == 1.5


def _windows(t_end, n, loss=0.0, rtt=25.0, jitter=3.0, step=10.0, sent=200):
    out = []
    for i in range(n):
        t = t_end - (n - 1 - i) * step
        recv = sent - round(sent * loss / 100)
        out.append(Window(t=t, sent=sent, received=recv, rtt_ms=rtt, jitter_ms=jitter))
    return out


def _paths(now, a_loss=0.0, b_loss=0.0, a_down=False, b_down=False, sat=False):
    ps = {
        "carrier-a": PathInput("carrier-a", "Carrier A", 1, windows=_windows(now, 30, a_loss), bfd_down=a_down),
        "carrier-b": PathInput("carrier-b", "Carrier B", 2, windows=_windows(now, 30, b_loss, rtt=35), bfd_down=b_down),
    }
    if sat:
        ps["sat"] = PathInput("sat", "Satellite", 3, satellite=True, windows=_windows(now, 30, 0.5, rtt=45, jitter=10))
    return ps


def _eval(ps, sla, now, policy=None, f=None):
    policy = policy or POLICY
    f = f or TrendForecaster()
    return {k: evaluate(p, sla, f, policy, now) for k, p in ps.items()}


def test_evaluate_rolling_loss_and_down():
    now = 1000.0
    ev = _eval(_paths(now, a_loss=2.0), VOICE, now)
    assert ev["carrier-a"].metrics["loss"].now == pytest.approx(2.0)
    assert ev["carrier-a"].score_now == 0
    assert ev["carrier-b"].healthy
    ev = _eval(_paths(now, a_loss=100.0), VOICE, now)
    assert not ev["carrier-a"].up and "no probe replies" in ev["carrier-a"].down_reason
    ev = _eval(_paths(now, b_down=True), VOICE, now)
    assert not ev["carrier-b"].up and ev["carrier-b"].down_reason == "BFD reports it down"


def test_steady_breach_moves_after_persistence():
    now = 1000.0
    ev = _eval(_paths(now, a_loss=2.0), VOICE, now)
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], None, POLICY, now)
    assert d is None and st.breach_streak == 1 and st.path == "carrier-a"
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now + 10)
    assert d.kind == "move" and d.to_path == "carrier-b"
    assert d.reason == "Moved voice from Carrier A to Carrier B: loss on Carrier A is 2.0% against a 1% SLA."


def test_no_alternative_logs_one_hold_note():
    now = 1000.0
    ev = _eval(_paths(now, a_loss=2.0, b_loss=3.0), VOICE, now)
    st = State("carrier-a", since=0, breach_streak=1)
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now)
    assert d.kind == "hold" and "no other path is forecast within SLA" in d.reason
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now + 10)
    assert d is None and st.path == "carrier-a"


def test_hard_down_ignores_hold_time():
    now = 1000.0
    ev = _eval(_paths(now, b_down=True), VOICE, now)
    st = State("carrier-b", since=now - 5)
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now)
    assert d.kind == "failover" and d.to_path == "carrier-a"
    assert d.reason == "Moved voice from Carrier B to Carrier A: Carrier B is down (BFD reports it down)."


def test_hold_time_blocks_a_second_move():
    now = 1000.0
    ev = _eval(_paths(now, a_loss=0.0, b_loss=2.0), VOICE, now)
    st = State("carrier-b", since=now - 30, breach_streak=5)
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now)
    assert d.kind == "hold" and "hold time 120 s" in d.reason and st.path == "carrier-b"


def test_moves_back_only_after_five_good_minutes():
    t0 = 1000.0
    st = State("carrier-b", since=t0 - 200)
    moved_at = None
    for i in range(0, 60):
        now = t0 + i * 10
        ev = _eval(_paths(now), VOICE, now)
        st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now)
        if d is not None:
            assert d.kind == "move_back" and d.to_path == "carrier-a"
            assert "has scored well for 5 minutes" in d.reason
            moved_at = now
            break
    assert moved_at == t0 + 300


def test_a_wobble_restarts_the_return_clock():
    t0 = 1000.0
    st = State("carrier-b", since=t0 - 200)
    for i in range(0, 25):  # 240 s good
        now = t0 + i * 10
        st, d = decide("voice", _eval(_paths(now), VOICE, now), ["carrier-a", "carrier-b"], st, POLICY, now)
        assert d is None
    now = t0 + 250  # carrier A dips to 0.8% loss: still within SLA but not "good"
    st, d = decide("voice", _eval(_paths(now, a_loss=0.8), VOICE, now), ["carrier-a", "carrier-b"], st, POLICY, now)
    assert d is None and st.return_path is None
    now = t0 + 260
    st, d = decide("voice", _eval(_paths(now), VOICE, now), ["carrier-a", "carrier-b"], st, POLICY, now)
    assert d is None and st.return_since == now


def test_storm_mode_satellite_and_leaving_it():
    now = 1000.0
    ev = _eval(_paths(now, a_down=True, b_down=True, sat=True), VOICE, now, STORM_POLICY)
    st = State("carrier-a", since=0)
    st, d = decide("voice", ev, ["carrier-a", "carrier-b", "sat"], st, STORM_POLICY, now)
    assert d.kind == "failover" and d.to_path == "sat"
    # Storm Mode off: satellite is no longer a candidate for the class.
    ev = _eval(_paths(now + 10, sat=True), VOICE, now + 10)
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now + 10)
    assert d.kind == "move" and d.to_path == "carrier-a" and "no longer allowed" in d.reason


def test_prefers_paths_within_commit():
    now = 1000.0
    ps = _paths(now, a_loss=2.0)
    ps["carrier-b"] = PathInput("carrier-b", "Carrier B", 2, over_commit=True, windows=_windows(now, 30, 0, rtt=35))
    ps["carrier-c"] = PathInput("carrier-c", "Carrier C", 4, windows=_windows(now, 30, 0, rtt=60))
    ev = _eval(ps, VOICE, now)
    st = State("carrier-a", since=0, breach_streak=1)
    st, d = decide("voice", ev, list(ps), st, POLICY, now)
    assert d.to_path == "carrier-c"


# --- Simulated brownout (demo step 2) --------------------------------------

LABELS = {"carrier-a": "Carrier A", "carrier-b": "Carrier B"}


def _simulate(engine, seed, ramp_s=180, peak=3.0, sla=VOICE, probes_per_window=200):
    """Carrier A loss ramps 0 -> peak over ramp_s starting at t=300 (20 probes/s).
    Returns (time the class left A, time A's measured SLA window first breached, reason)."""
    rng = random.Random(seed)
    f = TrendForecaster() if engine == "trend" else Holt() if engine == "holt" else LastValue()
    hist = {"carrier-a": [], "carrier-b": []}
    st = None
    moved = breached = reason = None
    for i in range(1, 120):
        now = i * 10.0
        frac = min(1.0, max(0.0, (now - 300) / ramp_s))
        for name, loss, rtt in (("carrier-a", peak * frac, 25), ("carrier-b", 0.0, 35)):
            lost = sum(1 for _ in range(probes_per_window) if rng.random() < loss / 100)
            hist[name].append(
                Window(now, probes_per_window, probes_per_window - lost, rtt + rng.gauss(0, 2), abs(rng.gauss(4, 2)))
            )
        ps = {k: PathInput(k, LABELS[k], n + 1, windows=hist[k][-40:]) for n, k in enumerate(hist)}
        recent = [w for w in hist["carrier-a"] if w.t > now - POLICY.loss_window_s]
        measured = 100 * sum(w.sent - w.received for w in recent) / sum(w.sent for w in recent)
        if breached is None and measured >= (sla.loss_pct or 1e9):
            breached = now
        st, d = decide("voice", _eval(ps, sla, now, f=f), list(ps), st, POLICY, now)
        if d is not None and d.kind == "move" and moved is None:
            moved, reason = now, d.reason
    return moved, breached, reason


@pytest.mark.parametrize("seed", range(20))
def test_brownout_predictive_moves_before_breach(seed):
    moved, breached, reason = _simulate("trend", seed)
    assert moved is not None and breached is not None
    assert moved < breached, (moved, breached)
    assert "forecast" in reason and "against a 1% SLA" in reason


@pytest.mark.parametrize("seed", range(8))
def test_brownout_rules_baseline_moves_after_breach(seed):
    moved, breached, _ = _simulate("rules", seed)
    assert moved is not None and moved >= breached


@pytest.mark.parametrize("seed", range(8))
def test_bulk_stays_on_a_through_the_brownout(seed):
    moved, _, _ = _simulate("trend", seed, sla=BULK)
    assert moved is None


@pytest.mark.parametrize(("seed", "background"), [(s, b) for s in range(6) for b in (0.2, 0.5)])
def test_no_flapping_on_noisy_healthy_paths(seed, background):
    """An hour of healthy paths with realistic noise: background loss, latency
    spikes and jittery windows. At 0.2% loss nothing moves. At 0.5% (half the
    voice SLA) a confident-looking forecast can rarely move a class once, but
    it must never bounce back and forth."""
    rng = random.Random(1000 + seed)
    hist = {"carrier-a": [], "carrier-b": []}
    st = None
    moves = []
    for i in range(1, 360):
        now = i * 10.0
        for name, rtt in (("carrier-a", 25), ("carrier-b", 35)):
            lost = sum(1 for _ in range(200) if rng.random() < background / 100)
            spike = 40 if rng.random() < 0.05 else 0
            hist[name].append(Window(now, 200, 200 - lost, rtt + abs(rng.gauss(0, 3)) + spike, abs(rng.gauss(6, 4))))
        ps = {k: PathInput(k, LABELS[k], n + 1, windows=hist[k][-40:]) for n, k in enumerate(hist)}
        st, d = decide("voice", _eval(ps, VOICE, now), list(ps), st, POLICY, now)
        if d is not None and d.kind != "hold":
            moves.append(d)
    assert len(moves) <= (0 if background < 0.5 else 1), moves


def test_noisy_but_good_alternative_is_still_a_place_to_go():
    # Carrier B's jitter swings 16..34 ms around 25 against a 30 ms SLA: too noisy
    # to be "confidently safe", but within SLA and far better than A in breach.
    now = 1000.0
    ps = _paths(now, a_loss=2.0)
    b = ps["carrier-b"].windows
    for i, w in enumerate(b):
        b[i] = Window(t=w.t, sent=w.sent, received=w.received, rtt_ms=w.rtt_ms, jitter_ms=16.0 if i % 2 else 34.0)
    ev = _eval(ps, VOICE, now)
    assert not all(m.safe_ahead for m in ev["carrier-b"].metrics.values())
    st = State("carrier-a", since=0, breach_streak=1)
    st, d = decide("voice", ev, ["carrier-a", "carrier-b"], st, POLICY, now)
    assert d.kind == "move" and d.to_path == "carrier-b"
