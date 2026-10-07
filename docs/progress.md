# Progress

## M0: Lab and skeleton (2026-09-30)

Done in this milestone:

* `CLAUDE.md` is the brief, verbatim.
* Repository layout from CLAUDE.md §6: `agent/`, `controller/`, `portal/`,
  `lab/`, `deploy/`, `docs/`, CI.
* **Lab**: containerlab topology with site-a, site-b, pop-miami, carrier-a,
  carrier-b, sat and a LAN traffic-generator node per site and the PoP;
  netem profiles; fault scripts (`brownout`, `latency-creep`, `cut`,
  `restore`, `storm`); smoke test; lab host setup script and runbook.
* **Agent**: Go skeleton that builds for linux/arm64 and amd64, reports its
  version and polls the controller's health endpoint.
* **Controller**: FastAPI app with `/healthz`, `/api/v1/version` and the
  published OpenAPI schema; Dockerfile; Compose file with TimescaleDB.
* **Portal**: ExaCarib shell built to the brand brief (navy 64px top bar with
  the reversed wordmark, Storm Mode switch placeholder, grey page and white
  cards, light and dark themes, phone layout). Overview with labelled example
  data; the other five screens from §4.6 are placeholders. See docs/brand.md.
* **CI**: Go vet, test and arm64 + amd64 builds; ruff and pytest; portal build;
  shellcheck and topology parse.
* ADRs 0001 (stack) and 0002 (netem semantics).

What runs, verified in the cloud session:

* `make test`: Go tests, 3 controller tests, portal type check all pass.
* `make build`: arm64 and amd64 agent binaries, portal bundle.
* `make lint`: gofmt, ruff, shellcheck clean.
* Fault scripts dry-run against a stub `docker`, producing the expected `tc`
  commands.

Verified on the lab host (2026-10-01, Dudley's VPS: Ubuntu 24.04, 4 vCPU,
8 GB):

* `make lab-up`: all six smoke-test paths ok. Measured average RTT against
  the profile: carrier-a 31/30 ms (25), carrier-b 43/41 ms (35), sat 48/55 ms
  (45 LEO), site-a/site-b. The extra 5 to 10 ms is container and host
  overhead plus netem jitter; M3 probes will measure it, and the profiles
  can be trimmed if the demo needs exact figures.
* `make controller-up`: controller and TimescaleDB up, `/healthz` ok, port
  bound to loopback only.

M0 is done.

Change on 2026-09-30: the lab host is a cloud Linux VM Dudley sets up, not a
Parallels VM (docs/lab.md §1 has the spec). The controller port is bound to
loopback so it is never exposed on a public IP.

## M1 to M3: tunnels, controller and agent, probing (2026-10-01)

Built together, because M2's desired state is what builds M1's tunnels and M3's
probes run inside them. Nothing in the lab hard-codes WireGuard or FRR config
any more; the agents apply what the controller sends.

**M1, tunnels and routing**

* WireGuard per underlay (`wg-a`, `wg-b`, `wg-sat`), hub and spoke through
  the PoP; addressing in docs/lab.md.
* FRR eBGP over every tunnel with BFD (200 ms × 3 terrestrial, 1 s × 3
  satellite) and local-pref 200 / 150 / 50 so carrier A is preferred, then B,
  then satellite. LAN prefixes announced from each site.
* `make lab-routing` checks handshakes, BGP sessions, BFD, and pings
  lan-a ↔ lan-b and lan-a → lan-pop through the PoP.

**M2, controller and agent**

* Controller: inventory (customers, carriers, sites, links, classes, SLA
  policies), versioned desired state per node, local accounts with roles and
  sessions, audit of every write, PostgreSQL schema with TimescaleDB
  hypertables when available.
* Enrolment with a one-time token bound to the site; the agent pins the CA by
  fingerprint, sends a CSR and its WireGuard public key, and gets a client
  certificate. mTLS terminates at an nginx proxy (ADR 0003).
* Agent: polls every 10 s, applies idempotently (`wg syncconf`, `ip`,
  `frr-reload`), keeps `last-good.json`, rolls back on failure, re-applies the
  last good state at start, and keeps forwarding when the controller is gone
  (events `controller_silent` / `controller_back`).
* Contract: docs/desired-state.md.

