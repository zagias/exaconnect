# Jibsy self-service: operator note

See ADR 0037. Code: `controller/exaconnect_controller/commai/selfservice/`
(`staff.py`, `helpcentre.py`, `enduser.py`, `branding.py`), API
`commai/api/selfservice.py`, tables `commai/sql/68_self_service.sql`, portal
`portal/src/pages/me/` (My settings) and `portal/src/pages/help/` (help centre
and its Settings tab). Tests: `controller/tests/test_commai_selfservice.py`.

## My settings (staff)

Portal: Jibsy → My settings (`/commai/me`). API under
`/api/v1/commai/customers/{id}`:

| Endpoint | What |
| --- | --- |
| `GET/PATCH /me/settings` | profile, portal language, languages answered, notifications, quiet hours, time zone, availability |
| `PUT /me/availability` | online, away, offline (only online people get new conversations) |
| `GET /me/notifications` | in-portal notices: assignments, mentions, service-target warnings |
| `GET /me/sessions`, `DELETE /me/sessions/{id}`, `POST /me/sessions/sign-out-others` | own portal sessions (not with an API key) |
| `POST /me/softphone-link` | a new sign-in link for your own softphone |

Calls use the existing `/voice/me...` endpoints.

## Help centre (the business's customers)

Switch on: Jibsy → Settings → Help centre. Publish articles there (only
approved knowledge). Public address: `<portal>/help/<slug>`.

Public API under `/api/v1/commai/help/{slug}` (writes need
`X-Requested-With: exa-help`):

- `GET ""`, `GET /articles/{id}`, `GET /search?q=`, `POST /ask`, `POST /contact`
- `POST /signin` (email a link), `POST /signin/link` (use it), `POST /signin/token`
  (the business's website token), `POST /signout`
- signed in: `GET /me`, `GET /me/conversations[/{id}]`,
  `POST /me/conversations/{id}/messages`, `GET /me/bookings`,
  `POST /me/bookings/{id}/reschedule`, `POST /me/bookings/{id}/cancel`,
  `GET /me/preferences`, `PUT /me/preferences/{identity_id}`, `PUT /me/language`,
  `GET/POST /me/data-requests`

Business admin API: `GET/PATCH /help-centre`, `PUT /help-centre/articles/{source_id}`,
`GET /help-centre/data-requests`, `POST /help-centre/data-requests/{id}`,
`GET /help-centre/data-requests/{id}/export`.

Signing customers in from the business's own website: sign the same HS256
token as for website chat (`sub`, `email`, `name`, `exp` within 24 hours,
secret of the chosen website chat key) and send them to
`<portal>/help/<slug>#user_token=<token>`.

Events: `help.asked`, `help.contact_form`, `help.signed_in`,
`help.booking_change`, `help.data_request`, `member.availability_changed`.
Job: `selfservice.booking_follow_up`.

## Simulated, and waiting on Dudley

- Sign-in emails go through the business's email account; with the simulated
  provider (or no account) they land in `sim_channel_outbox`. Real delivery
  needs the mail service from ADR 0018 (`EXA_SMTP_*`).
- `EXA_PUBLIC_URL` sets the address in sign-in emails (otherwise the request's).
- No QR code drawing in the portal yet; the softphone link is the QR text.
