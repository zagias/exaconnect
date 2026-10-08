# ADR 0018: CommAI channels: website chat, WhatsApp, SMS and email

Date: 2026-10-06. Status: accepted (real providers waiting on Dudley).

## Context

CommAI's inbox (ADR 0016) needs customers to reach it. The plan asks for
website chat installed with one tag, WhatsApp through the official Business
Platform, and SMS and email under the same rules: opt-out words honoured,
sending limits per country, and a 24-hour service window on WhatsApp that the
inbox, the AI and the API all respect. Repeated webhooks must never create
duplicate messages, and private notes must never leave through a channel.

No WhatsApp or SMS provider account exists yet, and Dudley has not chosen
between Twilio (wider, dearer) and 360dialog (WhatsApp-focused, cheaper). No
mail server is set up for CommAI.

## Decision

- **One gateway, rules in the channel.** WhatsApp, SMS and email are
  `channels.Channel` classes. `check_send` runs inside `inbox.send` for every
  author (person, AI, API key) and again in `deliver` when the durable send
  job runs, because a queued message may wait past a window. A refusal says
  why, in plain words, and the message is never sent.
- **Providers behind one interface**: send text, send template, verify a
  webhook signature, parse inbound messages, map receipts. Three adapters:
  - `simulated` (default): works end to end with no account. Sends are stored
    in `sim_channel_outbox`; inbound webhooks are signed with the account's
    own secret. Every screen that uses it says "Simulated".
  - `twilio` (WhatsApp and SMS): Messages API, `X-Twilio-Signature`
    (HMAC-SHA1 over the public URL and sorted form fields), `MessageStatus`
    receipts, Content SIDs for templates.
  - `360dialog` (WhatsApp): Cloud API message format, `X-Hub-Signature-256`
    (HMAC-SHA256 of the raw body).
  The two real adapters are written from the providers' public API
  documentation and tested offline (signatures and parsing only). **They are
  not live until Dudley picks the provider, creates the account and sets the
  credentials** (`EXA_TWILIO_*` or `EXA_360DIALOG_*`). Credentials come only
  from the environment; an account may name another variable prefix, but only
  one starting with its provider's own, so it can never read another service's
  secret. Whether 360dialog signs its forwarded webhooks as Meta does must be
  confirmed at set-up; until a secret is set, its webhooks are refused.
- **Accounts** (`channel_accounts`): business, channel, provider, number or
  address, status (`setup`, `live`, `paused`, `broken`), settings. Simulated
  accounts are live at once; real ones start in `setup` and can only go live
  once their credentials are on the server. Each has an unguessable webhook
  URL (`/api/v1/commai/channels/hooks/{token}`) and a secret shown once.
- **No duplicates.** Inbound messages go through `inbox.receive` with a
  namespaced external id (`whatsapp:twilio:SM...`, `email:<Message-ID>`,
  `web:<key>:<client id>`), which the database stores once. Receipts only
  touch the account's own business's messages and never move a status
  backwards.
- **WhatsApp window and templates.** Free-form text only within 24 hours of
  `conversations.last_inbound_at`. Outside it, only an approved template
  (`whatsapp_templates`: name, language, body with `{{1}}` placeholders,
  category, status), and only with exactly that template's text. With only
  simulated accounts the setup screen's Approve stands in for Meta's review;
  with a real provider only ExaCarib admins can record a status, mirroring the
  provider console. WhatsApp calling is not assumed.
- **SMS**: STOP, STOPALL, UNSUBSCRIBE, CANCEL, END and QUIT opt the number out;
  START and UNSTOP opt it back in. A daily limit per destination country
  (from the E.164 prefix, with NANP area codes told apart; 1,000 by default,
  set per account).
- **Email**: inbound by webhook with a shared secret, in a generic JSON format
  or Mailgun's forwarding format; outbound by SMTP behind a `Sender`
  interface (simulated while `EXA_SMTP_HOST` is empty). Message-IDs are
  deterministic per message and stored, so replies thread by In-Reply-To and
  References. Unsubscribing (a signed List-Unsubscribe link, or an email
  saying "unsubscribe") stops everything except replies to the person's own
  emails from the last 24 hours.
- **Website chat**: a TypeScript script (`widget/`, no dependencies, built
  into `commai/static/widget.js`) in a shadow DOM, served at
  `/api/v1/commai/widget/v1.js`. Per-website publishable keys with allowed
  origins checked on every call (CORS answered per key). Anonymous visitors
  get a fresh identity and a two-hour signed session; typing an email links
  nothing and shows no history. Customers signed in on the business's own
  site arrive with an HS256 JWT signed with the widget secret, and only they
  see their past conversations. Polling every few seconds rather than a web
  socket: simpler behind proxies, and enough for chat. Attachments up to 2 MB,
  images, PDF and plain text, checked by content. Business hours with an
  offline form (it becomes an email conversation, so the reply goes by
  email), callback requests, and the business's mode (AI first, human first,
  human only). An installation checker records the origin each widget loads
  on, allowed or not; nothing fetches the customer's site.
- **Diagnostics** (`whatsapp`, `sms`, `email`, `website_chat`) explain why a
  channel may have stopped: account status and missing credentials, the last
  provider error, refused webhooks, failed and blocked sends with their
  reasons, opted-out counts, conversations outside the window and template
  status.

## Consequences

- Everything can be demonstrated and tested today with simulated providers;
  nothing reaches a real phone or inbox until Dudley chooses and pays for a
  provider and a mail service.
- Switching the WhatsApp provider later is one account change; the rules do
  not move.
- The widget secret is stored, not hashed, because it must verify signatures;
  it is shown once and can be rotated (which ends open sessions).
- The widget has been type-checked and its API tested; it has not been run
  in a real browser in this build.
- Twilio's Messages API has no idempotency key, so a send that times out after
  Twilio accepted it could be sent twice on retry. The job only retries
  messages still `queued`; the gap is noted for when Twilio goes live.

Open for Dudley: the WhatsApp provider (Twilio or 360dialog); the Meta Business
account and business verification for the first customer; the mail service
for SMTP and inbound forwarding; the default SMS limits per country.
