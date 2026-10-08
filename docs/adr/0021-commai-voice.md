# ADR 0021: CommAI voice, the phone system, provisioning and billing

Date: 2026-10-06. Status: accepted.

## Context

The CommAI plan's voice stages 2 to 4 ("adds, moves and changes, and
self-service", "provisioning", "billing") let a business run its own phone
system with no ticket to ExaCarib, turn an approved order into a working
service, and bill every call on ExaCarib's own prices. Stage 1 (the AI on
browser calls) belongs to the AI runtime (ADR 0019). Stage 5 (real numbers,
porting go-live, emergency addresses per island) is phase 3.

Dudley has not opened a SIP provider account and has not given a price list.
Nothing here may depend on either.

## Decision

**One change path.** Every change, from a form, a spreadsheet, a scheduled
job, a sentence or an order, is a list of operations applied by
`commai/voice/config.apply()`. Each operation runs in its own savepoint, so a
bulk change reports every bad row at once and nothing applies unless all pass.
The provider is called only after every check has passed, with idempotency
keys derived from the version number, so a retried save never orders a second
number.

**Price before saving, spend permission to save.** The price impact (monthly
change and one-time fees, from the business's rate card) is worked out from
the operations. If the bill changes, the caller must hold spend permission and
must send back the exact price they were shown (`accepted_price`); a different
price is refused with 409. Spend permission is a business setting
(`voice_permissions`: voice admin, spend). Until a business names anyone,
every account of that business counts as a voice admin with spend permission,
so nobody is locked out; naming the first person ends that. ExaCarib admins
can do everything. Everyone else is staff.

**Versions and roll back.** Each saved change is a numbered version with a
full snapshot. Rolling back restores routing only (ring groups, queues,
business hours, menus, AI rules and where each number rings). People, numbers
and devices are not brought back or removed by a roll back, because they
change the bill and the provider; they go through their own change. Changes
can be set for a time: they are checked when scheduled (and the price
approved then) and checked again when the job runs.

**Moves carry the emergency address.** A person's emergency address is their
site's address. Moving them updates it, and that of the numbers that ring
them, and marks the site's registration pending. Registering addresses with a
provider, and the rules for each island, are phase 3; the fields are there.

**Self-service is "own" by construction.** `/voice/me` acts on the signed-in
account's extension; the request cannot name another. Staff change only
forwarding (optionally until a time; a job ends it then), do not disturb,
voicemail greeting and voicemail to email, and see their own calls.
Recordings follow the company policy (`none`, `own`, `admins`). Forwarding to
a blocked premium number is refused.

**"Say what you want" is a deterministic parser.** `selfservice.parse()`
understands forwarding (to my mobile, a number or an extension, until a time
or for a while), do not disturb, voicemail greeting and voicemail to email,
and, for voice admins, moving or removing a person. It returns the exact
change, stored as a proposal for 15 minutes, applied only when the same
person confirms it, through the same checks as the screens. No model is used;
one can be added in front later without changing the confirm step.

**PBX configuration is rendered, not edited.** `freeswitch.render()` turns the
database into FreeSWITCH XML (directory with a1-hashes only, a dial plan per
business, inbound numbers, IVR menus, mod_callcenter queues). It is a pure
function: the same data gives the same bytes. A job renders after every
change and writes to `EXA_FREESWITCH_DIR`; the `freeswitch` service (compose
profile `voice`) includes those files. Emergency numbers come first and are
never blocked; blocked premium prefixes are refused in the dial plan too. The
AI agent is a transfer with a fallback (voicemail, a ring group or a queue),
so basic calling never depends on the AI services.

**Provisioning runs as steps.** An approved order runs tenant → numbers →
devices → confirm → billing through the job queue. Each step runs in a
savepoint, records its own failure, and is idempotent (records carry a unique
`order_ref`; provider calls carry keys derived from the order). The job
retries with back-off, and Retry starts it again by hand; neither creates
duplicates. Confirm places a test call to every new number; the order is
active only when they all work. Billing starts at activation and stops when a
user or number is removed. Port orders have their own status and switch-over
date, and a ported number goes live only after its own test call.

**Billing on ExaCarib's prices.** Rate cards are per business and versioned;
a new version never changes an old one, and each charge keeps the version it
was priced with (the one in force when the call ended). Calls are rated when
the record arrives (once per call id), with per-second minutes, pooled
bundles (alerts at the business's level and when used up, as events) and
fraud limits that stop calls (daily spend cap, blocked prefixes,
international off) and alert on unusual volume. Monthly fees are pro rata by
day. Invoices go draft → issued; a database trigger refuses any change to an
issued invoice or its lines; corrections are credit notes whose lines point
at the lines they correct. Every invoice line traces to its charge and call.
The supplier side imports the provider's CSV (duplicates ignored) and
reconciles it with what was billed, with the margin. Money is `numeric` in
Postgres and `Decimal` in Python; floats are refused. Call minutes are also
recorded as the `voice_minute` usage meter (ADR 0016).

## What is simulated, and what waits on Dudley

- **SIP provider**: `SimulatedProvider` gives numbers from the fictional
  range +1 868 555 0100 to 0199, port orders that move only when an ExaCarib
  admin moves them, and test calls that succeed unless a fault is injected.
  `HttpSipProvider` reads `EXA_SIP_PROVIDER_URL` and `EXA_SIP_PROVIDER_KEY`
  and refuses until a provider is chosen and its adapter written (phase 3).
- **FreeSWITCH**: the configuration is rendered and tested as text; it has not
  been loaded into a running FreeSWITCH here. Telling FreeSWITCH to reload
  (`reloadxml` over the event socket) and posting call records from
  FreeSWITCH (`mod_json_cdr`) are wired with the real provider. The gateway
  file is an example (`deploy/freeswitch/sip_profiles/external/`).
- **Prices**: the example rate card is labelled "Example" in the API and the
  portal. ExaCarib replaces it with a new version when Dudley sets prices.
- **Single bill**: voice invoices are their own documents for now. Joining
  them to one ExaCarib bill waits on billing integration, which is out of
  scope.
- **Desk phone files** use generic (Yealink-style) keys; per-model templates
  come with the first real phones.

## Consequences

The phone system, orders and billing can be used and demonstrated end to end
without a provider account or a price list, and switching to real ones means
an adapter and a new rate card version, not a change to the flow. Roll back
is deliberately narrow; people and numbers are undone with an ordinary change
so the bill and the provider stay right.
