# ADR 0015: Ask proposes changes; a person applies them

Date: 2026-10-05. Status: accepted.

## Context

"Ask your network" (ADR 0006) only answered questions. Dudley asked why he
could not use it to write a rule, run a task or fix a service issue. The
platform already had every one of those changes behind its API; what was
missing was a safe way for the model to propose them.

## Decision

- One call to the model returns `{"answer", "actions"}`. Actions come from a
  fixed list: traffic rules (add, delete), firewall rules (add, delete), port
  forwards (add, delete), Storm Mode per site, a class's SLA limits, shadow
  mode, applying or dismissing an application suggestion, and an order.
  Anything else is shown as "can't do yet".
- The model sees the same snapshot as before plus the customer's
  configuration (rules, firewall, forwards, open suggestions, circuits) and
  the last four turns, so "yes, do that" follows on.
- Every action is checked against the customer's own data, then the whole
  plan is tried in a savepoint that is always rolled back, so the draft shows
  the errors applying would hit.
- Nothing changes until a person presses Apply. Applying runs every action or
  none, through the same code as the screens, and keeps what is needed to
  undo each one. Undo runs them in reverse; something already removed by hand
  is left alone. Propose, apply, undo and cancel are audited.
- Changes that cost money (circuits, bandwidth, partner connections, internet
  breakout) become a draft order for the Order screen, where price and
  gateway details are confirmed as usual. Undo cancels the draft.
- Customers act for their own organisation only; carrier accounts cannot use
  it. Admin-only settings (bulk on satellite) are not in the list.
- Text in the snapshot can try to steer the model. That is why the model only
  proposes, the list is fixed and scoped to one customer, and a person
  confirms every change.

## Consequences

Insights link to Ask with the question filled in. A plan is stored in
`assistant_plans` with the model's actions as given, re-checked when shown
and when applied, so a plan drafted before someone else changed the network
is checked against the network as it is.
