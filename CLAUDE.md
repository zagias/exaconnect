# ExaConnect MVP: build brief

Owner: Dudley Guye, ExaCarib
Version: 1.0, 30 September 2026

Save this file in the project folder as `CLAUDE.md`. Claude Code reads it at the start of every session.

---

## 1. What we are building and why

ExaCarib is a neutral connectivity platform for large Caribbean organisations. We own no networks. Carriers supply capacity; ExaConnect connects, measures, steers and meters it.

The MVP proves one thing to a first customer: **your sites stay up and your critical apps stay within SLA, even when a carrier degrades or a storm takes a link out, and you can see why every decision was made.**

It is a working product on commodity Linux, not slides. Keep it small, honest and demonstrable.

### In scope

1. **Edge agent** on any Linux box (customer site or PoP): WireGuard tunnels over each WAN link, FRR for routing, per-application path steering.
2. **Controller**: sites, links, tunnels, application classes, SLA policies and desired state, served by an API.
3. **SLA probing**: latency, jitter and loss on every path, every second.
4. **AI SLA routing**: predict an SLA breach from the trend and move the affected application class before the breach, with a plain-English reason logged for every move.
5. **Storm Mode**: one switch per customer that readies the backup path and tightens thresholds.
6. **Metering**: bytes per carrier link, 5-minute samples, 95th percentile against commit, and a settlement report per carrier.
7. **Portal**: ExaCarib-branded web app showing sites, path health, SLA, decisions, Storm Mode and metering. There is also a read-only carrier view.
8. **Lab**: a reproducible network lab that emulates two carriers, a satellite path and faults.

### Out of scope for the MVP

- High-speed forwarding with VPP/DPDK. Use the Linux kernel; design so VPP can replace it later.
- Cloud on-ramps (AWS, Azure, Google), MEF LSO ordering APIs, NetBox, Temporal, Kafka. These are in the full architecture; leave clean seams for them.
- Multi-tenancy beyond a `customer_id` on every record.
- Billing integration, payments and invoicing. The settlement report is a CSV and a screen.
- Firewalling beyond what the host needs to be safe.

---

## 2. Architecture for the MVP

```
                +-----------------------------+
                |   Controller (cloud VM)     |
                |  API · policy · AI routing  |
                |  metering · portal · DB     |
                +--------------+--------------+
                     mTLS HTTPS | desired state down
                                | telemetry and events up
      +-------------------------+-------------------------+
      |                         |                         |
+-----+------+           +------+------+           +------+-----+
|  Site A    |           | PoP Miami   |           |  Site B    |
|  agent     |==WG/A====>|  agent      |<====WG/A==|  agent     |
|  FRR       |==WG/B====>|  FRR        |<====WG/B==|  FRR       |
|  WireGuard |- -WG/SAT->|  WireGuard  |<- -WG/SAT-|  WireGuard |
+------------+           +-------------+           +------------+
   Carrier A, Carrier B and Satellite are separate underlay links.
```

- Hub and spoke through the PoP for the MVP. Site-to-site traffic goes through the PoP.
- One WireGuard tunnel per underlay link per site. Each is an interface: `wg-a`, `wg-b`, `wg-sat`.
- FRR runs BGP over every tunnel for reachability, with BFD for fast failure detection.
- Steering is layered on top of BGP with Linux policy routing: nftables marks packets by application class, and `ip rule` sends each mark to a per-path routing table. The agent owns these rules; FRR owns reachability.
- The controller is the brain. Agents are thin, hold the last good config, and keep forwarding if the controller is unreachable.

---

## 3. Technology choices

| Part | Choice | Why |
| --- | --- | --- |
| Edge agent | Go 1.22+, single static binary, `linux/arm64` and `linux/amd64` | Easy to ship to any box; good for networking |
| Routing | FRR 10.x (BGP, BFD) | Open source, proven at carrier scale |
| Tunnels | WireGuard (kernel) | Fast, simple, modern crypto |
| Steering | nftables marks, `ip rule`, per-path tables | Standard Linux; no custom data plane |
| Controller API | Python 3.12, FastAPI, Pydantic | Fast to build; the AI and data libraries are there |
| Database | PostgreSQL 16 with TimescaleDB | One store for inventory, time series and metering |
| Portal | React, TypeScript, Vite | Standard, easy to hand on |
| Agent to controller | HTTPS with mutual TLS; agent polls desired state every 10 s and posts telemetry | Simple, firewall-friendly, no inbound ports on sites |
| Lab | containerlab with FRR and Linux containers, `tc netem` for faults | Reproducible in one Linux VM |
| Packaging | Docker Compose for the controller; `.deb` or plain binary plus systemd for the agent | |

