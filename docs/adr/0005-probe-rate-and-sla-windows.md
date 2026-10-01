# ADR 0005: Probe rate, SLA windows and the forecaster

Date: 2026-10-01. Status: accepted. Changes CLAUDE.md §4.1 and §4.3.

## Context

The brief probes each path once a second and asks the engine to move `voice`
before its 1% loss SLA is breached when carrier A's loss ramps from 0% to 3%
over 3 minutes. At one probe a second, a 10 s window holds 10 probes, so loss
can only read 0%, 10%, 20%...; a minute holds 60 probes, where 1% loss is
0.6 lost probes. Nothing can tell 0.5% from 1% at that rate, so the
forecaster would either never act early or act on noise. Simulations
(`controller/tests/test_routing.py`) confirmed it.

## Decision

1. **Probe rate by underlay:** fibre and broadband 20 a second, LTE 5 a second
   (metered data), satellite once a second. At 20 a second the probes cost
   about 35 kbit/s per path including WireGuard overhead.
2. **SLA windows:** loss is measured over the last 3 minutes (3,600 probes at
   20 a second, so 1% is 36 lost probes); latency and jitter over the last
   minute. The portal and the decision log report these windows.
3. **Forecaster:** a weighted linear trend over the last 2 minutes, projected
   into the SLA window 60 s ahead, with a standard error (binomial for loss,
   residual for latency and jitter). The engine acts when the forecast minus
   one standard error crosses the limit, on two passes in a row; an
   alternative counts as healthy only when its forecast plus one standard
   error stays under the limit. Holt's method is kept behind the same
   interface, and a rules-only baseline that acts only on a measured breach is
   kept for comparison in tests.

## Evidence

In 20 simulated brownouts the trend engine moved voice before the measured
breach every time (median lead 30 s), the rules engine never did, and bulk
(5% threshold) never moved. On noisy healthy paths with 0.2% loss nothing
moved in an hour; at 0.5% loss (half the SLA) at most one move happened in an
hour, and never back and forth.

## Consequences

Probing costs a little bandwidth per path. The probe rate is per underlay type
in `controller/exaconnect_controller/desired.py` and can be tuned.