**M3, probing and telemetry**

* TWAMP-light style UDP probes once a second per path, pinned to the tunnel
  with SO_BINDTODEVICE; the PoP agent reflects them. RTT, RFC 3550 jitter and
  loss per 10 s window.
* Telemetry every 10 s (probe windows, events, tunnel and BFD state), byte
  counters every 60 s, buffered while the controller is unreachable.
* Portal: sign-in, live Overview, Sites list, a site page with latency, jitter
  and loss charts per path against the voice SLA, WireGuard and BFD state and
  the event log, and an Admin page with each agent's applied config version.

What runs, verified in the cloud session (no Docker or WireGuard there):

* `make test`: Go unit tests including the probe sender and reflector over
  loopback, apply and rollback, FRR and WireGuard rendering, and an agent loop
  test that loses its controller and restarts from last good; 11 controller
  tests including enrolment, desired state and telemetry against PostgreSQL
  16; portal type check. `make lint` clean.
* End to end with real processes: controller + PostgreSQL + the nginx proxy
  config, `exa-agent enrol` for pop-miami and site-a (a wrong CA fingerprint
  is refused), then `exa-agent run` fetching desired state over mTLS and
  posting status and telemetry. Applying stopped at `ip link add … type
  wireguard`, as expected without WireGuard, and the failure showed up in the
  controller as `config_failed` with the error.
* Portal pages checked in a browser against that controller, with sample
  probe rows written to the database.

Verified on the lab host (2026-10-01, Dudley's VPS, run through the Remote
Control session on his Mac):

* `make controller-up`, `make demo-seed`: all three agents enrol over mTLS
  and apply their desired state.
* `make lab-routing`: WireGuard handshakes on all six tunnels, BGP 6/6 and
  BFD 6/6 on the PoP (3/3 on each site), lan-a ↔ lan-b and lan-a → lan-pop.
  Site-to-site traffic goes through the PoP over carrier A, the preferred path
  (traceroute 192.168.10.1 → 100.64.1.1 → 100.64.1.12 → 192.168.20.10).
* Controller outage: with the controller and proxy stopped, `make lab-routing`
  still passes; the agents log "controller silent; holding the last good
  state" after about 60 s and reconcile within seconds of the restart.
* Probe telemetry lands in TimescaleDB (`path_metrics`, `iface_counters` and
  `events` are hypertables). A steady 3% loss on carrier A for two minutes
  measured 3.57% at site-a and 2.86% at site-b; other paths 0 to 0.7%.

Fixed on the way:

* The agent proxy returned 502: on the management network it carries the
  alias `controller`, so nginx proxied to itself. It now reaches the API as
  `controller-api` and resolves it per request.
* BFD state was missing from telemetry: vtysh prints a warning to stderr on
  every call and the agent parsed stdout and stderr together. The agent now
  parses stdout only; the node image ships an empty `vtysh.conf`.
* The lab mounted the agent binary as a single file, so a rebuild never
  reached running nodes. It now mounts `bin/lab`; `make agents-upgrade`
  rebuilds and restarts the agents.

Findings for M4:

* At one probe a second, loss resolution is coarse: a 10 s window moves in 10%
  steps and a minute in 1.7% steps, so a 1% voice SLA can only be judged over
  minutes. The forecaster will work on 2 to 5 minute trends as the brief says;
  if that is too slow for demo step 2, the proposal is to raise the probe rate
  (the desired state already carries `interval_ms` per path).
* On this VPS, jitter reads 9 to 20 ms against 2 to 10 ms of netem jitter, and
  ping through the tunnels shows 140 to 195 ms spikes that the raw underlay
  does not (load average 3 on 4 vCPU). It looks like host noise, likely CPU
  steal; being checked. M4 scoring should use robust statistics so a single
  spike does not move a class.

Next: M4, steering and AI SLA routing.

## M4 to M7 and the AI features (2026-10-01)

Scope widened on 2026-10-01: Dudley asked for the full platform (customer and
admin sides) plus four AI features. Lab results for every push are on the
`lab-results` branch (`latest.log`), written by the lab runner on the lab
host (docs/lab-runner.md).

