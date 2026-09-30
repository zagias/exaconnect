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
B, `eth3` satellite, `eth4` LAN. WireGuard (`wg-a`, `wg-b`, `wg-sat`), FRR BGP
and BFD come in M1.

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
| `SAT_PROFILE=geo lab/netem/apply-profiles.sh` | Satellite as GEO (600 ms) instead of LEO |

Profiles are in `lab/netem/profiles.env` and are round-trip figures; see
[ADR 0002](adr/0002-netem-profile-semantics.md).

## 4. Controller

```bash
cp .env.example .env     # set POSTGRES_PASSWORD locally; .env is git-ignored
make controller-up
curl http://localhost:8000/healthz
```

The controller listens on the host's loopback only. From your laptop:

```bash
ssh -L 8000:127.0.0.1:8000 ubuntu@<host-ip>      # keep this open
open http://localhost:8000/api/v1/docs
cd portal && npm ci && npm run dev               # portal on http://localhost:5173, proxies to :8000
```

## 5. Tear down

`make controller-down && make lab-down`
