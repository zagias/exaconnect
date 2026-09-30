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
| `lab/` | containerlab topology, netem profiles, fault scripts, lab VM setup |
| `deploy/` | Docker Compose for the controller and database |
| `docs/` | Lab runbook, ADRs, progress notes |

## Develop (any OS)

```bash
make test                               # Go tests, controller tests, portal type check
make build                              # agent for linux/arm64 + amd64, portal bundle
pip install -e 'controller[dev]'        # once, for the controller tests
cd portal && npm ci && npm run dev      # portal on http://localhost:5173
```

## Run the lab (Linux lab VM)

The lab runs in one Ubuntu 24.04 arm64 VM in Parallels. Setting it up is in
[docs/lab.md](docs/lab.md). Short version, inside the VM:

```bash
sudo lab/host/setup-ubuntu.sh           # once: Docker, containerlab, kernel modules
make lab-up                             # build node image, deploy, apply profiles, smoke test
cp .env.example .env && $EDITOR .env    # once: set a local database password
make controller-up                      # controller on http://<vm>:8000
lab/faults/brownout.sh carrier-a 3 180  # faults: brownout, latency-creep, cut, restore, storm
make lab-down
```
