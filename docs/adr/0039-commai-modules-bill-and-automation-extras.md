# ADR 0039: CommAI modules, the single bill, budgets and the automation gap fixes

Date: 2026-10-07. Status: accepted.

## Context

The CommAI gap audit found that the automation, assistant and billing pieces
of phases 1 and 2 stopped short of what a business needs day to day:

- no queue for sensitive actions waiting for a person, and no way to see what
  an action will do before approving it;
- workflows could start only on events, and a failed step could only stop the
  run or carry on;
- onboarding needed pasted text, free-text hours and no channel set-up;
- the assistant had no checks for voice, AI agents or sign-in, and support
  cases went nowhere;
- usage had quantity limits but no money budgets, and voice had its own
  invoice while messaging, AI and workflows had none;
- nothing checked AI supplier costs against what businesses were billed;
- the four parts of CommAI could not be sold separately.

Nothing may be paid for or call a real provider, and Dudley has not given a
price list.

## Decision

### Modules (entitlements)

- Messaging, voice, AI agents and automation are four modules
  (`commai/entitlements.py`). The inbox, contacts, notes, the developer
  platform, the assistant, support cases, reports and the bill belong to
  every business.
- The check is on the server, as a router dependency on each module's API
  file (`channels`, `voice`, `ai`, `automation`). It signs the person in
  first, then refuses with 403 "X is not part of this business's plan."
  A few paths inside those files are core and never blocked (assistant,
  support cases, reports, usage limits).
- No row means the module is on, so every business that existed before keeps
  everything. Only an ExaCarib admin changes a plan (`PUT
  /customers/{id}/entitlements`, audited, event `entitlement.changed`).
- Work that starts outside a route also checks: a workflow run needs
  automation, and the assistant makes phone changes only with voice.

### The single bill

- Messaging, AI and workflow usage is priced on versioned rate cards per
  business (`commai_rate_cards`), the same way voice is. Each usage record
  becomes one rated charge priced on the card in force when it was used, and
  keeps the card version. Voice keeps its own card and charges.
- One bill per business per month (`commai_bills`): a draft can be rebuilt
  any time from every charge not yet on an issued document, voice included.
  Issuing freezes it; a database trigger refuses any change. Corrections are
  credit notes: new issued documents whose negative lines point at the lines
  they correct, never more than the line.
- A voice charge lands on exactly one document: the single bill, or a
  voice-only invoice issued before the single bill existed.
- With no price list, a business gets the example card, marked "Example"
  wherever it is shown. Voice meters cannot be priced on this card.

### Budgets

- Money budgets (`commai_budgets`) for the whole bill, one channel, AI, or one
  workflow, each with an alert level (an event once, when crossed) and a hard
  limit that stops the work it covers: AI replies, outbound messages, and
  workflow runs and the messages they send.
- `usage.allowed(conn, customer_id, meter, quantity=1)` keeps its signature;
  `workflow_id` and `channel` are optional keywords. Quantity limits
  (`usage_limits`) stay as they were.

### AI supplier reconciliation

- A `UsageSource` interface imports a month of model and speech costs per
  day and model. The simulated DeepInfra source builds the supplier's view
  from CommAI's own records at example supplier prices. The real adapter
  refuses to run until `EXA_DEEPINFRA_API_KEY` and `EXA_DEEPINFRA_USAGE_URL`
  are set.
- Each business's share of a day's supplier cost follows its recorded
  quantity. The reconciliation shows billed, cost, margin, and any day where
  the supplier and CommAI counted differently.

### Approvals and impact preview

- A sensitive action gets an impact preview when it is proposed: what it will
  change, read-only checks against the app (for example, free or busy on the
  calendar, an existing contact in the CRM) run inside a savepoint, and
  whether it can be undone. A person can work it out again before approving.
- `GET /approvals` lists actions and workflow approval steps waiting, oldest
  first. Approving still uses the existing endpoints, and the person who
  proposed an action still cannot approve it.

### Workflows

- Triggers: an event (as before), a schedule (every N minutes, at least 5;
  or a time on chosen days, in the business's time zone), or by hand
  (`POST /workflows/{id}/trigger`, audited). A schedule is one durable timer
  job with a token that changes on publish, pause and resume, so a stale
  timer does nothing; after downtime it catches up once, not for every
  missed slot.
- Failures: each step can stop, carry on, or jump forward to a named step;
  steps marked "only after a failure" are skipped otherwise. A failed run
  notifies the people listed (a mention on the conversation and the
  `workflow.exception` event).

### Onboarding

- The website can be read from its address. The page is untrusted: http or
  https only, ports 80 and 443, no user name in the address, every resolved
  address must be public, the connection goes to the address that was
  checked, at most three redirects (each checked), 1 MB and 10 seconds. The
  text then goes through the same filter as pasted text.
- Opening hours are per day (`{"mon": ["09:00", "17:00"], "sun": null}`),
  given directly or read from the written hours; approving the profile makes
  them the website chat's hours.
- Each chosen channel gets a set-up draft. Approving website chat creates its
  key; WhatsApp, SMS and email record the set-up steps, because they need the
  provider and Meta first.

### Assistant and support

- New checks: voice (failed orders, PBX files out of date, calls blocked by
  the safety limits, rejected or slow number transfers, failed calls), AI
  agents (model failures, hand-over rate, knowledge gaps, tools not allowed),
  sign-in (SSO required but not on, no approved domain, failed SSO test,
  directory sync) and budgets. New fixes, applied only when an admin
  approves: retry a failed phone order, rebuild the PBX files.
- A phone change typed to the assistant becomes the same proposal as "say
  what you want" on Voice, with the same price check and confirm step.
- Support cases reach an ExaCarib queue with status, priority, assignee and
  replies; internal notes never reach the business or its webhooks.

### Smaller pieces

- QR codes are drawn on the server as SVG by a small built-in encoder (no new
  dependency, no outside service sees the link).
- The browser phone uses FreeSWITCH Verto over secure WebSocket; staff get
  only their own extension's sign-in details, on request, and it stays off
  until `EXA_VERTO_URL` is set.
- Lists that grow (workflows, runs, support cases, assistant history,
  approvals, bills) take an optional cursor; without one they answer as
  before.

## Consequences

- Prices, plans and the supplier account are Dudley's to set; until then the
  bill and the margin use example figures and say so.
- The Verto profile is in `deploy/freeswitch` but not mounted or exposed; it
  needs a certificate, the compose mount and a lab check of directory
  sign-in before use.
- Module checks sit on routers, so a new module file must be added to
  `ROUTER_MODULES`.
