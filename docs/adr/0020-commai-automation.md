# ADR 0020: CommAI automation: integrations, workflows, onboarding, the platform assistant and reports

Date: 2026-10-06. Status: accepted.

## Context

The CommAI plan (phases 1 and 2) asks for setup "as a product, not a services
project": integrations a business connects and repairs itself, workflows
written in plain English, AI onboarding, a platform assistant that diagnoses
and fixes, and outcome reports that count recorded events. Acceptance test 6
("a broken integration shows a clear status, the evidence and a safe way to
recover") and test 10 ("usage and outcome reports match the recorded events
exactly") belong here. The foundation (ADR 0016) already had connectors,
safe actions, the durable job queue, recorded events, diagnostics and usage
meters.

Google and HubSpot apps are not registered and no money has been agreed, so
nothing here may depend on a live provider.

## Decision

### Integrations

- One path for every app: catalogue, connect, sign in, allowed actions, field
  mapping, test, approve, live, then health and repair. Statuses: draft,
  authorised, testing, live, paused, broken.
- Sign-in is OAuth 2.0 authorisation code (Google, HubSpot) with a single-use,
  10-minute state (only its hash is stored), or a HubSpot private-app token
  through secure entry. Tokens are encrypted with Fernet under
  `EXA_SECRETS_KEY` in `commai_secrets`; the API never returns them and they
  are never logged or audited. Expired access tokens are refreshed; a refused
  refresh marks the sign-in expired.
- Allowed actions keep read apart from create, update, cancel, refund and
  delete. Sensitive actions still need a person each time (ADR 0016).
- Field mapping is suggested by a heuristic (exact name, synonyms, substring),
  plus the model when a key is set; the model may only pick from the app's own
  list. Anything not confidently matched needs a person. A person saving a
  field (or "don't send") closes it. Go-live needs sign-in, at least one
  allowed action, no open mapping and a passing test.
- Test mode runs every allowed read action as a sample lookup (nothing
  written). Controlled test writes go through `actions.propose(test=True)`;
  real connectors only write in test mode to a test calendar or a HubSpot
  sandbox (`test_calendar_id`, `test_writes`), otherwise they check and show
  the request.
- Health shows status, sign-in, last success, failures in 7 days and since
  the last success, the workflows that use the app, the cause in plain words
  (expired sign-in, missing permission, bad mapping, provider error, refused
  request), redacted evidence, and repair steps. The safe steps run here:
  "check again" (a cheap read; on success a broken connection goes back to
  live) and "retry failed actions" (re-queued with their original idempotency
  keys, so nothing is booked or created twice). Steps that need a person
  (sign in again, fix the mapping, switch an action off) are pointed to.

### Connectors

- **Google Calendar** (`google_calendar`): free/busy lookup and event insert
  from the public Calendar API v3. Availability always comes from free/busy and
  opening hours (in the business's time zone), so the AI cannot invent a slot.
  The event id is derived from the action's idempotency key: a retry finds the
  event (GET or 409) and returns it instead of booking twice.
- **HubSpot** (`hubspot`): contacts (find, create), leads, deals and tickets
  from the CRM v3 API. Created objects are kept in
  `commai_connector_objects` by idempotency key. If an attempt created an
  object but crashed before recording it, the retry searches first: contacts
  by email, deals and tickets by a short reference written into their
  description. Errors map to causes (401 expired sign-in, 403 or
  MISSING_SCOPES permission, unknown property mapping, 429/5xx provider).
- Both are **not live** until Dudley registers a Google OAuth app and a
  HubSpot app (or a business enters a private-app token) and sets the
  environment variables. Both are tested only against a fake HTTP layer.

### Workflows

- Definitions are JSON: a trigger (an event type from `commai_events` plus
  conditions) and up to 30 steps: condition, action, send_message, assign,
  add_note, wait (a duration or until an event, with a time-out), approval,
  remind, escalate and collect (ask the customer and wait for the answer).
  Jumps only go forward, so a workflow cannot loop; workflows cannot start
  from `workflow.*` events or from their own messages; a conversation can
  start a workflow at most 20 times an hour.
- Every edit saves a new version; one version is live. Each workflow has a
  preview (plain words), a test mode (a dry run against a recent event or a
  sample: actions are checked, not proposed; nothing is sent), a run log of
  every step, and a pause switch (new runs stop; runs in progress hold at
  their next step and carry on when resumed).
- Engine: Postgres jobs. A single dispatcher job reads `commai_events` after a
  stored cursor, starts runs and wakes waiting runs, then re-enqueues itself
  (keyed by a tick, so two chains collapse into one), so `events.emit` needed
  no change. A missing sequence number (an event still being committed) is
  waited on for 5 seconds before it is passed. Timers are delayed jobs; every
  wait has a token so a late or repeated signal is ignored. A live run happens
  once per (workflow version, event): a unique index enforces it. Temporal
  remains the plan at scale; `workflows.Engine` (start, signal) is the seam,
  and definitions would not change.
- Workflow actions go through `actions.propose` with role `workflow`, an
  idempotency key per (run, step), and wait for `action.succeeded` or
  `action.failed`. The workflow role may only use actions a person allowed;
  publishing with "allow these actions" grants exactly the actions that
  version uses, by the publishing person. Sending to many conversations needs
  an approval step before it. Approvals are decided by a signed-in person,
  never by the workflow or an AI role.
- Plain English: with a model key the model drafts a definition, which is
  validated like anyone's; otherwise (or if it fails validation) a
  deterministic parser handles common phrasings and lists what it did not
  understand. Keys such as `approved`, `grant`, `tools`, `permissions` or
  `skip_checks` are removed and reported: the AI cannot skip business rules,
  approve its own sensitive actions or widen its own permissions. Bookings
  take the time the customer gave; the calendar checks it.
- Starter packs (appointments, ecommerce, professional services, property,
  hospitality) are definitions on the same engine, added as drafts. Their CRM
  and calendar steps bind to HubSpot or Google when those are live for that
  action, else to the example apps.

### AI onboarding

The business gives business type, hours, locations, channels, teams and
pasted website text. CommAI drafts a profile (`commai_settings.config.profile`),
teams, knowledge entries, routing rules (keywords per team) and starter
workflows. Each is a draft until a person approves it; knowledge is added
through `commai.ai.knowledge.add_source(approved=True)` when approved.
Website text is untrusted: it only becomes draft knowledge, never a setting,
and lines that read like instructions to an AI are left out and listed.

### Platform assistant

It runs the diagnostic checks (integrations, webhooks, dead jobs, usage
limits and workflows from this module; channels from ADR 0018), picks the
findings that match the question, and answers with the evidence and whether
the cause is confirmed, likely or unknown. Fixes come from a fixed list
(`assistant.register_fix`): re-check an integration, retry failed actions,
resume an integration or workflow, retry work that gave up, set a channel
account live, raise a usage limit. A fix runs only when an admin approves it;
the checks then run again to see whether it worked. When it can't fix
something, an admin opens a support case with the configuration (no secrets),
redacted errors, all findings and correlation ids (action, job, delivery and
run ids). It never asks for passwords or keys; a question that looks like it
contains one is refused and not stored. The model, when set, only rewrites
the answer from the findings.

### Reports and cost controls

Reports count rows, never estimates: event counts from `commai_events`
(conversations, messages, resolved, reopened, `booking.confirmed`,
`action.succeeded` excluding tests, `action.failed` by app and cause, workflow
runs), first-response and resolution times from conversations, missed
contacts (no reply by the target time), backlog now.

- **AI kept**: a conversation with an `ai.replied` event in the period that
  never went to a person (no `conversation.handed_over` and no
  `conversation.handler_changed` to `human` up to the end of the period).
- **AI verifiably resolved**: AI kept, with a `conversation.state_changed` to
  `resolved` in the period made by a person, by the customer, or by the AI
  with a verified action (`action.succeeded`, not a test, on that
  conversation), and not reopened afterwards in the period. A resolve by the
  system (inactivity, auto-close) does not count: a customer who leaves a chat
  is not a resolution.

Usage and cost come from `usage_records` per meter, channel and workflow
(workflow runs are metered as `workflow_run` with the workflow id). Prices are
**examples** until Dudley's price list and are labelled so on screen and in
the API. Budgets are `usage_limits`: an alert level and a hard limit that stops
the metered work.

## Consequences

- Acceptance tests 6 and 10 run in `tests/test_commai_automation.py` and
  `tests/test_commai_automation_reports.py`.
- Simulated or waiting on Dudley: Google and HubSpot OAuth apps (and
  `EXA_SECRETS_KEY`, `EXA_PUBLIC_URL` on the server); a price list; a model
  key for model-drafted workflows, mappings, onboarding and answers (all work
  without one). Support cases are stored for ExaCarib to read; there is no
  ticketing integration yet. Satisfaction is not recorded yet, so reports say
  so.
- The dispatcher runs every 2 seconds while any workflow is live and deletes
  its own finished jobs. A transaction that holds an event uncommitted for
  more than 5 seconds could have that event missed by triggers.
