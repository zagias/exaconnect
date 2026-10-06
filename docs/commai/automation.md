# CommAI automation: operator note

Integrations, workflows, AI onboarding, the platform assistant, and outcome
and usage reports. Decision record: `docs/adr/0020-commai-automation.md`.

## What runs where

- `controller/exaconnect_controller/commai/automation/`: `integrations.py`
  (set-up, health, repair), `oauth.py`, `vault.py` (Fernet-encrypted tokens),
  `http.py` (replaceable HTTP layer), `workflows.py` (engine, dispatcher),
  `compose.py` (plain English and starter packs), `onboarding.py`,
  `assistant.py`, `reports.py`, `llm.py`, `redact.py`.
- `commai/connectors/google_calendar.py` and `commai/connectors/hubspot.py`.
- `commai/api/automation.py`: the API. `commai/sql/40_automation.sql`: tables.
- Portal: CommAI → Integrations, Workflows, Set up, Assistant, Reports.
- Jobs: `workflow.dispatch` (one chain, every 2 s while a workflow is live)
  and `workflow.step`.

## Endpoints

Signed in, under `/api/v1/commai/customers/{customer_id}`:

| Path | What |
| --- | --- |
| `GET /integrations`, `GET /integrations/{app}/health` | Catalogue with status; health, cause, evidence, repair steps |
| `POST /integrations/{app}/connect`, `/sign-in`, `/token` | Start; OAuth sign-in URL; HubSpot private-app token (write-only) |
| `PUT /integrations/{app}/actions`, `/settings` | Allowed actions (read kept apart); test calendar or sandbox |
| `GET/PUT /integrations/{app}/mapping`, `POST .../mapping/accept` | Field mapping |
| `POST /integrations/{app}/test`, `/test-action`, `/approve` | Test mode, controlled test write, go live |
| `POST /integrations/{app}/pause`, `/resume`, `/repair`; `DELETE /integrations/{app}` | Pause, repair step, disconnect |
| `GET/POST /workflows`, `GET/PUT/DELETE /workflows/{id}` | Workflows (every edit is a new version) |
| `POST /workflows/draft`, `/validate`; `GET /workflows/packs`, `POST /workflows/packs/{pack}` | Plain English; check; starter packs |
| `POST /workflows/{id}/publish`, `/pause`, `/resume`, `/test` | Go live, pause switch, dry run |
| `GET /workflows/{id}/runs`, `/sample-events`, `/versions/{n}`; `GET /workflow-runs/{id}` | Run log |
| `GET /workflow-approvals`, `POST /workflow-runs/{id}/approve`, `/reject`, `/cancel` | Approvals |
| `POST/GET /onboarding`, `PATCH /onboarding/{id}`, `POST .../approve`, `.../reject` | Set-up drafts |
| `POST /assistant/ask`, `GET /assistant/history`, `GET /assistant/checks` | Assistant |
| `POST /assistant/fixes/{id}/apply`, `/reject` | Approve or dismiss a fix |
| `POST/GET /support-cases`, `GET /support-cases/{id}` | Support cases |
| `GET /reports/outcomes?from=&to=`, `GET /reports/usage?from=&to=` | Reports |
| `GET /usage-limits`, `PUT/DELETE /usage-limits/{meter}` | Budgets and hard limits |

Public: `GET /api/v1/commai/oauth/{app}/callback` (the OAuth redirect). It
redirects to `{EXA_PORTAL_URL}/commai/integrations/{app}?signin=ok|failed`.

Reads need `commai:read`; changes need `commai:admin` and a business admin.
Approving or rejecting a workflow step needs `commai:write` and a reply seat. Every
write is audited. Tokens are never returned or logged.

## Environment

| Variable | What |
| --- | --- |
| `EXA_SECRETS_KEY` | Fernet key for stored tokens. Without it, sign-in and token entry are refused. Make one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `EXA_PUBLIC_URL` | Controller's public address, for the OAuth redirect |
| `EXA_PORTAL_URL` | Portal address to return to after sign-in. Unset means a relative path, which suits a portal served from the same address |
| `EXA_GOOGLE_CLIENT_ID`, `EXA_GOOGLE_CLIENT_SECRET` | Google OAuth app. Google Calendar is not live until these are set |
| `EXA_HUBSPOT_CLIENT_ID`, `EXA_HUBSPOT_CLIENT_SECRET` | HubSpot app. Without them, a business can still use a private-app token |

Register these redirect URLs with the apps:
`{EXA_PUBLIC_URL}/api/v1/commai/oauth/google_calendar/callback` and
`{EXA_PUBLIC_URL}/api/v1/commai/oauth/hubspot/callback`. Google scopes:
`calendar.freebusy`, `calendar.events`. HubSpot scopes:
`crm.objects.contacts.read/write`, `crm.objects.deals.read/write`, `tickets`.

A model key (ADR 0019) is optional: without it, mapping, plain-English
workflows, onboarding and assistant answers use deterministic rules.

## Prices

`reports.EXAMPLE_PRICES` are examples, labelled so on screen and in the API,
until Dudley's price list replaces them.

## Testing

```
cd controller
EXA_TEST_DATABASE_URL=postgresql://exa@127.0.0.1:5432/exatest_automation \
  pytest tests/test_commai_automation*.py
```

Google and HubSpot are tested only against a fake HTTP layer
(`automation.http.transport`), never live.

## Adding to it

- A connector registers with the connector registry and raises
  `ConnectorError(cause=...)` with one of `expired_signin`, `permission`,
  `mapping`, `provider`, `input`.
- Another module can add an assistant fix with `assistant.register_fix`.
