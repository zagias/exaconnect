"""The SLA routing decision engine (CLAUDE.md §4.3).

Pure functions over plain data, so every rule is unit-tested without a
database: score each path against a class SLA, forecast each metric, decide
whether the class should move, apply hysteresis, and explain the decision in
one line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .forecast import Forecast, Forecaster, Series

METRICS = ("latency", "jitter", "loss")
UNITS = {"latency": " ms", "jitter": " ms", "loss": "%"}


@dataclass(frozen=True)
class Sla:
    latency_ms: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None

    def limit(self, metric: str) -> float | None:
        return {"latency": self.latency_ms, "jitter": self.jitter_ms, "loss": self.loss_pct}[metric]


@dataclass(frozen=True)
class Window:
    """One probe window from an agent (10 s in production)."""

    t: float  # end of the window, epoch seconds
    sent: int
    received: int
    rtt_ms: float | None
    jitter_ms: float | None


@dataclass(frozen=True)
class PathInput:
    name: str
    label: str
    ordinal: int
    satellite: bool = False
    bfd_down: bool = False
    over_commit: bool = False
    windows: Sequence[Window] = ()


@dataclass(frozen=True)
class Policy:
    horizon_s: float = 60
    hold_s: float = 120
    return_after_s: float = 300
    # A path "scores well" (for moving back) when every metric is at most half its limit.
    good_score: float = 0.5
    # Consecutive evaluations a breach must be seen or forecast before acting.
    persistence: int = 2
    # SLA windows: loss is a rate, so it needs more probes to be meaningful
    # (3 min at 20 probes/s is 3,600 probes; 1% is 36 of them).
    loss_window_s: float = 180
    latency_window_s: float = 60
    jitter_window_s: float = 60
    # A breach is forecast only when (forecast - confidence x standard error)
    # crosses the limit; an alternative is healthy only when the forecast plus
    # that margin stays under it.
    confidence: float = 1.0
    history_s: float = 300
    # No probe replies for this long means the path is dead.
    dead_after_s: float = 30
    # No probe data at all for this long means we know nothing about it.
    stale_after_s: float = 45


STORM_POLICY = Policy(horizon_s=120, hold_s=60)


@dataclass(frozen=True)
class MetricView:
    now: float
    ahead: float
    slope_per_min: float
    se: float
    limit: float
    score_now: float
    score_ahead: float  # of the point forecast
    breach_forecast: bool  # confidently forecast to breach
    safe_ahead: bool  # confidently forecast to stay within SLA


@dataclass(frozen=True)
class PathEval:
    name: str
    label: str
    ordinal: int
    satellite: bool
    over_commit: bool
    up: bool
    down_reason: str | None
    known: bool  # enough recent data to judge it
    metrics: dict[str, MetricView] = field(default_factory=dict)

    @property
    def score_now(self) -> float:
        return min((m.score_now for m in self.metrics.values()), default=1.0)

    @property
    def score_ahead(self) -> float:
        return min((m.score_ahead for m in self.metrics.values()), default=1.0)

    @property
    def breach(self) -> bool:
        """Breaching now, or confidently forecast to breach within the horizon."""
        return any(m.score_now == 0 or m.breach_forecast for m in self.metrics.values())

    @property
    def healthy(self) -> bool:
        """Usable, within SLA now and confidently forecast to stay within it."""
        return self.up and self.known and all(m.score_now > 0 and m.safe_ahead for m in self.metrics.values())

    def worst(self) -> tuple[str, MetricView] | None:
        if not self.metrics:
            return None
        return min(
            self.metrics.items(),
            key=lambda kv: (not (kv[1].score_now == 0 or kv[1].breach_forecast), kv[1].score_ahead, kv[1].score_now),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "up": self.up,
            "known": self.known,
            "over_commit": self.over_commit,
            "score_now": round(self.score_now, 3),
            "score_ahead": round(self.score_ahead, 3),
            "metrics": {
                k: {
                    "now": round(m.now, 3),
                    "ahead": round(m.ahead, 3),
                    "slope_per_min": round(m.slope_per_min, 3),
                    "se": round(m.se, 3),
                    "limit": m.limit,
                }
                for k, m in self.metrics.items()
            },
        }


@dataclass(frozen=True)
class State:
    path: str
    since: float
    return_path: str | None = None
    return_since: float | None = None
    breach_streak: int = 0
    note: str | None = None


@dataclass(frozen=True)
class Decision:
    kind: str  # move, move_back, failover, hold
    from_path: str | None
    to_path: str | None
    reason: str


def score(value: float, limit: float) -> float:
    """1 = comfortable, 0 = at or past the limit."""
    if limit <= 0:
        return 1.0 if value <= 0 else 0.0
    return max(0.0, min(1.0, (limit - value) / limit))


def series(windows: Sequence[Window], metric: str, step: float) -> Series:
    if metric == "loss":
        pts = [(w.t, 100.0 * (w.sent - w.received) / w.sent, float(w.sent)) for w in windows if w.sent > 0]
        return Series([t for t, _, _ in pts], [v for _, v, _ in pts], [x for _, _, x in pts], binomial=True, step=step)
    attr = "rtt_ms" if metric == "latency" else "jitter_ms"
    pts = [(w.t, getattr(w, attr)) for w in windows if w.received and getattr(w, attr) is not None]
    return Series([t for t, _ in pts], [v for _, v in pts], [1.0] * len(pts), step=step)


def evaluate(
    p: PathInput, sla: Sla, forecaster: Forecaster, policy: Policy, now: float, step: float = 10.0
) -> PathEval:
    windows = sorted((w for w in p.windows if now - policy.history_s < w.t <= now), key=lambda w: w.t)
    recent = [w for w in windows if w.t > now - policy.dead_after_s and w.sent > 0]
    known = any(w.t > now - policy.stale_after_s and w.sent > 0 for w in windows)
    down_reason = None
    if p.bfd_down:
        down_reason = "BFD reports it down"
    elif recent and sum(w.received for w in recent) == 0:
        down_reason = f"no probe replies for {int(policy.dead_after_s)} s"
    span = {"loss": policy.loss_window_s, "latency": policy.latency_window_s, "jitter": policy.jitter_window_s}
    metrics: dict[str, MetricView] = {}
    for metric in METRICS:
        limit = sla.limit(metric)
        if limit is None or not windows:
            continue
        f: Forecast | None = forecaster.forecast(series(windows, metric, step), policy.horizon_s, span[metric])
        if f is None:
            continue
        lim = float(limit)
        margin = policy.confidence * f.se
        metrics[metric] = MetricView(
            now=f.now,
            ahead=f.ahead,
            slope_per_min=f.slope_per_min,
            se=f.se,
            limit=lim,
            score_now=score(f.now, lim),
            score_ahead=score(f.ahead, lim),
            breach_forecast=f.ahead - margin >= lim,
            safe_ahead=f.ahead + margin < lim,
        )
    return PathEval(
        name=p.name,
        label=p.label,
        ordinal=p.ordinal,
        satellite=p.satellite,
        over_commit=p.over_commit,
        up=down_reason is None,
        down_reason=down_reason,
        known=known,
        metrics=metrics,
    )


def preference(p: PathEval) -> tuple:
    """Static order: terrestrial before satellite, then the configured order."""
    return (p.satellite, p.ordinal)


def cost_rank(p: PathEval) -> int:
    """Prefer paths within commit; burst and satellite only when the SLA needs them."""
    if p.satellite:
        return 2
    return 1 if p.over_commit else 0


def _fmt(metric: str, v: float) -> str:
    if metric == "loss":
        return f"{v:.1f}%"
    return f"{v:.0f} ms"


def _limit(metric: str, v: float) -> str:
    return f"{v:g}%" if metric == "loss" else f"{v:g} ms"


def explain_breach(p: PathEval, horizon_s: float) -> str:
    w = p.worst()
    if w is None:
        return f"{p.label} has no recent measurements"
    metric, m = w
    if m.score_now == 0:
        return f"{metric} on {p.label} is {_fmt(metric, m.now)} against a {_limit(metric, m.limit)} SLA"
    rate = _fmt(metric, abs(m.slope_per_min))
    trend = "rising" if m.slope_per_min >= 0 else "falling"
    return (
        f"{metric} on {p.label} {trend} {rate} a minute, forecast {_fmt(metric, m.ahead)} "
        f"in {horizon_s:g} s against a {_limit(metric, m.limit)} SLA"
    )


def _brief(p: PathEval) -> str:
    parts = [
        _fmt(k, m.now) + ("" if k != "loss" else " loss") for k, m in p.metrics.items() if k in ("loss", "latency")
    ]
    return ", ".join(parts)


def decide(
    cls: str,
    paths: dict[str, PathEval],
    candidates: Sequence[str],
    state: State | None,
    policy: Policy,
    now: float,
) -> tuple[State, Decision | None]:
    """One evaluation for one class at one site.

    `candidates` are the paths the class may use right now (satellite only in
    Storm Mode, and only if the class allows it). Returns the new state and the
    decision, if any. A "hold" decision is a note: nothing moves, but the
    portal shows why (logged once per episode).
    """
    cands = [paths[c] for c in candidates if c in paths]
    if not cands:
        st = state or State(path="", since=now)
        return st, None
    by_pref = sorted(cands, key=preference)
    home = by_pref[0]
    if state is None or not state.path:
        state = State(path=home.name, since=now - policy.hold_s)

    def best_alternative(exclude: str, need_healthy: bool) -> PathEval | None:
        alts = [p for p in cands if p.name != exclude and p.up and (p.healthy if need_healthy else True)]
        if not alts:
            return None
        return min(alts, key=lambda p: (cost_rank(p), -p.score_ahead, p.ordinal))

    def moved(to: PathEval, kind: str, reason: str) -> tuple[State, Decision]:
        return State(path=to.name, since=now), Decision(kind, state.path, to.name, reason)

    def note(text: str) -> tuple[State, Decision | None]:
        if state.note == text:
            return state, None
        return replace(state, note=text), Decision("hold", state.path, state.path, text)

    cur = paths.get(state.path)

    # 1. The class is on a path it may no longer use (Storm Mode ended, link removed).
    if cur is None or state.path not in candidates:
        to = best_alternative(state.path, True) or best_alternative(state.path, False) or home
        why = "it is no longer allowed for this class" if cur is not None else "that path no longer exists"
        label = cur.label if cur is not None else state.path
        return moved(to, "move", f"Moved {cls} from {label} to {to.label}: {why}.")

    # 2. Hard down: leave at once, whatever the hold time says.
    if not cur.up:
        to = best_alternative(cur.name, True) or best_alternative(cur.name, False)
        if to is None:
            return note(f"Kept {cls} on {cur.label}: it is down ({cur.down_reason}) and no other path is up.")
        return moved(
            to, "failover", f"Moved {cls} from {cur.label} to {to.label}: {cur.label} is down ({cur.down_reason})."
        )

    # 3. Breach seen or forecast on the current path.
    breaching = cur.known and cur.breach
    streak = state.breach_streak + 1 if breaching else 0
    state = replace(state, breach_streak=streak)
    if breaching and streak >= policy.persistence:
        if now - state.since < policy.hold_s:
            return note(
                f"Kept {cls} on {cur.label}: {explain_breach(cur, policy.horizon_s)}, "
                f"but it moved here {int(now - state.since)} s ago (hold time {policy.hold_s:g} s)."
            )
        to = best_alternative(cur.name, True)
        if to is None:
            return note(
                f"Kept {cls} on {cur.label}: {explain_breach(cur, policy.horizon_s)}, "
                f"but no other path is forecast within SLA."
            )
        return moved(
            to, "move", f"Moved {cls} from {cur.label} to {to.label}: {explain_breach(cur, policy.horizon_s)}."
        )

    # 4. Move back to a preferred path once it has scored well for a while.
    better = [
        p
        for p in by_pref
        if preference(p) < preference(cur)
        and p.up
        and p.known
        and p.score_now >= policy.good_score
        and p.score_ahead >= policy.good_score
    ]
    target = better[0] if better else None
    if target is None:
        if state.return_path is not None:
            state = replace(state, return_path=None, return_since=None)
    else:
        if state.return_path != target.name:
            state = replace(state, return_path=target.name, return_since=now)
        assert state.return_since is not None
        good_for = now - state.return_since
        if good_for >= policy.return_after_s and now - state.since >= policy.hold_s:
            return moved(
                target,
                "move_back",
                f"Moved {cls} back from {cur.label} to {target.label}: {target.label} has scored well for "
                f"{int(good_for // 60)} minutes ({_brief(target)}).",
            )

    # Healthy and staying put: clear the note so the next episode is logged.
    if not breaching and state.note is not None:
        state = replace(state, note=None)
    return state, None
