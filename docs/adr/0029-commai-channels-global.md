# ADR 0029: More channels, countries and messaging carriers

Date: 2026-10-07. Status: accepted (real platforms and carriers waiting on Dudley).

## Context

CommAI phase 3 adds channels, countries and carriers, each "switched on against
written operating and security criteria" (ADR 0028). Customers in the
Caribbean also reach businesses on Facebook Messenger, Instagram and Telegram.
Sending SMS across many small markets needs per-country rules and more than one
carrier, without ever sending a message twice. Dudley also asked for WhatsApp
directly through Meta's Cloud API, interactive messages and click-to-WhatsApp
links.

No Meta app, Telegram bot, SMS carrier contract or country research sign-off
exists yet.

## Decision

- **Same channel interface as ADR 0018.** Messenger, Instagram and Telegram are
  `channels.Channel` classes on `channel_accounts`; inbound goes through
  `messaging.receive_one` (namespaced external ids, so repeats are stored once,
  and STOP words opt out); replies go through the durable send job and are
  checked again on delivery. Private notes are in another table and never reach
  a channel (tested on every new channel).
- **Gated by the go-live registry.** `channel/messenger`, `channel/instagram`,
  `channel/telegram` and `channel/whatsapp-cloud`, `carrier/sms-sim-a`,
  `carrier/sms-sim-b`, `carrier/sms-twilio`, and 25 `country/<ISO>` capabilities,
  each with its own criteria. Until a capability is on (or in pilot for the
  business), accounts can't be created or connected, webhooks are refused (403,
  logged) and sends are refused with the reason.
- **Platform rules.**
  - Messenger: free replies within 24 hours of the person's last message; after
    that only with a message tag: `HUMAN_AGENT` within 7 days, or
    `CONFIRMED_EVENT_UPDATE`, `POST_PURCHASE_UPDATE`, `ACCOUNT_UPDATE`. Instagram:
    only `HUMAN_AGENT`, 7 days. A person must write first. Tags are written
    `tag:HUMAN_AGENT` in the reply's template field, and only a person may use
    one: an AI reply carrying a tag is blocked at delivery. Meta has changed the
    tag list before, so it is a go-live criterion to recheck it.
  - Telegram: no window; private chats only; `/stop` or blocking the bot opts
    out, `/start` opts back in. The Bot API has no delivery or read receipts.
- **Signatures, as each platform defines them.** Meta (Messenger, Instagram,
  WhatsApp Cloud API): `X-Hub-Signature-256` = HMAC-SHA256 of the raw body with
  the app secret, compared in constant time, fail closed with no secret; the
  `hub.challenge` handshake. Telegram: `X-Telegram-Bot-Api-Secret-Token` equal to
  the secret we set with `setWebhook`. Real Meta traffic arrives on one app-wide
  webhook (`/channels/meta/webhook`) routed by Page, Instagram account or phone
  number id; a real Page or number can belong to one business only.
- **Simulated providers use the real formats.** `meta-simulated`,
  `telegram-simulated` and `meta-cloud-simulated` sign and parse exactly as the
  platforms do, with the account's webhook secret standing in for the app
  secret, so the verification and parsing code is what the tests exercise.
  Simulated sends are recorded once per message, even on a retry.
- **Tokens through secure entry.** Telegram bot tokens, Meta Page tokens and
  Cloud API business tokens are stored encrypted in the automation vault
  (ADR 0020, `EXA_SECRETS_KEY`), never returned, logged or audited, and deleted
  with the account.
- **WhatsApp Cloud API** (`meta-cloud`) is a third WhatsApp provider beside
  Twilio and 360dialog, under the same window, template and opt-out rules:
  Graph API sends (text, template, interactive), media upload and download,
  template create, submit and status sync (plus the
  `message_template_status_update` webhook), and Embedded Signup's server half
  (code exchange, app subscription to the WABA, phone registration). Accounts
  come only from the sign-up endpoint, where the go-live gate is. The portal's
  Meta SDK pop-up is not wired: it needs ExaCarib's Meta app first.
- **Interactive messages** (up to 3 reply buttons, or a list of up to 10 rows)
  inside the 24-hour window, validated against WhatsApp's limits, for the
  simulated provider, 360dialog and the Cloud API. Twilio needs pre-created
  Content templates for these, so a free-form one is refused with that reason.
- **Click-to-WhatsApp**: `wa.me` links with a pre-filled message and a QR code
  (SVG, generated on the server with `segno`, a small pure-Python library).
- **Countries.** A capability matrix per country (numbers, porting, SMS,
  WhatsApp, calling, emergency calling, restrictions) for 22 Caribbean and
  Atlantic markets plus US, CA and GB. Every entry is marked
  `verified: false` and shown as "Unverified research". The SMS gateway enforces
  per destination: country switched on; sender type allowed (long code,
  toll-free, short code, name); sender registration where required (US and PR
  10DLC, toll-free verification; ExaCarib records the outcome); quiet hours in
  the recipient's time zone where a rule was found (US, PR, CA), for messages
  the business starts only (a reply within 24 hours of the person's message is
  exempt; with several time zones and an unknown recipient zone, every zone
  must be inside the window); and a rate per minute. ExaCarib admins can adjust
  a country's SMS rules without a deploy.
- **SMS carriers and routes.** A route per destination country names a primary
  and a fallback carrier with a cost per segment. Each hand-off is recorded in
  `sms_route_attempts`, and the message id is the carrier's idempotency key:
  a definite refusal fails over at once; an unknown outcome (a timeout) is
  retried later on the same carrier only, never failed over; an accepted
  message is never sent again. The cost lands in usage (`sms_route_cost`).
  Countries with no route keep using the business's own SMS account provider.

## Consequences

- Everything runs and is tested today with simulated providers; nothing reaches
  a real person until Dudley sets up the Meta app (app review, Tech Provider),
  bots, carrier contracts, and the country research is checked.
- Countries now gate SMS. Existing SMS customers need their destination
  countries switched on before upgrading (a deploy note).
- `channel_accounts.channel` is no longer a fixed list; the code checks it.
- The registry's `sync` keeps database details over code details, so later
  edits to the matrix in code would not reach the registry rows; the screens
  read the matrix from code (plus admin overrides) instead.
- Twilio's Messages API has no idempotency key; an unknown outcome is retried
  on Twilio only, which could still duplicate (as noted in ADR 0018).

Open for Dudley: the Meta app and its review; whether to use Cloud API
directly or a provider; SMS carriers and their contracts; who checks the
country research; default rates per country.
