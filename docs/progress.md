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

Next: M1, tunnels and routing (WireGuard over three underlays, FRR BGP + BFD,
site-to-site ping via the PoP).