**M4, steering and AI SLA routing.** The agent classifies with nftables marks
and steers with `ip rule` per class (the PoP per class and site LAN), swapping
paths on BFD down in about a second without the controller. The controller's
engine scores every path per class every 10 s, forecasts 60 s ahead from a
2-minute weighted trend, moves a class before its SLA breaks, holds 120 s and
moves back after 5 good minutes, preferring paths within commit. Each
decision is stored with its inputs and a plain-English reason; shadow mode
logs without acting. Probing runs at 20 per second on terrestrial paths so a
1% loss SLA can be judged in minutes (ADR 0005). Lab run d10f35e: 28 of 30
checks, with voice moving at 1.22% measured loss, after the SLA broke,
because the engine judged carrier B too noisy to trust. Fixed in 318f8d2: a
destination needs to be within SLA and better than the current path, and the
confidence margin is only used to decide to leave.

**M5, Storm Mode.** Per site since 2026-10-01 (ADR 0004 revision): satellite
joins the steering lists of voice and business at that site, its probes go to
5 per second, the hold time drops to 60 s and the horizon rises to 120 s.
Bulk pauses rather than use satellite unless an admin allows it. Who switched
it and when is in the events and audit log; the portal shows a coral marker.

**M6, metering.** 5-minute samples from interface counters, the 95th
percentile per period (top 5% discarded, higher of in and out), commit and
burst charges, a read-only carrier view and a CSV with exactly the samples
behind each figure. Hand-worked tests cover 20 samples and a full month.

**M7, portal and demo.** Admin screens for agents, customers, sites and links
(with coordinates), enrolment tokens, classes and SLA policies, users with
one-time passwords, settings and the audit log. An account page with password
change. Sign-in throttling by account and address, with failed sign-ins and
enrolments audited. `make demo` runs the seven demo steps (lab/demo.sh) and
`make lab-ci` runs the same checks unattended, including the controller
outage (step 7).

**AI features** (ADR 0006). Hurricane watch from NHC's active-storms feed that
suggests Storm Mode for the sites in a storm's path; a bill-shock forecast
with a locked-in floor; carrier anomaly flags against the usual for the hour;
and Ask your network, answered from the customer's own data through DeepInfra
(off until `EXA_LLM_API_KEY` is set on the host). An example hurricane near
Jamaica, labelled as example data, shows the watch without a real storm.

**Public portal.** `make public-up` serves the portal and its API at
connect.exacarib.com through Caddy with Let's Encrypt, opening only 80 and
443. Agent endpoints are not served there; the controller API and database
stay private. The lab host brings it up on every lab run.

**Traffic rules and priorities** (ADR 0007). Customers create their own
classes (queue priority, SLA, preferred path) and rules that put
applications, addresses, VLANs, websites and DSCP marks into them, per site
or everywhere. Each tunnel has a CAKE priority queue shaped under the link
speed. Agents report flows from connection tracking; the controller
recognises known applications and real-time or bulk behaviour and suggests
a class, applied with one click or automatically for known applications. A
new Traffic screen covers all three. Lab check `m9-traffic.sh`.

**Disaster watch** (ADR 0008, 2026-10-03). Earthquakes (USGS), multi-hazard
alerts (GDACS: floods, volcanoes, wildfires, cyclones outside NHC's waters)
and tsunami messages (PTWC and NTWC) near a customer's sites, every 10
minutes. Reports of one event from several feeds are merged, and each event
(and each hurricane) raises one alert per customer listing every site in
range. An alert notifies once, again only if it gets worse, and reopens
rather than repeats if an event drops out and comes back. The Insights
example button now also shows a made-up earthquake off Trinidad reported by
all three feeds, which raises one alert.

**Fixes from the lab (2026-10-03).** The agent now watches BFD on its own
goroutine and interrupts any controller request in flight when a path
changes state: with the controller hung, a request could hold local
failover for up to the 15-second client timeout (lab check m7 saw 5 to
10 s). The public proxy now really returns 404 for agent endpoints (a
bare `respond` ran after `handle /api/*`, so enrol reached the
controller). Lab checks m4 and m9 no longer assume a fresh database.

**Fabric step 2: virtual circuits** (ADR 0009, 2026-10-03). A new Fabric
screen creates circuits to a cloud's site-to-site VPN gateway (AWS, Azure,
Google, Oracle or any IKEv2 gateway) and layer 2 circuits between two
sites, with a different VLAN at each end if needed. The PoP terminates each
cloud circuit (route-based IPsec with strongSwan, BGP over the tunnel) and
acts as the customer's cloud router: sites learn the cloud's routes, and
clouds reach each other through the PoP unless the customer turns that
off. Each circuit's speed can be changed at any time and is billed by the
hour; the screen shows status, round trip, loss, traffic and charges this
month. The pre-shared key is never shown again after it is entered. The
lab gains an exchange router and two simulated cloud gateways; lab check
`m9-fabric.sh` brings both clouds up, reaches the AWS VPC from lan-a and
Azure from AWS, changes a speed, and bridges VLAN 100 at site A to VLAN 200
at site B.

