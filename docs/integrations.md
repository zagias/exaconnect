# Connect integrations

How ExaCarib Connect talks to the tools organisations and carriers already run.
The design is in [ADR 0026](adr/0026-connect-integrations.md). Open standards come
first; vendor connectors are thin profiles on the same events.

**Every integration is simulated until it is live.** Live needs two things: the
controller has `EXA_INTEGRATIONS_LIVE=1`, and the integration's credentials are
saved. Until then Connect runs the same code against a stand-in that answers like
the real service, and keeps what it would have sent (with secrets replaced) in the
integration's outbox. The portal labels each one **Simulated** or **Live**.

What Dudley provides is listed under each item. Customers' own credentials (their
PagerDuty key, their Splunk token) are entered by the customer in the portal and
stored encrypted with `EXA_SECRETS_KEY`; they never go in `.env`.

## Platform settings

| Setting | Purpose | Status |
| --- | --- | --- |
| `EXA_SECRETS_KEY` | Encrypts integration secrets (shared with CommAI). Required before any secret can be saved. | Set in production |
| `EXA_INTEGRATIONS_LIVE` | `1` lets integrations with credentials send for real. | Off |
| `EXA_PUBLIC_URL` | Links back to the portal in alerts. | Set in production |
| `EXA_IANA_PEN` | ExaCarib's IANA private enterprise number for syslog, SNMP and IPFIX. 32473 (documentation) until set. | Dudley: apply at iana.org/assignments/enterprise-numbers (free) |

Admin > Integrations shows which are configured, never their values.

## The events

`GET /api/v1/integrations/catalogue` lists them; `GET /api/v1/integrations/asyncapi.json`
is the AsyncAPI 3.0 document. Each event is a CloudEvent with type
`com.exacarib.connect.<name>` and extension attributes `severity`
(info, warning, critical), `action` (trigger, resolve, notify), `dedupkey` and
`organisationid`.

| Event | Action | When |
| --- | --- | --- |
| `path.down` / `path.up` | trigger / resolve | BFD reports a path down or back |
| `sla.breach_forecast` | notify | The forecaster predicts a class will breach (Connect moves it if it can) |
| `sla.breach` | trigger or notify | A class breaches and there is no better path, or did before a move |
| `routing.moved` | resolve | A class moved (closes the SLA problem); news in shadow mode |
| `storm.on` / `storm.off` | notify | Storm Mode switched |
| `hazard.alert` / `insight.raised` | trigger / notify | Disaster watch and AI insights |
| `node.enrolled`, `node.revoked` | notify | Agent enrolled or revoked |
| `node.offline` / `node.online` | trigger / resolve | No report for 90 s, and back |
| `config.apply_failed` | notify | An agent could not apply desired state (it keeps the last good one) |
| `ddos.blocked` | notify | Protection blocked a source |
| `carrier.fault`, `carrier.maintenance`, `carrier.notice_resolved` | trigger / notify / resolve | Carrier notices |
| `maintenance.started` / `maintenance.ended` | trigger / resolve | A maintenance window opens and closes |
| `audit.recorded` | notify | Every audited change (only when asked for by name) |
| `test.ping` | notify | Send test |

Subscriptions filter by kind (`*`, `path.*`, exact names), minimum severity and
sites.

## Standards

### CloudEvents webhooks (Standard Webhooks signing)

- **API**: CloudEvents 1.0 over HTTP, structured (`application/cloudevents+json`) or
  binary (`ce-*` headers) mode. Signed per Standard Webhooks: `webhook-id`,
  `webhook-timestamp`, `webhook-signature: v1,<base64 HMAC-SHA256>` over
  `id.timestamp.body`, with the key from `whsec_…`. Retries at 10, 20, 40 s… for six
  attempts with the same `webhook-id`.
- **Verify**: any Standard Webhooks library, or `exaconnect.verify_event` in the
  Python SDK.
- **Dudley provides**: nothing. **Status**: built, simulated until live.

### REST hooks: Zapier, Make and n8n

- **API**: `POST /api/v1/hooks` `{"target_url" (or "hookUrl"), "events": [...], "platform"}`
  subscribes and returns the hook id and signing secret; `DELETE /api/v1/hooks/{id}`
  unsubscribes; `GET /api/v1/hooks/me` tests the connection; `GET
  /api/v1/hooks/sample?event=path.down` gives sample data.
- **Zapier**: in a private Zapier integration, add API key authentication (Bearer,
  test `GET /hooks/me`), and a REST Hook trigger per event with subscribe `POST
  /hooks`, unsubscribe `DELETE /hooks/{{bundle.subscribeData.id}}` and perform list
  `GET /hooks/sample?event=…`.
