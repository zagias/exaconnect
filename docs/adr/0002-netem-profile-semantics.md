# ADR 0002: What the netem profile numbers mean

Date: 2026-09-30 · Status: accepted

## Context

CLAUDE.md §4.7 gives underlay profiles such as "carrier A: 25 ms, 2 ms jitter"
and "GEO: 600 ms". It does not say whether these are one-way or round-trip.
600 ms is the usual round-trip time for geostationary satellite, so the figures
read as round-trip.

## Decision

Profiles are round-trip. Each underlay router applies half the delay and half
the jitter on every egress interface (a packet crosses one egress per
direction), and applies the loss once, on the PoP-facing interface, so a
round-trip probe sees about the profile's loss.

A cut is 100 % loss on every interface with the links left up, as in a real
carrier outage; detection must come from BFD and probes, not link state.

## Consequences

One-way metrics (M3 probes carry timestamps) will show about half the profile
delay per direction, with loss only on the site → PoP direction. If one-way
loss in both directions matters later, split the loss across both directions.

## Revision, 2026-10-07: loss both ways, and settings that stick

`LOSS_BOTH_WAYS=1 lab/netem/apply-profiles.sh` splits each link's loss across
both directions: every egress interface drops p = 1 - sqrt(1 - L), so a round
trip still sees about L (a cut stays 100 % everywhere). It is off by default,
so existing runs see the same numbers. `apply-profiles.sh` saves this and
`SAT_PROFILE` in `lab/.state/settings`, so later fault and restore scripts, and
`make demo-seed` (which seeds the satellite link as `leo` or `geo` to match),
keep the lab as it was brought up. The environment still overrides both.