Unit tests: 131 controller tests, agent tests, portal type check and build.

**Lab host timing (2026-10-04).** On the lab VPS, packets that netem
delays sometimes leave 50 to 300 ms late, while the same path without netem
stays under 2 ms. Freezing the agents, pausing the controller and keeping
CPUs out of idle made no difference, so it is the virtual machine's timer
wake-ups on shared CPUs, not ExaConnect. Measured probe jitter then reaches
voice's 30 ms SLA on every path at times, and the steering check can find
voice parked on carrier B (correctly, by its numbers). Runs pass when the
host is quiet (108 of 108 at a14f9fa). A host with dedicated CPUs would
make the lab steady; `m0-timing.sh` prints the figures on every run.

**Fabric step 3: internet breakout, NAT gateway and firewall** (ADR 0010,
2026-10-04). A new Internet screen sets, per site, whether internet
traffic goes through ExaCarib's PoP (the default), straight out of the
site's own carrier links with failover between them, or nowhere. The PoP
is each customer's NAT gateway; customers write ordered firewall rules
(applied where traffic leaves, with hit counts) and port forwards on the
PoP's public address. Only traffic from the LAN is affected, never the
agent's own. The admin forms now carry the PoP's cloud and internet
settings and each link's carrier next hop; editing a PoP no longer clears
its cloud settings, and editing a link keeps its shaping speed. Lab check
`m9-internet.sh`.


**Fabric step 4: partner directory and plain-English ordering** (ADR 0011,
2026-10-04). A new Order screen takes a request in plain English, such as
"Connect Kingston to AWS us-east-1 at 50 Mbps for 10.100.0.0/16", and
drafts an order of up to five changes: circuits to clouds or between sites,
partner connections, bandwidth and internet breakout. The AI service drafts
when it is configured, with a rules parser as fallback; the controller
checks every draft against the customer's own sites and circuits, lists any
problems, the inputs still needed and the change in monthly charge, and
applies nothing until a person confirms. Confirmation applies every change
or none, and keys are never kept in the order. A partner directory lists
the four clouds and two example service partners; admins manage it and
complete orders that wait on a service partner (Admin, Partners). Lab check
`m9-order.sh`.

Unit tests: 145 controller tests, agent tests, portal type check and build.

**Fabric step 5: resilient circuits, DDoS protection and encryption
reporting** (ADR 0012, 2026-10-04). A cloud circuit can have a second
tunnel to the cloud's second gateway address; when one tunnel fails, BGP
moves traffic to the other and the circuit stays up. The PoP's public
address is protected: a source opening too many connections is blocked for
a while, there is a SYN flood limit, and ExaCarib keeps a block list
(Admin, Protection); customers see the drop counts on the Internet screen.
A new Encryption screen shows every path and circuit tunnel with the
algorithms in use and flags weak ones. Pairs through two PoPs wait for a
second PoP; blackholing at carriers and route-server peering are left as
seams. Lab check `m9-protection.sh`.

**Fabric step 6: API keys, Python SDK and Terraform provider** (ADR 0013,
2026-10-04). People create API keys on the Account screen; a key acts as
its owner and can expire or be revoked. The Python SDK (`sdk/python`)
covers circuits, internet, traffic rules, orders, partners, encryption,
metering, decisions and Storm Mode, and is tested against the real API. The
Terraform provider (`terraform/`) manages circuits, firewall rules, port
forwards, internet breakout and traffic rules. Publishing to PyPI and the
Terraform Registry waits for ExaCarib accounts there.

