# CommAI inbox extras: operator note

Decision record: ADR 0032. Code: `commai/inbox_jobs.py`, `commai/presence.py`,
`commai/attachments.py`, `commai/history.py`, `commai/visitor_calls.py`,
`commai/ratelimit.py`, `exaconnect_controller/errors.py`,
`commai/api/inbox_extras.py`. Schema: `commai/sql/69_inbox_extras.sql`.

## Settings

| What | Where | Default |
| --- | --- | --- |
| Service-target reminders and escalation | `GET/PUT .../service-targets` | reminders on at 80%, escalation on, no escalation team |
| Routing skills | routing rule `skills`, member `skills` | none |
| Email sending limits | email account `settings.email_limits` | 2,000 a day, 300 a day to one domain |
| Visitor AI calls | widget key `settings.ai_calls` | off |
| API rate limit | `EXA_API_RATE_PER_MIN`; per key `PUT /api/v1/commai/rate-limits/keys/{id}` (ExaCarib admin) | 600 a minute |
| Provider webhook rate limit | `EXA_WEBHOOK_RATE_PER_MIN`, per webhook address (not per sending address: providers share a few) | 6,000 a minute |
| Media links for Twilio and 360dialog | `EXA_PUBLIC_URL` | unset: media to those providers is refused |

## API

- `GET .../contacts/{id}/history`: conversations, calls (phone and website
  AI calls), open requests, linked records in connected apps, bookings.
- `GET/POST .../contacts/{id}/identities`, `POST .../identities/{id}/verify`
  (needs `evidence`), `DELETE .../identities/{id}`. All audited, with
  `contact.identity_*` events.
- `POST .../conversations/{id}/files`, then send the ids in a reply's
  `attachments`. `GET .../attachment-rules` lists what each channel takes.
- `POST .../conversations/{id}/typing`, `GET .../presence`. The live feed
  carries `{"type": "presence"}` messages.
- Widget (public, by widget key and visitor session): `POST/GET
  /widget/{key}/typing`, `POST /widget/{key}/calls`, `.../turns`, `.../end`.
- Paging: `?cursor=<next>&limit=` on messages, notes, teams, members and
  routing rules; contacts keep `before`.
- Errors: `{"detail", "code", "request_id"}`; 429 carries `Retry-After`.

## Events

`conversation.woken`, `conversation.target_due_soon`,
`conversation.target_missed`, `conversation.escalated`,
`conversation.tags_changed`, `conversation.priority_changed`,
`contact.identity_added`, `contact.identity_verified`,
`contact.identity_removed`. Typing and presence are never events.

## Runbook

- A conversation did not wake: check the `inbox.wake` job in `jobs`
  for that conversation; a later snooze replaces it.
- A customer hits 429: look at `api_rate_counters` for the caller's bucket,
  or raise the key's own limit.
- Emails refused with "daily limit": raise `email_limits` on the address,
  or wait until midnight UTC.
