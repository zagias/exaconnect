# CommAI modules, bill and automation extras: operator note

Decision record: `docs/adr/0033-commai-modules-bill-and-automation-extras.md`.
Tables: `controller/exaconnect_controller/commai/sql/70_automation_extras.sql`.

## What runs where

- `commai/entitlements.py`, `api/entitlements.py`: modules per business.
- `commai/bill.py`, `api/bill.py`: rate cards, rated charges, the single bill,
  credit notes, budgets. `commai/usage.py`: limits and budgets.
- `commai/ai_supplier.py`: AI supplier import (simulated DeepInfra) and margin.
- `commai/impact.py`, `api/approvals.py`: impact preview and approvals queue.
- `commai/automation/workflows.py`: schedules, manual starts, failure paths.
- `commai/automation/website.py`, `onboarding.py`: website by address, hours.
- `commai/voice/checks.py`, `commai/assistant_checks.py`: assistant checks.
- `commai/automation/support.py`, `api/support.py`: ExaCarib support queue.
- `commai/qr.py`, `api/qr.py`: QR codes. `api/webphone.py` and
  `deploy/freeswitch/autoload_configs/verto.conf.xml`: browser phone.
- Portal: CommAI → Approvals, Usage and bill, Support queue (ExaCarib),
  Voice → Browser phone; QR codes on Phone system; speech input on the
  voice change box and the assistant.

## Endpoints (under /api/v1/commai)

| Method and path | Who |
| --- | --- |
| GET, PUT `/customers/{id}/entitlements` | read: the business; change: ExaCarib |
| GET `/customers/{id}/approvals`, GET and POST `/actions/{run}/preview` | the business |
| POST `/customers/{id}/workflows/{wf}/trigger` | write access, reply seat |
| GET, POST `/customers/{id}/bill/rate-cards` | read; new card: ExaCarib |
| GET `/customers/{id}/bill/usage?period=YYYY-MM` | read |
| GET `/customers/{id}/bills`, `/bills/{b}`, `/bills/{b}/lines/{l}/charges` | read |
| POST `/customers/{id}/bills/draft`, `/bills/{b}/issue`, `/bills/{b}/credit` | ExaCarib |
| GET, PUT, DELETE `/customers/{id}/budgets[/{scope}]` | business admins |
| POST `/exacarib/ai-supplier/import`, GET `/customers/{id}/ai-supplier/reconcile` | ExaCarib |
| GET, PATCH `/exacarib/support/cases[/{c}]`, POST `.../replies` | ExaCarib |
| GET `/customers/{id}/support-cases/{c}/thread`, POST `.../replies` | business admins |
| POST `/customers/{id}/qr` (returns SVG) | read |
| GET `/customers/{id}/voice/webphone[?sign_in=true]` | the person, own extension |

Lists accept `cursor` (empty for the first page) and return
`{"items", "next"}`; without it they return the plain list as before.

## Settings

- `EXA_DEEPINFRA_API_KEY`, `EXA_DEEPINFRA_USAGE_URL`: the real supplier
  import. Unset: only the simulated source runs.
- `EXA_VERTO_URL` (`wss://...`): switches the browser phone on.

## Turning the browser phone on (not done yet)

1. Mount `deploy/freeswitch/autoload_configs/verto.conf.xml` in the
   `freeswitch` service and load `mod_verto`.
2. Put the TLS certificate and key at `/etc/freeswitch/tls/wss.pem` on the
   host, never in the repo.
3. Publish port 8082 behind the proxy and set `EXA_VERTO_URL`.
4. On the lab, check that a staff extension signs in (directory a1-hash) and
   that an internal call connects.

## Still simulated

- Prices: the example rate card until ExaCarib sets a price list.
- The DeepInfra usage import.
- The browser phone (no Verto endpoint yet) and calls outside the business
  (no SIP provider).
