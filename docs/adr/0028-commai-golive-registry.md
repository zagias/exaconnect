# ADR 0028: CommAI go-live registry

Status: accepted, 7 October 2026

## Context

Phase 3 of CommAI adds countries, channels, languages, carriers and regions. The
scope says each is "switched on against written operating and security criteria",
and nothing is claimed before it is true.

## Decision

One registry for every such capability (`commai/golive.py`, `sql/55_golive.sql`).

- Modules declare each capability and its written criteria in code with
  `golive.declare(kind, key, name, criteria)`. Every capability also carries three
  base criteria: tested end to end, security review, and an operations runbook.
- Declared capabilities are written at start-up and **start off**. Start-up never
  changes a status or a check.
- An ExaCarib admin records each criterion as met, with evidence (a test run, a
  document, a review). Only when all are met can the status become `pilot` (named
  customers only) or `on` (everyone). Marking a criterion unmet switches the
  capability off again.
- Code asks `golive.enabled()` or `golive.require()` before using a capability.
- Every check and status change is audited. Customers see whether a capability is
  available to them, not ExaCarib's internal checks.

## Consequences

- A new country or channel ships dark and is switched on without a deploy.
- The criteria live next to the code that needs them, so they are reviewed with it.
