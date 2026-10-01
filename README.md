# ExaConnect

ExaCarib's SD-WAN MVP: a Go edge agent (WireGuard + FRR), a FastAPI controller,
AI SLA routing, Storm Mode, carrier metering and an ExaCarib-branded portal.
The build brief is [CLAUDE.md](CLAUDE.md); progress per milestone is in
[docs/progress.md](docs/progress.md).

| Path | What |
|---|---|
| `agent/` | Go edge agent, one static binary for linux/arm64 and linux/amd64 |
| `controller/` | FastAPI controller (API, routing, storm, metering, models) |
| `portal/` | React + TypeScript + Vite portal |
| `lab/` | containerlab topology, netem profiles, fault scripts, lab host setup |
| `deploy/` | Docker Compose for the controller, database and agent TLS proxy |
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
make lab-down
```
