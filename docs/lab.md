# Lab runbook (Phase A)

One Ubuntu 24.04 Linux host runs everything: containerlab for the network,
Docker Compose for the controller and database. The host is a cloud VM with
root access that you create yourself (CLAUDE.md §4.7); arm64 first, amd64 also
works.

```
            carrier-a (25 ms)          carrier-a, carrier-b and sat are
  site-a ── carrier-b (35 ms) ── pop-miami   Linux routers that apply tc netem
  site-b ── sat (LEO 45 ms / GEO 600 ms) ──┘
    │                                  │
  lan-a / lan-b                     lan-pop      traffic generators
```

| Segment | Carrier side | Site / PoP side |
|---|---|---|
| carrier-a ↔ pop-miami | 10.11.0.1 | 10.11.0.2 |
| carrier-a ↔ site-a / site-b | 10.11.1.1 / 10.11.2.1 | 10.11.1.2 / 10.11.2.2 |
| carrier-b | 10.12.x.1 | 10.12.x.2 (same pattern) |
| sat | 10.13.x.1 | 10.13.x.2 (same pattern) |

LANs: site-a 192.168.10.0/24 (`lan-a` .10), site-b 192.168.20.0/24 (`lan-b` .10),
PoP 10.200.0.0/24 (`lan-pop` .10). Management network `exaconnect-mgmt`
172.30.0.0/24; the controller joins it at 172.30.0.5 as `controller`.

Underlay interfaces on every site and the PoP: `eth1` carrier A, `eth2` carrier
B, `eth3` satellite, `eth4` LAN.

Overlay (M1): one WireGuard tunnel per underlay, hub and spoke through the PoP.

| Tunnel | Overlay | PoP port | PoP / site-a / site-b | BGP local-pref | BFD |
|---|---|---|---|---|---|
| `wg-a` | 100.64.1.0/24 | 51820 | .1 / .11 / .12 | 200 | 200 ms × 3 |
| `wg-b` | 100.64.2.0/24 | 51821 | .1 / .11 / .12 | 150 | 200 ms × 3 |
| `wg-sat` | 100.64.3.0/24 | 51822 | .1 / .11 / .12 | 50 | 1 s × 3 |

eBGP over every tunnel: PoP AS 65000, site-a AS 65001, site-b AS 65002. The
agents write the WireGuard and FRR config from the controller's desired state
([desired-state.md](desired-state.md)); nothing in `lab/` hard-codes it.

## 1. The lab host (once)

You create the VM in your cloud account; nothing here creates cloud resources
or costs money on its own.

| | Minimum | Comfortable |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| Memory | 8 GB | 16 GB |
| Disk | 40 GB | 60 GB |
| Image | Ubuntu 24.04 LTS, arm64 (amd64 also fine) | |
| Access | root via sudo, SSH key login | |

Any provider that gives you a full VM works (for example Oracle Cloud's free
Ampere A1 shape, AWS Graviton, Hetzner Arm). It must be a real VM, not a
container service, because the lab needs Docker in privileged mode and the
WireGuard and netem kernel modules.

Network: allow inbound SSH (22) only. Do not open 8000 or 5173; reach the
controller and portal through an SSH tunnel (section 4).

On the host:

```bash
git clone -b claude/exaconnect-mvp-lp3alu https://github.com/zagias/exaconnect
cd exaconnect
sudo lab/host/setup-ubuntu.sh     # Docker, containerlab, kernel modules
exit                              # log out and back in for the docker group
```

## 2. Bring the lab up

```bash
make lab-up
```

This builds `exaconnect/node:dev` (FRR 10.2 + WireGuard tools, iperf3, tcpdump,
nftables), deploys the topology, applies the netem profiles and runs
`lab/scripts/smoke.sh`, which pings the PoP from both sites over all three
underlays. Expected, with the default profiles:

```
ok    site-a  -> PoP via carrier-a avg rtt ~25ms
ok    site-a  -> PoP via carrier-b avg rtt ~35ms
ok    site-a  -> PoP via sat       avg rtt ~45ms
...
```

Shell into a node with `docker exec -it clab-exaconnect-site-a bash`.

## 3. Faults

All take a link name: `carrier-a`, `carrier-b` or `sat`. They act on the whole
carrier (both sites).

