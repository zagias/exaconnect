# ADR 0026: Connect integrations, standards first

Date: 2026-10-07 · Status: accepted

## Context

Large Caribbean organisations already run a NOC toolset: PagerDuty or Opsgenie,
ServiceNow or Jira, Splunk, Elastic or Sentinel, Prometheus and Grafana, and in
banks and telcos syslog and SNMP collectors. Carriers have their own fault and
change systems. Connect sees path failures, SLA forecasts, routing moves and
Storm Mode first, so it has to tell those tools, and hear from carriers, without
anyone copying screens. A connector per vendor alone would never end; most of
these tools already speak an open standard.

## Decision

**One event catalogue, one publish point.** `integrations/catalogue.py` names
every event (path.down/up, sla.breach_forecast, sla.breach, routing.moved,
storm.on/off, hazard.alert, node.enrolled/revoked/offline/online,
config.apply_failed, ddos.blocked, carrier.fault, carrier.maintenance,
maintenance.started/ended, audit.recorded, test.ping). Each has a severity and an
action: `trigger` opens a problem, `resolve` closes it, `notify` is news. A
problem and its recovery share a dedup key (`path:<site>:<path>`,
`sla:<site>:<class>`), so every alerting and ITSM tool can close what it opened.

Database triggers on `events`, `decisions` and `audit_log` queue a publish job
only when some enabled integration could want it, so the hot paths pay nothing
when nobody subscribes. The job writes `connect_events` and fans out one
`connect_deliveries` row and one `integrations.deliver` job per matching
subscription (kind patterns, minimum severity, sites). Deliveries reuse the
CommAI job queue: a 5xx, 408, 425, 429 or time-out retries at 10, 20, 40 s… for six
attempts; any other 4xx stops at once. Every attempt is in the delivery log, with
secrets replaced by `[secret]`.

**Standards before vendors.** The wire formats are open standards, and vendor
connectors are thin profiles over the same event:

- CloudEvents 1.0 webhooks (structured or binary), signed per Standard Webhooks,
  with REST-hook subscribe and unsubscribe for Zapier, Make and n8n, and an
  AsyncAPI 3.0 document of the catalogue.
- Prometheus/OpenMetrics scraped with an API key; the key sees its own
  organisation (a carrier key, its own links). A `metrics` key scope reaches only
  that endpoint.
- OTLP/HTTP (JSON) logs and metrics; syslog RFC 5424 over UDP, TCP (octet
  counting) and TLS; SNMPv2c traps (hand-written BER, no new dependency); IPFIX
  from each site's agent, configured through desired state.
- RESTCONF (RFC 8040, RFC 7951 JSON) read-only over the `exacarib-connect` YANG
  module in the repo.
- TM Forum TMF621 (trouble tickets), TMF622 (product orders) and TMF688 (event hub
  and inbound carrier events); MEF LSO Sonata POQ, Quote, Product Order and
  Trouble Ticket. Orders map onto plain-English ordering (ADR 0011) and cloud
  on-ramps, so there is one set of checks.

**Simulated until live.** An integration sends for real only when
`EXA_INTEGRATIONS_LIVE=1` and its credentials are present. Otherwise the same
provider code runs against a simulated stand-in that answers in the provider's
shape and records the request in `connect_sim_outbox`. Tests use local fake
servers; no test touches the network. Secrets are Fernet-encrypted with
`EXA_SECRETS_KEY` (shared with CommAI), write-only in the API, and never logged.
Live URLs pass the same public-address check as CommAI webhooks.

**Carriers.** Carriers post faults and maintenance about their own links only
(native JSON, TMF621 or TMF688), and can subscribe outbound webhooks that get a
reduced view: their link and the measurement, never the customer's business. A
notice becomes one event per affected site. From 60 seconds before a maintenance
window until it ends, the routing engine treats the link as down for a logged
reason, so classes leave it through the normal decision path ("Moved voice from
Carrier A to Carrier B ahead of planned maintenance by Carrier A…") and return
after the usual hold time.

**Cloud on-ramps** (AWS Direct Connect hosted connections, Azure ExpressRoute,
Google Partner Interconnect, Megaport) share one adapter interface and are
ordered from the API, TMF622 or Sonata; ExaCarib's partner credentials come from
the environment.

**Enterprise number.** Syslog SD-IDs, SNMP OIDs and the IPFIX class element use
PEN 32473 (RFC 5612's documentation number) until ExaCarib registers one and sets
`EXA_IANA_PEN`.

## Left as seams

NETCONF and gNMI (the YANG module is the shared model; RESTCONF is the first
transport), SNMPv3 and an SNMP read agent, writes over RESTCONF, and TMF678
customer bills (billing belongs to the billing work, not Connect).

## Consequences

Adding a tool that speaks one of these standards needs no code. A new vendor
profile is one class with `deliver` and `simulate`. Going live is a setting and a
credential, not a release.
