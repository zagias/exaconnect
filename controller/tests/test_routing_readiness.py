"""Fresh telemetry is not proof that every configured SLA can be met."""

from exaconnect_controller.routing.engine import PathInput, Policy, Sla, State, Window, decide, evaluate
from exaconnect_controller.routing.forecast import TrendForecaster

NOW = 1000.0
POLICY = Policy()
VOICE = Sla(latency_ms=150, jitter_ms=30, loss_pct=1)


def path(name, ordinal, *, count=30, rtt=30, jitter=3, down=False, sla=VOICE):
    windows = [Window(NOW - 10 * (count - i - 1), 200, 200, rtt, jitter) for i in range(count)]
    return evaluate(
        PathInput(name, name.upper(), ordinal, bfd_down=down, windows=windows), sla, TrendForecaster(), POLICY, NOW
    )


def test_verified_healthy_backup_beats_a_new_unassessed_link():
    paths = {"a": path("a", 1, down=True), "b": path("b", 2, count=1, rtt=500, jitter=100), "c": path("c", 3)}
    assert paths["b"].known  # a fresh report exists, but there is not a forecast yet
    assert not paths["b"].metrics_complete
    assert not paths["b"].healthy

    state, decision = decide("voice", paths, list(paths), State("a", since=0), POLICY, NOW)

    assert decision.kind == "failover"
    assert state.path == "c"


def test_a_missing_sla_metric_cannot_make_a_path_a_verified_backup():
    incomplete = path("b", 2, jitter=None)
    assert "loss" in incomplete.metrics and "latency" in incomplete.metrics
    assert "jitter" not in incomplete.metrics
    assert not incomplete.metrics_complete
    assert not incomplete.healthy


def test_unassessed_link_remains_a_last_resort_after_a_hard_failure():
    paths = {"a": path("a", 1, down=True), "b": path("b", 2, count=1)}

    state, decision = decide("voice", paths, list(paths), State("a", since=0), POLICY, NOW)

    assert decision.kind == "failover"
    assert state.path == "b"


def test_a_return_clock_does_not_finish_with_missing_sla_metrics():
    paths = {"a": path("a", 1, jitter=None), "b": path("b", 2)}
    prior = State("b", since=0, return_path="a", return_since=NOW - 400)

    state, decision = decide("voice", paths, list(paths), prior, POLICY, NOW)

    assert decision is None
    assert state.path == "b"
    assert state.return_path is None


def test_best_effort_path_needs_no_unconfigured_metrics():
    best_effort = path("a", 1, count=1, sla=Sla())

    assert best_effort.metrics_complete
    assert best_effort.healthy
