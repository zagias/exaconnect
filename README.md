# ExaConnect

ExaCarib's connectivity platform: a Go edge agent (WireGuard + FRR), a FastAPI
controller, AI SLA routing, customer traffic rules with priority queues and
application detection, per-site Storm Mode, carrier metering and
settlement, AI insights (hurricane watch, bill forecast, carrier anomalies,
"Ask your network") and an ExaCarib-branded portal for customers, carriers
and admins.
The build brief is [CLAUDE.md](CLAUDE.md); progress per milestone is in
[docs/progress.md](docs/progress.md).

| Path | What |
|---|---|
| `agent/` | Go edge agent, one static binary for linux/arm64 and linux/amd64 |
| `controller/` | FastAPI controller (API, routing, storm, metering, models) |
| `portal/` | React + TypeScript + Vite portal |
| `lab/` | containerlab topology, netem profiles, fault scripts, lab host setup |
| `deploy/` | Docker Compose for the controller, database and agent TLS proxy; `deploy/public` for the HTTPS portal |
| `docs/` | Lab runbook, ADRs, progress notes |

## Develop (any OS)

```bash
make test                               # Go tests, controller tests, portal type check
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
make lab-up                             # build node image, deploy, apply profiles, smoke test
make controller-up                      # controller on 127.0.0.1:8000 (SSH tunnel); generates .env secrets
make demo-seed                          # seed sites, enrol and start the agents
make lab-routing                        # tunnels, BGP, BFD, site-to-site ping via the PoP
lab/faults/brownout.sh carrier-a 3 180  # faults: brownout, latency-creep, cut, restore, storm
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
Only ports 80 and 443 are opened; the cloud firewall must allow them too.
The controller API and database stay private, and agent endpoints are not
served publicly.

## Configuration

Secrets live only in `.env` on the host (git-ignored); `.env.example` lists the
names. `lab/scripts/init-env.sh` generates the database password, proxy
secret and admin password. Optional: `EXA_LLM_API_KEY` turns on "Ask your
network" (DeepInfra by default, model `deepseek-ai/DeepSeek-V4-Flash`), and
`EXA_NHC_URL` points the hurricane watch elsewhere or, set empty, turns it off.
