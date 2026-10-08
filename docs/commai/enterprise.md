# Enterprise administration and data governance

Operator note for ADR 0030. Code: `controller/exaconnect_controller/commai/enterprise/`,
API `commai/api/enterprise.py`, SQL `commai/sql/61_enterprise.sql`, portal
Jibsy > Settings > Organisation, Roles, Security and Data.

## API (under `/api/v1/commai/customers/{id}`)

| Area | Endpoints |
| --- | --- |
| Organisation | `GET /organisation`; `POST/PUT/DELETE /organisation/locations[/{id}]`; `PUT /organisation/locations/{id}/hours`; `POST/DELETE /organisation/brands`, `/holidays`, `/closures`; `PUT /organisation/teams/{team}`; `GET /organisation/due?minutes=` |
| Roles | `GET /roles`; `GET /roles/me`; `POST/PUT/DELETE /roles[/{id}]`; `POST/DELETE /roles/assignments[/{id}]` |
| Security | `GET/PUT /security`; `GET /security/audit?kind=signin\|failed\|settings`; `GET /security/alerts`; `POST /security/alerts/{id}/acknowledge`; `POST /security/watch`; `GET /security/keys`; `POST /security/keys/{id}/unlock` |
| Data | `GET /data`; `PUT /data/retention`; `POST /data/retention/run`; `PUT /data/contacts/{c}/hold`; `GET /data/contacts/{c}/export?format=json\|zip`; `POST /data/contacts/{c}/erase`; `POST/GET /data/exports`; `GET /data/exports/{e}/download`; `GET /data/processing` |
| ExaCarib | `GET /api/v1/commai/enterprise/alerts`; `POST /api/v1/commai/enterprise/watch` |

## Running it

- Durable jobs: `enterprise.tick` (every 5 minutes: unusual-use checks for
  every business, queues each business's daily `enterprise.retention`, drops
  expired exports), `enterprise.retention`, `enterprise.export`. The first
  tick is queued by the first authenticated request once the background
  workers run.
- Settings: `EXA_HOSTING_REGION` (shown on the processing page),
  `EXA_GEOIP_DB` (optional, for new-country alerts), `EXA_BACKUP_RCLONE_REMOTE`
  (shown as the off-site backup line).
- Locked out by an IP allow-list? An ExaCarib admin is not limited by it and
  can clear the list from the business's Security tab.

## Permissions per section

`roles.required` maps scope and endpoint to a permission: notes endpoints
need `notes`; conversation exports and `/data/.../export*` need `export_data`;
`/reports` needs `view_reports`; approving actions and voice orders needs
`approve_spending`; other admin endpoints by section (teams, members,
routing, settings, organisation: `manage_teams`; channels and webhooks:
`manage_channels`; integrations, workflows, AI, assistant: `manage_integrations`;
usage limits: `approve_spending`; voice: `voice_admin`; roles, security,
data: `manage_security`; anything else: `manage_security`). Other writes need
`reply`, other reads `read_inbox`.

## Simulated or waiting

- Country detection waits on a GeoIP database (MaxMind account).
- Recording deletion at the SIP provider waits on the real SIP adapter.
- SSO sign-out tested against a fake issuer, not a live Keycloak.
