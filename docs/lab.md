# Lab runbook (Phase A)

One Ubuntu 24.04 arm64 VM in Parallels runs everything: containerlab for the
network, Docker Compose for the controller and database.

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

## 1. Create the VM (once, in Parallels on the Mac)

Nothing is installed on macOS itself.

1. Download the Ubuntu Server 24.04 LTS **arm64** ISO from ubuntu.com.
2. Parallels: File → New → Install from image → the ISO. Before starting,
   Configure → Hardware: 4 CPUs, 8 GB RAM, 40 GB disk. Network: Shared.
3. Install with OpenSSH enabled. Note the VM's IP (`ip -4 addr`).
4. In the VM: `git clone https://github.com/zagias/exaconnect && cd exaconnect`
5. `sudo lab/host/setup-ubuntu.sh`, then log out and in again.

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

API docs: `http://<vm-ip>:8000/api/v1/docs`. To run the portal against it from
the Mac: `cd portal && EXA_CONTROLLER=http://<vm-ip>:8000 npm run dev`.

## 5. Tear down

`make controller-down && make lab-down`