The Mac is Apple Silicon. Build and test for `arm64` first, and keep `amd64` builds working in CI.

---

## 4. Components in detail

### 4.1 Edge agent (`agent/`)

Responsibilities:

- **Enrol** with a one-time token. Generate the WireGuard key pair locally; send only the public key. Receive a client certificate for mTLS.
- **Apply desired state** idempotently: WireGuard interfaces and peers, FRR config (render a template, apply with `vtysh` / `frr-reload`), nftables classes, ip rules and tables. Keep the last good state on disk and roll back if applying fails.
- **Probe** every path to the PoP once a second with a small UDP probe (TWAMP-light style: sequence number and timestamps; the PoP agent reflects it). Compute latency, jitter (RFC 3550 style) and loss per path.
- **Report** 10-second aggregates of probe results, interface byte counters every 60 seconds, and events such as BFD down/up, tunnel handshake age and config applied.
- **Steer locally when told to**: the controller sends a steering map (application class to primary and backup path). The agent swaps `ip rule` targets in under one second.
- **Fail safe**: if the controller has been silent for 60 seconds, keep the current map and fall back to local rules. If BFD reports a path down, move classes off it immediately, without waiting for the controller.

Application classes for the MVP (match on DSCP, then ports and subnets):

| Class | Example | Default SLA |
| --- | --- | --- |
| `voice` | SIP/RTP, Teams/Zoom media | latency ≤ 150 ms, jitter ≤ 30 ms, loss ≤ 1% |
| `business` | ERP, core banking, VDI | latency ≤ 250 ms, loss ≤ 2% |
| `bulk` | Backups, updates | best effort; never on satellite unless Storm Mode allows it |

### 4.2 Controller (`controller/`)

- **Inventory**: customers, sites, links (with carrier, underlay type `fibre | broadband | lte | leo | geo`, commit Mbps, cost per Mbps, burst price), tunnels, application classes, SLA policies.
- **Desired state** per agent, versioned. Every change produces a new version, and agents report the version they applied.
- **Telemetry ingestion** into TimescaleDB hypertables.
- **Decision engine** (section 4.3).
- **Metering** (section 4.5).
- **Auth**: portal users with roles `admin`, `customer`, `carrier` (read-only, own links only). Use local accounts with hashed passwords for the MVP; keep an interface for SSO later.
- **API**: REST under `/api/v1`, with the OpenAPI schema published. Every write is audited.

### 4.3 AI SLA routing (`controller/routing/`)

Start simple and explainable. The value is in acting early and showing the reason.

