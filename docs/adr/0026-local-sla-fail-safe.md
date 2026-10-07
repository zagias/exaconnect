# ADR 0026: The agent checks SLAs itself while the controller is silent

Date: 2026-10-07 · Status: accepted

## Context

CLAUDE.md §4.1 says an agent that has not heard the controller for 60 s keeps
the current steering map and moves classes off paths BFD reports down. BFD
only sees a dead path. A carrier that browns out during a controller outage
(loss creeping up, latency climbing) kept voice on it until the controller
came back.

## Decision

- Each class in the steering map carries its SLA limits as an optional `sla`
  object (`max_latency_ms`, `max_jitter_ms`, `max_loss_pct`, each only when
  set). Older agents ignore it; newer agents accept maps without it.
- The agent keeps its own view of every class on every path from each 10 s
  probe window, all the time: a window outside the limits demotes the path for
  that class; three good windows in a row restore it. A window with nothing
  sent changes nothing. Latency is the probe round trip, as the controller
  scores it.
- The view is used only while the controller is silent. A usable path (tunnel
  present, BFD not down) that is demoted for a class is passed over for the
  next usable one that is not. If every usable path is demoted, the class
  keeps the least bad one (largest value-over-limit ratio is smallest), so a
  class is never stranded and bulk is not paused for an SLA breach.
- Moves are reported as `class_moved` events with `why: sla` and `failover:
  true`. When the controller is back its map applies again at once.

## Consequences

- No forecasting on the agent: it reacts to a breach, it does not predict one.
  The controller's engine still moves classes early when it is reachable.
- Three windows is 30 s before a recovered path takes a class back, which is
  short of the controller's 5 minute move-back rule; it is a fail-safe, not
  the engine.
