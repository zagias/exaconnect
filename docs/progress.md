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

Not yet verified, needs the lab host: WireGuard and FRR actually coming up
from desired state, BGP and BFD state, real probe numbers, and the controller
outage test. The steps are in docs/lab.md §4.

Next: M4, steering and AI SLA routing.
