# ADR 0016: CommAI foundation in the Connect controller

Date: 2026-10-06. Status: accepted.

## Context

Dudley asked to build CommAI (ExaCarib's AI communications and customer
service platform, scoped in "ExaCarib CommAI architecture and scope") up to
and including phase 2. Two open decisions in that document had no answer:
which repository, and which paid providers. The plan says CommAI reuses
Connect's portal, sign-in, roles, audit log, API keys and SDK, and Ask's
propose-then-apply pattern (ADR 0015).

## Decision

- **Same repository, same controller.** CommAI lives in
  `controller/exaconnect_controller/commai/` and `portal/src/pages/commai/`.
  It uses Connect's customers as businesses, Connect's users and roles, the
  audit log and API keys. Its tables live in `commai/sql/*.sql`, applied in
  name order after `schema.sql`. Splitting it out later means moving one
  package and its SQL files.
- **Separate tables for customer-facing messages and private notes.**
  `messages` and `commai_notes` share no code path: every customer-facing
  read (widget, channel send, export, the customer AI, webhooks, the event
  log) reads `messages` only, and events about notes carry ids, never text.
  Notes need the `commai:notes` scope; a key without it is "customer-facing"
  and can never reach them. Tests prove each path.
- **One handler at a time.** A conversation's handler is `none`, `ai` or
  `human`. The AI may only write while it is the handler, checked under a
  row lock when the reply is written, so a takeover always wins. A person
  replying takes over from the AI; replying while someone else handles it
  needs an explicit takeover.
- **Durable work in Postgres.** A `jobs` table with leases and
  `FOR UPDATE SKIP LOCKED` replaces a broker for now. Dedupe keys make
  enqueueing idempotent; handlers key their effects on stable ids, so a
  job that runs twice after a crash does nothing twice. Kafka stays the plan
  at scale.
- **Events are the record.** Every change writes a row to `commai_events`
  in the same transaction. Webhooks, live updates (a web socket) and reports
  all read from it, so they agree with each other.
- **Webhooks** are signed (HMAC-SHA256 over "timestamp.body"), timestamped,
  retried with backoff, de-duplicated by event id and logged per delivery.
  Addresses must be public HTTPS.
- **Idempotency keys and a rate limit** on every API write
  (`Idempotency-Key` header; the first response is replayed for 24 hours).
- **Scoped API keys.** `api_keys.scopes` (NULL keeps today's meaning):
  connect, commai:read, commai:write, commai:notes, commai:admin. A key
  without `connect` cannot reach the network API.
- **Seats.** An `internal` seat reads conversations and writes notes but
  never replies.
- **Actions go through one service** (`commai/actions.py`): propose, check
  (role tool list per business, the connection's switched-on actions, input
  validation), approve (sensitive actions wait for a person; the AI cannot
  approve its own), execute once from a job with an idempotency key, and
  confirm to the customer only after the external system reports success.
  On failure the conversation goes to a person with a holding reply.
- **Paid providers stay behind interfaces** with working simulated ones
  (calendar, CRM, channels, speech, SIP), as the plan and the brief's
  spend rule require. Nothing paid is switched on.

## Consequences

Each module (channels, AI, automation, voice, identity) registers its job
handlers, channels, connectors and diagnostic checks with the foundation and
owns its own SQL file, router and portal screen. Typing indicators and
presence are not in the live feed yet; the portal also polls, so a blocked
web socket only slows updates.