1. **Score each path per class** every 10 seconds from the latest window: how close latency, jitter and loss are to the class SLA (0 = breaching, 1 = comfortable).
2. **Forecast**: fit a short trend (Holt's linear method, or EWMA plus slope) to the last 2 to 5 minutes of each metric. Predict the value 60 seconds ahead.
3. **Decide**: if a class's current path is predicted to breach within 60 seconds and another path is predicted to stay within SLA, move the class. Add cost: prefer paths within commit; use burst or satellite only when the SLA needs it.
4. **Hysteresis**: a class must not flap. Hold time of 120 seconds, and move back only when the original path has scored well for 5 minutes.
5. **Explain**: store each decision with inputs and a one-line reason, such as "Moved voice from Carrier A to Carrier B: loss on A rising 0.4% a minute, forecast 1.3% in 60 s against a 1% SLA."
6. **Shadow mode**: a setting that logs decisions without acting, so a customer can build trust first.

Put the decision logic behind an interface so a trained model can replace the forecaster later. Keep a baseline "rules only" engine for comparison in tests.

### 4.4 Storm Mode (`controller/storm/`)

One switch per customer, on the portal and API. When it is on:

- Bring the satellite tunnel up and keep it warm (probing continues; BGP is established but less preferred).
- Allow `voice` and `business` onto satellite if both terrestrial paths fail. `bulk` stays off satellite unless the admin allows it.
- Tighten the forecast horizon to 120 seconds and cut the hold time to 60 seconds.
- Raise probe frequency to 2 per second on terrestrial paths.
- Log a Storm Mode event and show a coral Storm Mode marker in the portal.

Switching it off reverses all of this and records who switched it and when.

### 4.5 Metering and settlement (`controller/metering/`)

- Sample bytes in and out per link every 60 seconds from the agent, and at the PoP interface facing each carrier.
- Roll up to 5-minute average Mbps.
- **95th percentile per billing period**: sort the 5-minute samples for the month, discard the top 5%, and the highest remaining sample is the billable rate. Use the higher of in and out.
- **Settlement per carrier link**: commit charge, plus burst charge (billable rate above commit × burst price). Show both side by side with the raw samples.
- **Carrier view**: a read-only page and CSV export showing exactly the samples used, so disputes are settled on the same numbers.
- **Customer view**: usage per site and path, and the share that went over burst or satellite, with the routing reason.

Test the 95th percentile against a hand-worked example with known answers.

### 4.6 Portal (`portal/`)

Screens:

1. **Overview**: sites with health, SLA met in the last 24 hours per class, active decisions, and the Storm Mode switch.
2. **Site**: every path with live latency, jitter and loss charts, the current steering map, and BFD and tunnel state.
3. **Decisions**: a timeline of routing decisions with reasons, filterable by site and class.
4. **Metering**: usage per link, the 95th percentile line, the commit line, and burst.
5. **Carrier view**: read-only, the carrier's own links only.
6. **Admin**: sites, links, classes, SLA policies, enrolment tokens.

Brand rules (from the ExaCarib brand system):

- Colours: navy `#011F4D`, navy-deep `#00142F`, teal `#00ABB6` (fills and marks only), teal-strong `#00737B` (teal text and links), page grey `#E6ECF2`, white cards, ink `#0E1B2E`, ink-muted `#4A5A70`, line `#C9D3DE`. Status colours: success `#18704B`, warning `#9A5B00`, danger `#B42318`, always with a word or icon. Coral `#FF7A59` means backup and Storm Mode only.
- Fonts: Lexend for headings, IBM Plex Sans for body, IBM Plex Mono for figures.
- Layout: grey page, white cards with a soft shadow, 8px corners on cards and buttons.
- Copy: plain, short, British and Caribbean spelling ("organisation", "centre"). Never call ExaCarib a carrier, telco or integrator. Any screen with sample data carries an "Example data" label.

### 4.7 Lab (`lab/`)

Phase A runs on one Linux host with containerlab. Use whichever is available:

- an Ubuntu 24.04 arm64 VM in Parallels on Dudley's Mac (when working from the desktop app), or
- a Linux cloud VM with root access (for example Oracle Cloud's free Arm tier) when working in a cloud session that cannot run containerlab itself.

If neither is available yet, build and unit-test M0 to M3 without the lab, and keep the lab files ready to run later. The lab has:

- Nodes: `site-a`, `site-b`, `pop-miami`, and three underlay routers `carrier-a`, `carrier-b`, `sat`.
- Underlay profiles with `tc netem`:
  - carrier A: 25 ms, 2 ms jitter
  - carrier B: 35 ms, 4 ms jitter
  - satellite, LEO: 45 ms, 10 ms jitter, 0.5% loss
  - satellite, GEO: 600 ms, 30 ms jitter, 1% loss
- Traffic generators on each site: `voice` (small UDP packets at 50 per second), `business` (iperf3 TCP), `bulk` (iperf3 TCP, high rate).
- Fault scripts in `lab/faults/`: `brownout.sh <link> <loss%> <ramp-seconds>`, `latency-creep.sh`, `cut.sh <link>`, `restore.sh`, and `storm.sh` (cut both terrestrial links in turn).
- The controller runs in Docker Compose on the same VM for Phase A.

Phase B, the real-world test: two Parallels VMs or small boxes as sites, one on home broadband and one on a phone hotspot, with the controller and PoP on a cloud VM (for example Oracle Cloud's free Arm tier). Dudley sets up cloud accounts and logins himself. Never ask for, store or print passwords, API keys or tokens; read them from the environment or a local `.env` that is git-ignored.

---

## 5. Demo script (the acceptance test)

The MVP is done when this runs end to end, from `make demo`, and each step shows in the portal:

1. **Start**: `make lab-up && make demo-seed`. Two sites, one PoP, three paths, three classes. Everything green.
2. **Brownout**: carrier A loss ramps from 0% to 3% over 3 minutes. `voice` moves to carrier B **before** its 1% loss SLA is breached, and the decision log shows the forecast reason. `bulk` stays on A until A breaches its own threshold.
3. **Hard cut**: carrier B is cut. BFD detects it within 1 second, and classes move to the best remaining path within 3 seconds. A continuous `voice` stream loses under 3 seconds of packets.
4. **Recovery**: the links are restored. Classes move back only after the hold time. No flapping.
5. **Storm Mode**: switched on in the portal. The satellite tunnel is warm. Both terrestrial links are cut. `voice` and `business` continue on satellite and `bulk` pauses. Switch off, and it all reverses.
6. **Metering**: the metering screen shows the 95th percentile and burst for each carrier link for the demo period. The carrier view shows only that carrier's links, and the CSV matches the screen.
7. **Controller outage**: stop the controller. Sites keep forwarding on the last map, and BFD failover still works. Start it again, and the agents reconcile.

---

## 6. Repository layout

```
exaconnect/
  CLAUDE.md            this brief
  README.md            how to run the lab and demo
  Makefile             lab-up, lab-down, demo-seed, demo, test, build
  agent/               Go edge agent
  controller/          FastAPI app: api, routing, storm, metering, models
  portal/              React app
  lab/                 containerlab topology, netem profiles, fault scripts
  deploy/              docker-compose, systemd units, cloud notes
  docs/                decisions (ADR files), API notes, runbooks
  .github/workflows/   CI: lint, unit tests, arm64 and amd64 builds
```

---

## 7. Milestones

Work through these in order. At the end of each one, stop, show what runs, and write a short note in `docs/progress.md`.

| # | Milestone | Done when |
| --- | --- | --- |
| M0 | Lab and skeleton | containerlab topology up; repo layout, Makefile and CI in place |
| M1 | Tunnels and routing | WireGuard over three underlays; FRR BGP and BFD up; site-to-site ping via the PoP |
| M2 | Controller and agent | Enrolment, mTLS and desired-state apply with rollback; agent survives controller loss |
| M3 | Probing and telemetry | Per-path latency, jitter and loss in the DB and charted in a basic portal page |
| M4 | Steering and AI SLA routing | Demo steps 2 to 4 pass, with decisions and reasons logged; shadow mode works |
| M5 | Storm Mode | Demo step 5 passes |
| M6 | Metering and carrier view | Demo step 6 passes; 95th percentile unit tests pass |
| M7 | Portal polish and full demo | All seven demo steps run from `make demo`; README complete |

---

## 8. Working rules

- Ask before installing anything on the Mac itself, before creating cloud resources, and before anything that costs money.
- Linux-only work (containerlab, FRR, WireGuard, nftables) happens on a Linux host (the Parallels VM or a cloud VM), never on macOS itself.
- Commit and push to GitHub at the end of every milestone, so desktop and cloud sessions always work from the same code.
- No secrets in the repo, in logs or in chat. Use `.env.example` with placeholder names only.
- Tests with every component: unit tests for scoring, forecasting, hysteresis and the 95th percentile; integration tests against the lab for steering.
- Keep a decision record in `docs/adr/` when you choose between options.
- Prefer boring, well-known tools. If something in this brief turns out wrong in practice, say so, propose the change and record it.

---

## 9. After the MVP (for context only; do not build now)

- VPP/DPDK fast path on the PoP for higher throughput.
- Physical carrier handoffs and cross-connects in neutral data centres.
- Cloud on-ramps (AWS Direct Connect, Azure ExpressRoute, Google Partner Interconnect).
- MEF LSO Sonata ordering with carriers.
- NetBox as the source of truth; Temporal for workflows; Kafka for telemetry at scale.
- The AI-Based Self-Managed NOC and the IoT Platform on top of the same data.
- The Caribbean Connectivity Index built from aggregated probe data, publishing a carrier's name only with that carrier's agreement.