| Script | Effect |
|---|---|
| `lab/faults/brownout.sh <link> <loss%> <ramp-s>` | Ramp loss up from the current value |
| `lab/faults/latency-creep.sh <link> <extra-ms> <ramp-s>` | Ramp round-trip delay up |
| `lab/faults/cut.sh <link>` | 100 % loss both ways; interfaces stay up |
| `lab/faults/restore.sh [link]` | Back to profile (all links without an argument) |
| `lab/faults/storm.sh [gap-s]` | Cut carrier A, then carrier B after `gap-s` (default 30) |
| `SAT_PROFILE=geo lab/netem/apply-profiles.sh` | Satellite as GEO (600 ms) instead of LEO; saved for later scripts and `make demo-seed` |
| `LOSS_BOTH_WAYS=1 lab/netem/apply-profiles.sh` | Split each link's loss across both directions (off by default) |

Profiles are in `lab/netem/profiles.env` and are round-trip figures; see
[ADR 0002](adr/0002-netem-profile-semantics.md).

### Traffic

`make traffic` starts the three classes in both directions between `lan-a`
and `lan-b`, through the PoP; `make traffic-stop` stops them and
`lab/scripts/traffic.sh status` shows what runs. Each class can be started or
stopped on its own (`lab/scripts/traffic.sh start voice`).

| Class | Traffic | Classified by |
|---|---|---|
| voice | 160-byte UDP datagrams, 50 a second, to UDP 10000 | voice ports |
| business | iperf3 TCP at `BUSINESS_RATE` (5M) to port 5301 | DSCP AF31 |
| bulk | iperf3 TCP at `BULK_RATE` (50M) to port 5302 | DSCP CS1 |

Senders and receivers restart on their own after a cut. `make demo` runs this
traffic through steps 2 to 5 and stops it for the metering step, which
measures its own 20 Mbit/s; `DEMO_TRAFFIC=0 make demo` leaves it off.

## 4. Controller, agents and routing (M1 to M3)

```bash
make controller-up      # writes .env with generated secrets if missing, then
                        # starts TimescaleDB, the controller and the agent TLS proxy
make demo-seed          # inventory, enrolment tokens, enrols and starts the three agents
make lab-routing        # M1 check: handshakes, BGP, BFD, site-to-site ping via the PoP
```

`make controller-up` runs `lab/scripts/init-env.sh`, which adds any missing
secret to `.env` (mode 600) without printing it: the database password, the
proxy secret and the first admin's password. The admin email defaults to
`admin@exacarib.local`. Read the password on the host when you need it:
`grep EXA_ADMIN_PASSWORD .env`.

`make demo-seed` writes `lab/.state/seed.json` (mode 600, git-ignored) with
one-time tokens, valid for two hours, then runs `lab/scripts/agents.sh enrol`.
Each agent generates its WireGuard key and a TLS key on the node, pins the
controller's CA by fingerprint, and receives a client certificate. Only public
keys leave the node.

Agents:

| Command | What it does |
|---|---|
| `make agents-status` | Is each agent running |
| `lab/scripts/agents.sh logs site-a` | Agent log on one node |
| `make agents-stop` / `make agents-start` | Stop or start all agents; forwarding keeps running |
| `docker exec clab-exaconnect-site-a exa-agent apply -f <file>` | Apply a desired-state file by hand |

Controller outage test (demo step 7, the M2 part). On a host that also runs
the live site, never stop the controller or its proxy: that takes the portal
down. Cut the agents off from it instead, as `lab/ci/checks/m7-outage.sh` does
(ADR 0040):

```bash
for n in site-a site-b pop-miami; do docker exec clab-exaconnect-$n ip route add unreachable 172.30.0.5/32; done
make lab-routing       # still passes: agents keep the last good state
for n in site-a site-b pop-miami; do docker exec clab-exaconnect-$n ip route del unreachable 172.30.0.5/32; done
```

The agents log `controller_silent` after 60 s and `controller_back` when it
returns, and the events show on the site page.

## 5. Portal

The controller and portal listen on the host's loopback only. On the host, run
the portal in a Node container:

```bash
docker run --rm -d --name exa-portal --network host -v "$PWD/portal":/app -w /app \
  -e EXA_CONTROLLER=http://127.0.0.1:8000 node:22 \
  sh -c "npm ci && npx vite --host 127.0.0.1 --port 5173"
```

From your laptop:

```bash
ssh -L 5173:127.0.0.1:5173 -L 8000:127.0.0.1:8000 root@<host-ip>    # keep this open
```

Browse to http://localhost:5173 and sign in as the admin. The API docs are at
http://localhost:8000/api/v1/docs.

## 6. Tear down

`docker stop exa-portal; make agents-stop; make controller-down && make lab-down`

`make lab-down` deletes the nodes and with them the agents' keys and
certificates. After `make lab-up` again, run `make demo-seed` to enrol fresh.
