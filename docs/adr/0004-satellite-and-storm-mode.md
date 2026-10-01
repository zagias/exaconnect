# ADR 0004: Satellite in normal operation and in Storm Mode

Date: 2026-10-01. Status: accepted.

## Context

The brief keeps `bulk` off satellite unless Storm Mode allows it, and lets
`voice` and `business` onto satellite in Storm Mode when both terrestrial
paths fail. It does not say what happens outside Storm Mode when both
terrestrial paths fail.

## Decision

- The satellite tunnel is always configured, probed once a second and in BGP
  at the lowest preference. Outside Storm Mode it carries no steered class.
- The steering map lists, per class, the paths a class may use in order. Outside
  Storm Mode the satellite is never in the list. In Storm Mode it is added last
  for classes whose SLA policy allows satellite, and for every class if the
  admin allowed bulk on satellite.
- When no listed path is up, a class that may use satellite falls back to BGP
  (which reaches the far end over satellite if that is all that is left): we
  prefer degraded calls to no calls. A class that may never use satellite
  (`bulk` by default) is paused with a blackhole rule instead, so BGP cannot
  put it there.
- Storm Mode also shortens the hold time to 60 s and lengthens the forecast
  horizon to 120 s (CLAUDE.md §4.4).

## Consequences

Outside Storm Mode, voice survives a double terrestrial failure through BGP,
a little slower to converge than steering. Storm Mode makes the satellite an
explicit, pre-warmed backup and lets the agent fail over to it locally.