- **Make**: use a custom app with an instant trigger whose attach is `POST /hooks`
  (`platform: "make"`) and detach `DELETE /hooks/{{webhook.id}}`; or use a plain
  Make webhook as a Connect webhook integration.
- **n8n**: a Webhook node's production URL saved as a Connect webhook, or a custom
  node's `webhookMethods` calling `POST /hooks` and `DELETE /hooks/{id}`.
- **Dudley provides**: a Zapier developer account and a Make partner account to list
  public apps (optional; private use needs nothing). **Status**: built.

### Prometheus and OpenMetrics

- **API**: `GET /api/v1/metrics`, Prometheus text 0.0.4, or OpenMetrics 1.0 with
  `Accept: application/openmetrics-text`. Bearer API key; create one with the
  `metrics` scope, which reaches only this endpoint. A key sees its own
  organisation; a carrier's, only its own links.
- **Series**: `exacarib_path_latency_ms`, `_jitter_ms`, `_loss_percent`, `_up`,
  `exacarib_link_in_mbps`, `_out_mbps`, `_commit_mbps`, `exacarib_sla_score`,
  `exacarib_node_up`, `_last_seen_seconds`, `_config_ok`, `exacarib_storm_mode`.
- **Grafana**: add a Prometheus data source pointed at the Prometheus that scrapes
  Connect, then import [`integrations/grafana-dashboard.json`](integrations/grafana-dashboard.json).
- **Datadog**: its OpenMetrics check can scrape the same endpoint.
- **Dudley provides**: nothing. **Status**: built, live.

### OpenTelemetry (OTLP/HTTP)

- **API**: OTLP/HTTP with JSON encoding: events to `/v1/logs`, metrics to
  `/v1/metrics` every minute. Optional auth header.
- **Grafana Cloud profile**: the stack's OTLP gateway with Basic `instance:token`.
- **Customer provides**: the endpoint (and token). **Status**: built, simulated until live.

### Syslog (RFC 5424)

- **API**: RFC 5424 over UDP (RFC 5426), TCP with octet counting (RFC 6587) or TLS
  (RFC 5425, with an optional private CA). Structured data `[exacarib@<PEN> …]`.
  Events and, if subscribed, `audit.recorded`.
- **Customer provides**: collector host, port, transport. **Status**: built.

### SNMP traps (v2c)

- **API**: SNMPv2-Trap-PDU over UDP (RFC 3416). Trap OIDs under
  `1.3.6.1.4.1.<PEN>.1.0.N`; varbinds for event, severity, site, path, summary.
  Path, node, SLA breach, carrier and maintenance events only.
- **Seam**: SNMPv3 (USM) and an SNMP read agent.
- **Customer provides**: receiver host, port and community. **Status**: built.

### IPFIX flow export

- **API**: IPFIX (RFC 7011) over UDP from each site's agent: template 256 with
  protocol, destination address and port, bytes and packets each way (reverse
  elements per RFC 5103), flow count and the Connect class (enterprise element 1).
  Set as an `ipfix` integration; it reaches agents through desired state.
- **Customer provides**: a collector reachable from the sites. **Status**: built.

### RESTCONF and YANG

- **API**: RFC 8040 read-only, JSON per RFC 7951: `/api/v1/restconf/data/exacarib-connect:connect`,
  narrowed with `/site=<name>/link=<path>` or `/class=<name>`; the module at
  `/api/v1/restconf/modules/exacarib-connect.yang`
  (`controller/exaconnect_controller/integrations/yang/exacarib-connect.yang`);
  discovery at `/.well-known/host-meta`.
- **Seams**: NETCONF and gNMI on the same model; RESTCONF writes.
- **Status**: built.

### TM Forum Open APIs

- **TMF621 Trouble Ticket v4** at `/api/v1/tmf-api/troubleTicket/v4/troubleTicket`:
  carriers create faults and `MaintenanceTroubleTicket`s on their own links;
  organisations raise tickets to ExaCarib on their own links and can close them.
- **TMF622 Product Ordering v4** at `/api/v1/tmf-api/productOrderingManagement/v4/productOrder`:
  offerings `cloud-circuit`, `site-circuit`, `partner-connection`,
  `bandwidth-change`, `internet-breakout`, `cloud-onramp` with camelCase
  characteristics. Checked like any order; a valid one completes at once, otherwise
  it is `rejected` with the reasons. Pre-shared keys are never stored.
- **TMF688 Event v4**: `POST /api/v1/tmf-api/event/v4/hub` registers a listener
  (`query: eventType=path.down,…`), events arrive as TMF Event resources;
  `GET …/event` lists recent events; carriers `POST …/event` with
  `TroubleTicketCreateEvent`, `…StatusChangeEvent` or `…ResolvedEvent`.
