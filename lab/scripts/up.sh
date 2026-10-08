#!/usr/bin/env bash
# Bring the lab host to this commit: lab (rebuilt only when its definition
# changed), controller, agents. Used by make lab-ci and make demo.
set -uo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=lab/netem/netem.sh
source lab/netem/netem.sh
set +e
COMPOSE=(docker compose -f deploy/docker-compose.yml --env-file .env)
step() { printf '\n== %s (%s)\n' "$*" "$(date -u +%T)"; }

# Kernel modules the containers can't load themselves: XFRM interfaces for
# cloud circuits, VXLAN for layer 2 circuits (ADR 0009).
modprobe -a xfrm_interface vxlan 2>/dev/null || echo "note: could not load xfrm_interface/vxlan"

want=$(cat lab/exaconnect.clab.yml lab/images/node/* lab/netem/profiles.env 2>/dev/null | sha256sum | cut -c1-16)
have=$(cat lab/.state/lab.hash 2>/dev/null)
fresh=0
if ! docker ps --format '{{.Names}}' | grep -q "^$(node site-a)$" || [[ $want != "$have" ]]; then
  step "Redeploying the lab"
  make -s lab-down >/dev/null 2>&1
  make -s lab-up || { echo "FAIL lab-up"; exit 1; }
  mkdir -p lab/.state && echo "$want" >lab/.state/lab.hash
  fresh=1
else
  step "Lab already up; rebuilding the agent"
  make -s lab-agent || { echo "FAIL agent build"; exit 1; }
fi

step "Release (controller, agent gateway, public portal)"
# Health-checked, with automatic rollback to the previous release (ADR 0025). A fresh
# lab has no agents running yet, so the agents check waits for the next release.
if ! EXA_RELEASE_SKIP_AGENTS=$fresh deploy/release/release.sh; then
  echo "FAIL release: this commit was not healthy and the previous release is back (see above)"
  exit 1
fi

step "Voice SBC (locked down)"
# Kamailio and FreeSWITCH next to the controller, with no SIP or media port published
# (Dudley approved starting it on 2026-10-08). Calls stay on the simulated provider until
# a carrier account exists (deploy/kamailio/README.md). A failure here leaves the site up.
make -s voice-up || echo "FAIL voice SBC did not start (the site is unaffected)"

step "Agents"
if [[ $fresh == 1 || ! -s lab/.state/seed.json ]]; then
  make -s demo-seed || { echo "FAIL demo-seed"; exit 1; }
else
  # Re-seeding upserts inventory (new classes, SLAs) without re-enrolling.
  umask 077
  "${COMPOSE[@]}" exec -T controller python -m exaconnect_controller.seed --lab --sat "$SAT_PROFILE" >lab/.state/seed.json
  lab/scripts/agents.sh restart
fi

step "Waiting for tunnels, BGP and BFD"
for _ in $(seq 1 24); do
  lab/scripts/check-routing.sh >/dev/null 2>&1 && break
  sleep 5
done
exit 0