**CommAI phases 0 to 2** (ADRs 0016 to 0021, 2026-10-06). The shared
inbox with private notes kept in their own table, one handler at a time,
routing and service targets; a durable Postgres job queue; signed webhooks,
idempotency keys, scoped API keys and a rate limit; actions that are
proposed, checked, approved, executed once and confirmed. Sign-in moves to a
secure cookie, with two-step sign-in, a Keycloak gateway for Google and
Microsoft, enterprise SSO, SCIM and tested database backups. Website chat,
WhatsApp, SMS and email; AI agents with knowledge, a copilot, memory,
multilingual replies and browser calls; integrations (Google Calendar,
HubSpot), workflows from plain English, AI onboarding, the platform
assistant and outcome reports; and voice stages 2 to 4. Paid providers are
simulated until Dudley chooses them; see docs/commai/README.md.

**Billing phase 1** (ADR 0022, 2026-10-07). Connect and CommAI are sold as
separate plans; an organisation's subscriptions say which it holds. Each
plan has versioned price lists (currency, tax, SLA credit table). Charges are
worked out per subscription and calendar month: Connect site fees, commit,
burst at the 95th percentile from the metering settlement, satellite data
and hourly Fabric circuits, less automatic SLA credits from the same probe
windows as the Overview; CommAI plan fee, metered usage and voice as voice
billing rated it. Draft, issued (EXA-2026-0001) and void invoices with CSV
and a printable page; carrier and partner payables and margin; Stripe and
hosted-page payment adapters and Xero / QuickBooks exports, all off or
simulated until ExaCarib supplies accounts. Portal: Billing, and Admin >
Billing.

**Agent gateway for real sites** (ADR 0024, 2026-10-07). The public
server now accepts agents on port 8443 with mutual TLS, so a box at a real
site can enrol over the internet. Enrolment tokens show a one-line install
command; `deploy/agent/install.sh` and a systemd unit install the agent.
Enrolment is rate limited, the server certificate gains the public name
when it is missing, and Admin, Agents can revoke a certificate. The lab
check `m9-gateway.sh` enrols a real agent container through the public
gateway, pulls desired state, and confirms a revoked certificate is
refused. Steps for a real site are in `docs/real-site.md`.

**Safer releases** (ADR 0025, 2026-10-07). `make release` tags the running
images, optionally backs up the database, deploys, then checks the
controller, sign-in, the public site and portal, the gateway and that
agents report again. Any failure rolls back to the previous images on its
own. `make rollback TO=<commit>` goes back by hand, and Admin, Releases
lists every release and its result. The lab runner deploys this way.

**Gap fixes** (2026-10-07). After an audit against this brief, the
architecture and the Fabric roadmap: the product is named ExaCarib Connect
throughout the API and assistants; demo organisations carry an "Example
data" label on every page; telemetry has retention (90 days for path and
circuit metrics, 400 days for counters and events); admins can rename an
organisation, remove a link or delete a site (refused while it has usage
in the current or previous billing month unless forced) and list or cancel
open enrolment tokens; the hurricane watch uses the NHC's official
forecast track and cone when published; CI builds the arm64 agent on an
arm64 runner; and the portal has unit tests (vitest).

**Connect integrations** (ADR 0026, 2026-10-07). One event catalogue and one
publish point feed standards first: CloudEvents webhooks signed per Standard
Webhooks with REST hooks for Zapier, Make and n8n, Prometheus/OpenMetrics scoped
to the API key's organisation, OTLP, syslog (RFC 5424 over UDP, TCP and TLS),
SNMPv2c traps, IPFIX from the agents, read-only RESTCONF over a YANG module,
TMF621, TMF622, TMF688 and MEF LSO Sonata. Vendor profiles for Slack, Teams,
PagerDuty, Opsgenie, ServiceNow, Jira Service Management, Datadog, Splunk,
Elastic/OpenSearch, Sentinel and Grafana Cloud, NetBox sync, and cloud on-ramp
adapters for AWS Direct Connect, Azure ExpressRoute, Google Partner Interconnect
and Megaport. Carriers post faults and maintenance on their own links, and a
maintenance window moves traffic before it starts. Everything is simulated until
live sending is on and credentials exist; see docs/integrations.md.