- **TMF678**: not served; billing is outside Connect.
- **Status**: built.

### MEF LSO Sonata

- **API**: under `/api/v1/mefApi/sonata/`: productOfferingQualification v7 (instant,
  synchronous), quoteManagement v8 (instant, monthly recurring price), productOrderingManagement
  v10 (an item can reference an orderable quote item), troubleTicket v4.
- **Dudley provides**: a MEF membership if ExaCarib wants certification; carriers'
  Sonata endpoints for ordering from them (the outbound seam). **Status**: built (buyer
  side towards Connect).

## Carrier fault feed

Carriers post notices on their **own** links only, in the portal (Notices), as JSON
(`POST /api/v1/carrier/notices`), as TMF621 tickets or as TMF688 events. A notice
appears on each affected site's timeline and goes out as `carrier.*`. A planned
maintenance window with "move traffic" (the default) moves classes off the links
60 seconds before it starts, with the reason in the decision log, and they come back
after the hold time when it ends. Carriers can add webhooks of their own and receive
path and notice events for their links only, without the customer's details.

**Dudley provides**: a carrier account per carrier (Admin > Users). **Status**: built.

## Connectors

| Connector | Real API | Customer provides | Status |
| --- | --- | --- | --- |
| Slack | Incoming webhook, or Web API `chat.postMessage` (Block Kit) | Webhook URL, or bot token and channel | Built |
| Microsoft Teams | Workflows webhook with an Adaptive Card 1.4 | Workflow HTTP POST URL | Built |
| PagerDuty | Events API v2 `/v2/enqueue` trigger and resolve by `dedup_key`; news to `/v2/change/enqueue` | Integration (routing) key | Built |
| Opsgenie | Alert API: create with `alias`, close by alias | API key (EU: base URL) | Built |
| ServiceNow | Table API: create incident with `correlation_id`, work notes, resolve (state 6) | Instance, integration user and password | Built |
| Jira Service Management | Service Desk API: create request, comment, transition to resolve | Site, email, API token, desk and request type IDs | Built |
| Datadog | Events API v1 (`aggregation_key`); metrics via OpenMetrics scrape | API key and site | Built |
| Splunk | HTTP Event Collector `/services/collector/event` | HEC URL and token | Built |
| Elastic / OpenSearch | Bulk API, NDJSON `create` with the event id | Cluster URL and API key, or user and password | Built |
| Microsoft Sentinel | Logs Ingestion API via a data collection rule | Entra app (tenant, client, secret), DCE, DCR ID, stream | Built |
| Grafana Cloud | OTLP gateway | Stack endpoint, instance ID, token | Built |
| NetBox | REST API v4: sites, providers, circuits, prefixes; push or pull | URL and API token | Built |

## Cloud on-ramps

One adapter interface, ordered from `POST /api/v1/customers/{id}/onramps`, TMF622 or
Sonata (`cloud-onramp`), and refreshed every few minutes.

| Adapter | Real API | Dudley provides (environment) |
| --- | --- | --- |
| AWS Direct Connect | `AllocateHostedConnection`, `DescribeConnections`, `DeleteConnection` (SigV4) | AWS Direct Connect partner status and an interconnect: `EXA_AWS_DX_ACCESS_KEY_ID`, `EXA_AWS_DX_SECRET_ACCESS_KEY`, `EXA_AWS_DX_INTERCONNECT_ID` |
| Azure ExpressRoute | ARM: reads the customer's circuit and checks the service key | ExpressRoute partner status and an Entra app: `EXA_AZURE_TENANT_ID`, `EXA_AZURE_CLIENT_ID`, `EXA_AZURE_CLIENT_SECRET` |
| Google Partner Interconnect | Compute `interconnectAttachments` (PARTNER_PROVIDER) with the pairing key | Partner Interconnect status and a service account: `EXA_GCP_SERVICE_ACCOUNT_JSON`, `EXA_GCP_PROJECT`, `EXA_GCP_INTERCONNECT` |
| Megaport | `/v3/networkdesign/buy`, `/v2/product/{uid}`, `CANCEL_NOW` | A Megaport account and port: `EXA_MEGAPORT_CLIENT_ID`, `EXA_MEGAPORT_CLIENT_SECRET`, `EXA_MEGAPORT_PORT_UID` |

Each stays simulated until all of its settings are present and live sending is on.

## Not built (seams)

NETCONF, gNMI, SNMPv3 and an SNMP read agent, RESTCONF writes, outbound Sonata
ordering towards carriers, and TMF678 (billing).
