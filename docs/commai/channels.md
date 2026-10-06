# CommAI channels: operator note

Website chat, WhatsApp, SMS and email, all landing in the shared inbox.
Decision record: `docs/adr/0018-commai-channels.md`.

## What runs where

- `controller/exaconnect_controller/commai/channels/`: `messaging.py`
  (WhatsApp and SMS channels, inbound, opt-out, country limits),
  `providers.py` (simulated, Twilio, 360dialog), `email.py`, `widget.py`
  (keys, sessions, signed-in tokens, hours, files), `checks.py` (diagnostics).
- `controller/exaconnect_controller/commai/api/channels.py`: the API.
- `controller/exaconnect_controller/commai/sql/20_channels.sql`: the tables.
- `widget/src/widget.ts`: the website chat script. Build it with
  `portal/node_modules/.bin/tsc -p widget`; the output,
  `controller/exaconnect_controller/commai/static/widget.js`, is committed.
- Portal: CommAI → Channels (`portal/src/pages/commai/Channels.tsx`).

## Endpoints

Signed in, under `/api/v1/commai/customers/{customer_id}`:

| Path | What |
| --- | --- |
| `GET/POST /widget-keys`, `PATCH /widget-keys/{id}` | Website chat keys, allowed origins, look, hours; installs (checker) |
| `POST /widget-keys/{id}/rotate`, `/test-conversation` | New secret (shown once); a test conversation |
| `GET/POST /channel-accounts`, `PATCH/DELETE /channel-accounts/{id}` | WhatsApp numbers, SMS numbers, email addresses |
| `POST /channel-accounts/{id}/simulate-inbound`, `/simulate-receipt` | Test traffic through the real webhook path |
| `GET/POST /whatsapp-templates`, `POST /whatsapp-templates/{id}/review` | Templates and their approval |
| `POST /conversations/{id}/template` | Send an approved template with its values |
| `GET /channels/outbox` | What the simulated providers "sent" |
| `GET /channels/diagnostics` | Why a channel may have stopped sending |
| `GET /files/{id}` | A file a visitor attached |

Public (no sign-in), under `/api/v1/commai`:

| Path | What |
| --- | --- |
| `GET /widget/v1.js` | The script |
| `/widget/{key}/config, session, conversations, messages, files, contact, offline, callback, heartbeat` | The visitor API (Origin must be allowed; session in `X-Widget-Session`) |
| `POST /channels/hooks/{token}` | Provider webhooks and inbound email (signature or shared secret checked) |
| `GET /channels/hooks/{token}` | Meta-style `hub.challenge` check |
| `GET/POST /channels/email/{token}/unsubscribe/{sig}` | One-click unsubscribe |

## Install website chat

```html
<script src="https://HOST/api/v1/commai/widget/v1.js" data-key="wk_..." async></script>
```

For signed-in customers, the business's server signs an HS256 JWT with the
widget secret (`sub`, `email`, `name`, `exp` within 24 hours) and adds
`data-user-token="..."` (or calls `ExaCaribChat.identify(token)`).

## Environment

All optional; everything works with the simulated providers while empty.

- `EXA_PUBLIC_URL`: the public address (Twilio signatures, unsubscribe links, webhook URLs).
- `EXA_TWILIO_ACCOUNT_SID`, `EXA_TWILIO_AUTH_TOKEN`: Twilio. **Not live** until set.
- `EXA_360DIALOG_API_KEY`, `EXA_360DIALOG_WEBHOOK_SECRET`: 360dialog. **Not live** until set.
- `EXA_SMTP_HOST`, `EXA_SMTP_PORT`, `EXA_SMTP_USER`, `EXA_SMTP_PASSWORD`, `EXA_SMTP_STARTTLS`: outbound email.

An account can use a second set of credentials with the setting
`credentials_env` (for example `EXA_TWILIO_ACME`, reading
`EXA_TWILIO_ACME_ACCOUNT_SID`); the prefix must start with the provider's own.

## Going live with a real WhatsApp provider (waits on Dudley)

1. Choose Twilio or 360dialog and create the account.
2. The business creates its Meta Business account and completes business verification.
3. Connect the number through the provider's Embedded Signup.
4. Set the provider's credentials in the controller's environment and restart.
5. Add the number on the WhatsApp tab with that provider, point the provider's
   webhook at the URL shown, then set it live.
6. Submit templates in the provider's console; record each approval on the
   Templates card (ExaCarib admins only once a real provider is in use).

## Testing

```sh
cd controller
EXA_TEST_DATABASE_URL=postgresql://exa@127.0.0.1:5432/exatest_channels python -m pytest -q tests/test_commai_channels.py
```

By hand: on the WhatsApp tab add a simulated number, use "Send a test message
in", reply from the inbox, and see the reply under "Sent by the simulator".
