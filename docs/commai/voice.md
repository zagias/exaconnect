# CommAI voice: operator note

Stage 5 (numbers by country, porting, emergency addresses per island, carriers,
fraud protection, Kamailio and LiveKit) is in `voice-global.md` and ADR 0033.

See ADR 0021 for the decisions. Code: `controller/exaconnect_controller/commai/voice/`,
API `commai/api/voice.py`, tables `commai/sql/50_voice.sql`, portal
`portal/src/pages/commai/Voice.tsx` and `voice/`.

## What it does

- **Phone system**: sites with emergency addresses, people and extensions,
  numbers, desk phones and softphones, ring groups, queues, business hours,
  menus and AI agent rules. Every change shows its price first, needs spend
  permission if the bill changes, can run now or at a set time, is versioned
  and audited; routing rolls back to any version. Bulk changes come from a CSV
  checked row by row.
- **Self-service**: each person's own forwarding, do not disturb, voicemail,
  calls and (by company policy) recordings, plus a "say what you want" box
  that shows the exact change and waits for confirmation.
- **Orders**: approved by someone with spend permission, then tenant →
  numbers → devices → confirm → billing, each step retried safely. Ports have
  their own status and switch-over date.
- **Billing**: versioned rate cards (an example card until prices are set),
  per-call rating, pooled bundles with alerts, fraud limits, invoices that
  freeze on issue, credit notes, and supplier reconciliation.

## Endpoints (under `/api/v1/commai/customers/{id}`)

| Who | Endpoints |
| --- | --- |
| Anyone in the business | `GET /voice`, `GET/PATCH /voice/me`, `GET /voice/me/calls`, `GET /voice/me/recordings`, `POST /voice/say`, `POST /voice/say/{id}/confirm`, `POST /voice/say/{id}/cancel`, `GET /voice/rate-cards` |
| Voice admins | `POST /voice/preview`, `POST /voice/changes` (with `run_at` to schedule), `POST /voice/changes/{id}/cancel`, `GET /voice/versions[/{v}]`, `POST /voice/versions/{v}/rollback`, `POST /voice/bulk`, `GET /voice/bulk/template`, `GET/PUT /voice/permissions`, `PUT /voice/policy`, orders (`GET/POST /voice/orders`, `/approve`, `/retry`, `/cancel`), `GET /voice/ports`, `POST /voice/devices/{id}/link`, `GET /voice/pbx`, `GET/PUT /voice/fraud-limits`, `GET /voice/spend`, `GET /voice/invoices[/{id}]`, calls (`POST /voice/calls`, `POST /voice/calls/authorise`, `POST /voice/calls/simulate`, `GET /voice/calls`) |
| Spend permission | saving changes that alter the bill, approving orders, granting spend permission |
| ExaCarib admins | `POST /voice/rate-cards`, `POST /voice/bundles`, `POST /voice/invoices/draft`, `/issue`, `/credit`, `POST /voice/ports/{id}`, `POST /voice/supplier/import`, `GET /voice/supplier/reconcile` |

Device endpoints, no sign-in (the per-device token is the credential):
`GET /api/v1/commai/voice/provision/{token}/{mac}.cfg` (desk phone file) and
`POST /api/v1/commai/voice/softphone/join` (redeem a sign-in link once).

Events: `voice.config_changed`, `voice.config_rolled_back`, `voice.user_moved`,
`voice.change_scheduled`, `voice.change_failed`, `voice.settings_changed`,
`voice.order_approved`, `voice.order_failed`, `voice.order_active`,
`voice.port_updated`, `voice.call_rated`, `voice.call_blocked`,
`voice.fraud_alert`, `voice.bundle_alert`, `voice.bundle_used_up`,
`voice.invoice_issued`, `voice.credit_note_issued`, `voice.rate_card_changed`.
Jobs: `voice.render`, `voice.apply_change`, `voice.provision`, `voice.settings_expire`.

## Settings and environment

- `EXA_SIP_PROVIDER`: `simulated` (default) or `sip`. `sip` is not live until
  a provider is chosen (phase 3); it reads `EXA_SIP_PROVIDER_URL` and
  `EXA_SIP_PROVIDER_KEY`.
- `EXA_FREESWITCH_DIR`: where rendered PBX files are written
  (`/data/freeswitch` in `deploy/docker-compose.yml`). Unset, they are kept in
  the database only (`voice_pbx_renders`).
- `EXA_SIP_USERNAME`, `EXA_SIP_PASSWORD`, `EXA_SIP_REALM`: for the gateway
  example in `deploy/freeswitch/sip_profiles/external/` once there is an account.
- Business settings: `commai_settings.config["voice"]["recording_access"]`
  (`own` by default, `admins`, `none`); `voice_permissions` (voice admins and
  spend permission).

## Running the PBX

`docker compose --profile voice up -d freeswitch` starts FreeSWITCH reading the
rendered files from the shared volume. Not live: with no SIP provider, outside
calls go nowhere and no SIP or RTP ports are published. Reloading FreeSWITCH
after a change (`fs_cli -x reloadxml`) is manual until the event socket is
wired. Check the image's arm64 build before first use; this has not been run here.

## Testing

```
cd controller
EXA_TEST_DATABASE_URL=postgresql://exa@127.0.0.1:5432/exatest_voice \
  python -m pytest -q tests/test_commai_voice.py tests/test_commai_voice_billing.py
```

In tests, `provider.FAULTS["order_number" | "submit_port" | "test_call"] = n`
makes the simulated provider fail the next n calls. In the portal, Billing →
Calls places simulated calls to see rating, bundles and fraud limits at work.
