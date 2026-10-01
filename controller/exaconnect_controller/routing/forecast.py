"""Short-term forecasts of path metrics against an SLA window.

An SLA value is measured over a window (loss over the last 3 minutes, latency
and jitter over the last minute). The forecaster predicts what that window
will read `horizon_s` from now, with a standard error, so the engine can act
only when a breach is forecast with confidence (CLAUDE.md §4.3).

Forecasters share one small interface so a trained model can replace them
later. TrendForecaster is the default; Holt is kept as the alternative named
in the brief; LastValue is the rules-only baseline used for comparison.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Series:
    """Per-window values of one metric, oldest first.

    For loss, values are percentages of probes and weights the probes sent,
    so the noise is binomial. For latency and jitter, weights are 1.
    """

    times: Sequence[float]
    values: Sequence[float]
    weights: Sequence[float]
    binomial: bool = False
    step: float = 10.0


@dataclass(frozen=True)
class Forecast:
    now: float  # the SLA window as measured now
    ahead: float  # the SLA window as forecast `horizon_s` from now
    slope_per_min: float
    se: float  # standard error of `ahead`


class Forecaster(Protocol):
    name: str

    def forecast(self, s: Series, horizon_s: float, window_s: float) -> Forecast | None: ...


def window_mean(s: Series, end: float, window_s: float) -> float | None:
    pts = [(v, w) for t, v, w in zip(s.times, s.values, s.weights, strict=True) if end - window_s < t <= end]
    total = sum(w for _, w in pts)
    return sum(v * w for v, w in pts) / total if total else None


def _fit(ts: Sequence[float], vs: Sequence[float], ws: Sequence[float]) -> tuple[float, float, float, float, float]:
    """Weighted least squares. Returns (t mean, v mean, slope, weight sum, sxx)."""
    W = sum(ws)
    tm = sum(t * w for t, w in zip(ts, ws, strict=True)) / W
    vm = sum(v * w for v, w in zip(vs, ws, strict=True)) / W
    sxx = sum(w * (t - tm) ** 2 for t, w in zip(ts, ws, strict=True))
    sxy = sum(w * (t - tm) * (v - vm) for t, v, w in zip(ts, vs, ws, strict=True))
    return tm, vm, (sxy / sxx if sxx else 0.0), W, sxx


@dataclass
class TrendForecaster:
    """A weighted linear trend over the last `fit_s` seconds, projected forward.

    The forecast SLA window at now+h is what was already measured inside it
    plus the trend line over the part still to come.
    """

    fit_s: float = 120
    min_points: int = 6
    name: str = "trend"

    def forecast(self, s: Series, horizon_s: float, window_s: float) -> Forecast | None:
        if not s.times:
            return None
        now_t = s.times[-1]
        idx = [i for i, t in enumerate(s.times) if t > now_t - self.fit_s and s.weights[i] > 0]
        if len(idx) < self.min_points:
            return None
        ts = [s.times[i] for i in idx]
        vs = [s.values[i] for i in idx]
        ws = [s.weights[i] for i in idx]
        tm, vm, slope, W, sxx = _fit(ts, vs, ws)
        now = window_mean(s, now_t, window_s)
        if now is None:
            return None

        end = now_t + horizon_s
        span = min(horizon_s, window_s)  # the part of the future window not yet measured
        # Future windows end at end-span+step ... end; their mid-point:
        mid = end - (span - s.step) / 2
        future = max(0.0, vm + slope * (mid - tm))
        rate = (sum(ws) / len(ws)) / s.step  # weight per second (probes/s for loss)
        n_future = rate * span
        obs = [(v, w) for t, v, w in zip(s.times, s.values, s.weights, strict=True) if end - window_s < t <= now_t]
        obs_w = sum(w for _, w in obs)
        total = obs_w + n_future
        ahead = (sum(v * w for v, w in obs) + future * n_future) / total

        if s.binomial:
            q = max(vm, 0.1) / 100  # floor: a clean path still has some uncertainty
            unit = q * (1 - q) * 1e4  # variance of one probe, in %^2
            se_line = math.sqrt(unit * (1 / W + (mid - tm) ** 2 / sxx)) if sxx else math.sqrt(unit / W)
            se_future = math.sqrt(unit / max(n_future, 1))
        else:
            resid = [v - (vm + slope * (t - tm)) for t, v in zip(ts, vs, strict=True)]
            sigma = math.sqrt(sum(r * r for r in resid) / max(1, len(resid) - 2))
            se_line = sigma * math.sqrt(1 / len(ts) + ((mid - tm) ** 2 / (sxx / (W / len(ts))) if sxx else 0))
            se_future = sigma / math.sqrt(max(span / s.step, 1))
        se = (n_future / total) * math.hypot(se_line, se_future)
        return Forecast(now=now, ahead=max(0.0, ahead), slope_per_min=slope * 60, se=se)


@dataclass
class Holt:
    """Holt's linear (double exponential) smoothing on the per-window values,
    projected into the SLA window like TrendForecaster. Kept as an alternative."""

    alpha: float = 0.3
    beta: float = 0.2
    min_points: int = 6
    name: str = "holt"

    def forecast(self, s: Series, horizon_s: float, window_s: float) -> Forecast | None:
        vs = [v for v, w in zip(s.values, s.weights, strict=True) if w > 0]
        if len(vs) < self.min_points:
            return None
        now = window_mean(s, s.times[-1], window_s)
        if now is None:
            return None
        level = vs[0]
        trend = (vs[min(3, len(vs) - 1)] - vs[0]) / min(3, len(vs) - 1)
        errs = []
        for v in vs[1:]:
            errs.append(v - (level + trend))
            prev = level
            level = self.alpha * v + (1 - self.alpha) * (level + trend)
            trend = self.beta * (level - prev) + (1 - self.beta) * trend
        h = horizon_s / s.step
        sigma = math.sqrt(sum(e * e for e in errs) / max(1, len(errs) - 2))
        var = 1 + sum((self.alpha * (1 + j * self.beta)) ** 2 for j in range(1, int(h)))
        point = level + trend * h
        frac = min(horizon_s, window_s) / window_s
        ahead = (1 - frac) * now + frac * point
        return Forecast(
            now=now, ahead=max(0.0, ahead), slope_per_min=trend * 60 / s.step, se=frac * sigma * math.sqrt(var)
        )


@dataclass
class LastValue:
    """The rules-only baseline: act only on what the SLA window reads now."""

    name: str = "rules"

    def forecast(self, s: Series, horizon_s: float, window_s: float) -> Forecast | None:
        if not s.times:
            return None
        now = window_mean(s, s.times[-1], window_s)
        if now is None:
            return None
        return Forecast(now=now, ahead=now, slope_per_min=0.0, se=0.0)
