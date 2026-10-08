# ExaConnect

The code behind ExaCarib Connect, ExaCarib's connectivity platform, and
Jibsy by ExaCarib, its customer conversations app ([docs/commai](docs/commai/README.md);
the code and API still call it `commai`).
Connect is a Go edge agent (WireGuard + FRR), a FastAPI
controller, AI SLA routing, customer traffic rules with priority queues and
application detection, virtual circuits to clouds and between sites
(ExaConnect Fabric, with a cloud router, elastic bandwidth and resilient
pairs), internet breakout with a NAT gateway, firewall and DDoS protection,
an encryption report, a partner directory with plain-English ordering, API
keys with a Python SDK and a Terraform provider, per-site Storm Mode, carrier
metering and settlement, AI insights (hurricane and disaster watch, bill
forecast, carrier anomalies, "Ask your network") and an ExaCarib-branded
portal for customers, carriers and admins. Organisations hold Connect, Jibsy
or both as separate plans, with shared people, sign-in and billing.
The build brief is [CLAUDE.md](CLAUDE.md); progress per milestone is in
[docs/progress.md](docs/progress.md).

| Path | What |
|---|---|
| `agent/` | Go edge agent, one static binary for linux/arm64 and linux/amd64 |
| `controller/` | FastAPI controller (API, routing, storm, metering, models) |
| `portal/` | React + TypeScript + Vite portal |
| `lab/` | containerlab topology, netem profiles, fault scripts, lab host setup |
| `sdk/python/` | Python SDK (`exaconnect` package) for the REST API |
| `terraform/` | Terraform provider for circuits, firewall rules, port forwards, internet breakout and traffic rules |
| `deploy/` | Docker Compose for the controller, database and agent gateway; `deploy/public` for the HTTPS portal; `deploy/agent` and `deploy/systemd` to install the agent at a site; `deploy/release` for releases with rollback |
| `docs/` | Lab runbook, ADRs, progress notes |

## Develop (any OS)

```bash
make test                               # Go tests, controller tests, portal type check and unit tests
make build                              # agent for linux/arm64 + amd64, portal bundle
pip install -e 'controller[dev]'        # once, for the controller tests
export EXA_TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1/exatest   # optional: DB tests (the DB is wiped)
cd portal && npm ci && npm run dev      # portal on http://localhost:5173
```

## Run the lab (Linux lab host)

The lab runs on one Ubuntu 24.04 cloud VM with root access (arm64 first, amd64
works too). Setting it up is in [docs/lab.md](docs/lab.md). Short version, on
the host:

```bash
sudo lab/host/setup-ubuntu.sh           # once: Docker, containerlab, kernel modules
make lab-up                             # build node image, deploy, apply profiles, smoke test (SAT_PROFILE=geo for GEO)
make controller-up                      # controller on 127.0.0.1:8000 (SSH tunnel); generates .env secrets
make demo-seed                          # seed sites, enrol and start the agents
make lab-routing                        # tunnels, BGP, BFD, site-to-site ping via the PoP
lab/faults/brownout.sh carrier-a 3 180  # faults: brownout, latency-creep, cut, restore, storm
make traffic                            # voice, business and bulk traffic between the sites (make traffic-stop)
make demo                               # the acceptance demo, all seven steps (DEMO_PAUSE=1 to step through)
make lab-down
```

## The demo (CLAUDE.md section 5)

`make demo` brings the lab, controller and agents up to this commit, then runs
each step and prints what to look at in the portal:

1. Start: two sites, one PoP, three paths, three classes, all green.
2. Brownout: carrier A loss ramps to 3% over 3 minutes; voice moves to
   carrier B before its 1% SLA breaks, with the forecast reason in Decisions;
   bulk stays on A.
3. Hard cut of carrier B: BFD failover within a second or two, voice loses
   under 3 seconds of packets.
4. Recovery: classes move back only after the hold time, without flapping.
5. Storm Mode at site-a: satellite warm; both terrestrial links cut; voice and
   business on satellite, bulk paused; switched off, all reversed. Site-b is
   unaffected.
