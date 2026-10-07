# Jibsy phase 3 channels, countries and SMS carriers: operator note

Decision record: `docs/adr/0029-commai-channels-global.md`.

## What runs where

- `controller/exaconnect_controller/commai/channels/social.py`: Messenger, Instagram, Telegram (channels, providers, webhooks, opt-out, diagnostics).
- `.../channels/whatsapp_cloud.py`: WhatsApp through Meta's Cloud API, interactive messages, click-to-chat links.
- `.../channels/countries.py`: the country matrix and the SMS rules the gateway enforces.
- `.../channels/sms_routing.py`: SMS carriers, route table, failover; replaces the SMS channel.
- `.../commai/api/channels_global.py`: the API. `.../commai/sql/60_channels_global.sql`: the tables.
- Portal: Jibsy → Channels (Messenger, Instagram, Telegram tabs; Cloud API and Click to WhatsApp on the WhatsApp tab) and Jibsy → Countries.

## Switching things on

Everything starts off. An ExaCarib admin records each criterion with evidence and sets the status through
`/api/v1/commai/golive` (ADR 0028): `channel/messenger`, `channel/instagram`, `channel/telegram`,
`channel/whatsapp-cloud`, `carrier/sms-sim-a`, `carrier/sms-sim-b`, `carrier/sms-twilio`, `country/<ISO>`.

## Endpoints

Signed in, under `/api/v1/commai/customers/{customer_id}`:

| Path | What |
| --- | --- |
| `GET/POST /social-accounts`, `PATCH/DELETE /social-accounts/{id}` | Messenger Pages, Instagram accounts, Telegram bots |
| `PUT /social-accounts/{id}/token` | Secure entry of a bot or Page token (never returned) |
| `POST /social-accounts/{id}/connect` | setWebhook (Telegram) or Page subscription (Meta) |
| `POST /social-accounts/{id}/simulate-inbound`, `/simulate-receipt` | Test traffic in the platform's own format |
| `GET /whatsapp-cloud`, `POST /whatsapp-cloud/signup` | Cloud API status and Embedded Signup (or simulated) |
| `POST /whatsapp-cloud/{id}/templates/{tid}/submit`, `/templates/sync` | Submit templates to Meta; pull their status |
| `POST /whatsapp-cloud/{id}/media`, `GET /whatsapp-cloud/{id}/media/{media_id}` | Media upload and download |
| `POST /whatsapp-cloud/{id}/simulate-inbound`, `/simulate-status`, `/simulate-template-status` | Test traffic |
| `POST /conversations/{id}/interactive` | WhatsApp reply buttons or a list |
| `GET /whatsapp-link?account_id=&text=` | Click-to-WhatsApp link and QR code (SVG) |
| `GET /countries` | The matrix as the business sees it |
| `GET/POST /sms-senders`, `GET /sms-route-attempts` | Sender registrations; carrier hand-offs |

ExaCarib admins, under `/api/v1/commai`: `GET /sms-routes`, `PUT/DELETE /sms-routes/{country}`,
`GET /sms-carriers`, `PUT /sms-carriers/{key}/fault` (simulators only), `PUT /countries/{code}/sms-rules`,
`GET /sms-senders`, `PUT /sms-senders/{id}`.

Public webhooks: `/channels/meta/hooks/{token}` (GET handshake, POST), `/channels/meta/webhook` (Meta's
app-wide webhook), `/channels/telegram/hooks/{token}`, `/channels/whatsapp-cloud/hooks/{token}`.

## Environment

All optional; the simulated providers work with none of them.

- `EXA_SECRETS_KEY`: the vault key (needed to save any token).
- `EXA_META_APP_ID`, `EXA_META_APP_SECRET`, `EXA_META_ES_CONFIG_ID`, `EXA_META_VERIFY_TOKEN`: ExaCarib's Meta app. **Not live.**
- `EXA_TWILIO_ACCOUNT_SID`, `EXA_TWILIO_AUTH_TOKEN`: Twilio as an SMS carrier. **Not live.**

## Demonstrating failover

1. Switch on `country/TT`, `carrier/sms-sim-a` and `carrier/sms-sim-b`.
2. `PUT /sms-routes/TT {"primary_carrier": "sms-sim-a", "fallback_carrier": "sms-sim-b"}`.
3. `PUT /sms-carriers/sms-sim-a/fault {"mode": "refuse"}` and reply to an SMS conversation: it leaves
   through B, once. Try `timeout_after_send`: the message waits and is retried on A only, and still goes once.

## Testing

```sh
cd controller
EXA_TEST_DATABASE_URL=postgresql://... python -m pytest -q tests/test_commai_social.py tests/test_commai_countries.py tests/test_commai_whatsapp_cloud.py
```
