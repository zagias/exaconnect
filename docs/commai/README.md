# CommAI

ExaCarib CommAI is the AI communications and customer-service platform
scoped in "ExaCarib CommAI architecture and scope" (5 October 2026). Phases
0, 1 and 2 of its build order are in this repository, inside the Connect
controller and portal (ADR 0016).

| Area | Code | Decision record | Operator note |
| --- | --- | --- | --- |
| Inbox, notes, jobs, events, webhooks, idempotency, actions | `controller/exaconnect_controller/commai/` | 0016 | this file |
| Sign-in gateway, cookies, two-step, SSO, SCIM, backups | `controller/exaconnect_controller/identity/`, `deploy/backup/`, `deploy/keycloak/` | 0017 | identity.md |
| Website chat, WhatsApp, SMS, email | `commai/channels/`, `widget/` | 0018 | channels.md |
| AI runtime, knowledge, copilot, memory, browser calls | `commai/ai/` | 0019 | ai.md |
| Integrations, workflows, onboarding, assistant, reports | `commai/automation/`, `commai/connectors/` | 0020 | automation.md |
| Voice phone system, provisioning, billing | `commai/voice/`, `deploy/freeswitch/` | 0021 | voice.md |
| Organisations, calendars, roles, security settings, data governance, abuse protection | `commai/enterprise/` | 0024 | enterprise.md |

API: `/api/v1/commai/customers/{customer_id}/...` (OpenAPI at
`/api/v1/docs`), SCIM at `/api/v1/scim/v2`. Python SDK:
`ExaConnect(...).commai()`. Portal: the CommAI group in the sidebar.

## Acceptance tests (plan, phase 1)

Each runs in the controller suite on every change.

| # | Test | Where |
| --- | --- | --- |
| 1 | Website or WhatsApp enquiry reaches the inbox, is assigned, reply on its own channel | test_commai_channels, test_commai_core |
| 2 | Notes unreachable via widget, channels, customer keys, exports, customer AI | test_commai_core, test_commai_channels, test_commai_ai |
| 3 | A human taking over stops AI replies and keeps context | test_commai_core, test_commai_ai |
| 4 | Booking confirmed only after the calendar reports success | test_commai_core, test_commai_ai, test_commai_automation |
| 5 | Repeats and retries create no duplicate messages or bookings | test_commai_core, test_commai_channels |
| 6 | A broken integration shows status, evidence and a safe recovery | test_commai_automation |
| 7 | One tenant's key cannot read another's data or notes | test_commai_core |
| 8 | When the AI fails, the conversation goes to a person | test_commai_ai |
| 9 | Free-form WhatsApp outside 24 hours, and unapproved sensitive actions, are blocked | test_commai_channels, test_commai_core |
| 10 | Reports match recorded events exactly | test_commai_automation_reports |

## What waits on Dudley

Everything paid or account-bound is behind an interface with a working
simulated provider. To go live:

- WhatsApp and SMS: choose Twilio or 360dialog, open the account, Meta
  business verification, a test number (cost).
- Google and Microsoft sign-in, Google Calendar, HubSpot: create the free
  OAuth apps and put their ids in the server `.env`.
- Server-side speech on DeepInfra (cost); browser calls work today with the
  browser's own speech.
- A SIP provider for real numbers and calls (phase 3).
- A price list for messaging, voice and AI (example prices are shown).
- An off-site backup target for `deploy/backup/backup.sh`.
