# ADR 0032: CommAI inbox, contacts and API platform gaps

Date: 2026-10-07. Status: accepted.

## Context

The phase 3 gap audit listed what the shared inbox, contacts and the public
API still lacked against the CommAI scope: intent and skills routing,
language detection on inbound messages, snooze wake-ups, service-target
reminders and escalation, typing and presence, staff attachments on every
channel, a full customer history, contact identity management, email sending
limits, a hard spend limit on sending, a website visitor's call with the AI
assistant, one error shape, a shared rate limit, cursor paging, SDK coverage
and accessibility. WhatsApp template submission and Embedded Signup are left
to the channel go-live work, because they need Dudley's Meta account.

## Decision

**Routing.** A routing rule can name skills. The team is the rule's team, or
the team whose members hold every skill; the person is the least busy member
holding the skills and, when there is one, the conversation's language. If no
one speaks the language, routing falls back to skills alone. The AI agent's
intent is written to `conversations.intent` on hand-over; if a rule for that
intent names a different team, the conversation moves there. Inbound messages
with no language get one from the ADR 0019 detector before routing.

**Durable jobs, not a sweeper.** Snooze and service targets queue jobs
(`jobs.enqueue` with a dedupe key) for the exact moment: `inbox.wake` at
`snoozed_until`, `inbox.target` at the reminder point (80% of the time, by
default) and at the due time. Each job re-reads the conversation and does
nothing if things moved on (a later snooze, a reply, resolved). Each alert
goes out once (`conversations.target_alerts`). A missed target raises the
priority one step and, when a team is set, moves it to that team, with a
note and a `conversation.escalated` event. Tag and priority changes are
events too, so webhooks and workflows see them.

**Presence lives in its own table.** `commai_presence` holds who is typing
(lapsing after 6 seconds) and who is viewing (30 seconds). It is pushed on
the inbox live feed as `presence` messages and is never a recorded event, so
webhooks and exports never see it. Visitors learn only that "the team" is
typing, never who.

**Attachments by channel.** Staff upload a file to the conversation, then
send its id with the reply. Rules per channel: web, API and email take
images, PDFs and text up to 2 MB; WhatsApp PNG, JPEG, PDF and text; SMS
(MMS) PNG, JPEG and GIF up to 500 KB; voice none. Providers that need a link
(Twilio, 360dialog) get an HMAC-signed link valid for 24 hours, signed with
the channel account's hook secret and served at
`/api/v1/commai/channels/media/...`; it needs `EXA_PUBLIC_URL`.

**Spend and sending limits.** Every send on a provided channel asks
`usage.allowed` for the channel's meter, so a hard monthly limit stops
sending. Each email address has daily limits (2,000 in all, 300 to one
domain, both adjustable in `settings.email_limits`).

**Visitor AI calls.** Off by default per widget (`settings.ai_calls`), and
only when the business is AI-first with the AI agent on. Speech is
recognised and spoken in the visitor's browser; the controller sees text.
Limits: 3 calls an hour per visitor, 60 an hour per widget key, 20 turns a
minute, 15 minutes a call, and the hard limit on AI voice minutes.

**One error shape.** Every error is `{"detail": "...", "code": "...",
"request_id": "..."}`. `detail` stays a sentence (the portal and SDK already
read it); `code` is a stable word (`not_found`, `rate_limited`,
`validation_error`...); `request_id` matches the `X-Request-ID` header,
echoed when the caller sends a safe one.

**Rate limit in Postgres.** A fixed one-minute window per caller in
`api_rate_counters`, shared by every API process: 600 a minute by default
(`EXA_API_RATE_PER_MIN`), or `api_keys.rate_per_min` set by an ExaCarib
admin. A fixed window lets a caller burst to twice the budget across a
minute boundary; we accept that for one table and one upsert. Responses
carry `X-RateLimit-Limit` and `X-RateLimit-Remaining`; 429 carries
`Retry-After`. Redis was not added: it would be one more service to run for
a counter Postgres already handles at our volume.

**Paging.** List endpoints take `cursor` and `limit` and answer
`{"items", "next"}`. Without a cursor they behave as before.

## Consequences

- A business can see every alert, escalation and routing reason in the
  conversation's notes and events.
- Accessibility: the inbox announces new customer messages and typing in
  polite live regions; forms are labelled; the inbox, contacts and channel
  pages work at phone width. No automated axe check yet: the portal has no
  test runner, and adding one is a separate decision.
- Media sending through Twilio and 360dialog is written to their documented
  APIs but only exercised against the simulated provider.
