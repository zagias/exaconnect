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

step "Controller"
make -s controller-up || { echo "FAIL controller-up"; exit 1; }

step "Agents"
if [[ $fresh == 1 || ! -s lab/.state/seed.json ]]; then
  make -s demo-seed || { echo "FAIL demo-seed"; exit 1; }
else
  # Re-seeding upserts inventory (new classes, SLAs) without re-enrolling.
  umask 077
  "${COMPOSE[@]}" exec -T controller python -m exaconnect_controller.seed --lab >lab/.state/seed.json
  lab/scripts/agents.sh restart
fi

if [[ -s deploy/public/site.env ]]; then
  step "Public portal"
  make -s public-up || echo "FAIL public-up"
fi

step "Waiting for tunnels, BGP and BFD"
for _ in $(seq 1 24); do
  lab/scripts/check-routing.sh >/dev/null 2>&1 && break
  sleep 5
done
exit 0