6. Metering: 95th percentile, commit and burst per carrier link; the carrier
   view and its CSV match.
7. Controller outage: forwarding continues on the last map, BFD failover
   still works, and the agents reconcile when the controller is back.

Then the AI insights: an example hurricane warns site-a only, and "Ask your
network" answers a question from the decision log when an AI key is set.

## Public portal

`make public-up` serves the portal at `EXA_PUBLIC_HOST` (connect.exacarib.com,
in `deploy/public/site.env`) over HTTPS with a Let's Encrypt certificate.
Ports 80 and 443 serve the portal; port 8443 is the agent gateway, mutual TLS
only, so sites can reach the controller (ADR 0024). The controller API and
database stay private. The OpenAPI schema is at `/api/v1/openapi.json` and the
API reference at `/api/v1/docs`.

## Real sites

Admin, Enrolment tokens shows a one-line install command for a site's Linux
box; `deploy/agent/install.sh` installs the agent with a systemd unit and it
enrols through the gateway. Agents renew their certificates on their own;
Admin, Agents can revoke one. Step by step: [docs/real-site.md](docs/real-site.md).

## Releases

`make release` deploys the current commit: it tags the running images, backs
up the database when `/etc/exaconnect/backup.env` is set, then checks the
controller, sign-in, the portal, the gateway and that agents report again,
and rolls back on its own if anything fails (ADR 0025). `make rollback
TO=<commit>` goes back by hand; `make releases` and Admin, Releases list
them. The lab runner deploys this way on every new commit
([docs/lab-runner.md](docs/lab-runner.md)).

## Accounts, plans and billing

People belong to one or more organisations as owner, admin, member or viewer
(read only); they are invited from Account, People and switch organisation
from the header (ADR 0023). Sign-in supports passwords with reset links,
two-step codes, passkeys, Google and Microsoft, and company single sign-on
with SCIM. Connect and Jibsy are separate plans: an organisation sees an app
only while it holds that plan, and owners and admins choose which apps each
person may open. Both apps share one portal with an app switcher (ADR 0041).
Billing works out monthly charges per plan,
draft and issued invoices, SLA credits and partner margin (ADR 0022); online
payment and accounting exports stay off or simulated until ExaCarib adds those
accounts. API keys can be limited, for example to read-only Connect access.

## Integrations

Open standards first: signed CloudEvents webhooks (Standard Webhooks), REST
hooks for Zapier, Make and n8n, Prometheus and OpenTelemetry, syslog, SNMP
traps, IPFIX from the agents, read-only RESTCONF/YANG, TM Forum (TMF621, 622,
688) and MEF LSO Sonata. On top sit ready-made connectors: Slack, Teams,
PagerDuty, Opsgenie, ServiceNow, Jira Service Management, Datadog, Splunk,
Elastic, Sentinel, Grafana Cloud, NetBox and cloud on-ramps (AWS, Azure,
Google, Megaport). Carriers post faults and maintenance on their own links.
Each connector is simulated until `EXA_INTEGRATIONS_LIVE=1` and its account
exists. Details: [docs/integrations.md](docs/integrations.md) (ADR 0026).

## Configuration

Secrets live only in `.env` on the host (git-ignored); `.env.example` lists the
names. `lab/scripts/init-env.sh` generates the database password, proxy
secret and admin password. Optional: `EXA_LLM_API_KEY` turns on "Ask your
network" (DeepInfra by default, model `deepseek-ai/DeepSeek-V4-Flash`), and
`EXA_NHC_URL` points the hurricane watch elsewhere or, set empty, turns it off;
`EXA_USGS_URL`, `EXA_GDACS_URL` and `EXA_TSUNAMI_URLS` do the same for the
disaster watch (ADR 0008). `EXA_SMTP_HOST` and its login turn on outgoing
email (password resets, Jibsy email); without it, nothing is sent. Payment
and accounting settings are listed, off, in `.env.example`.
