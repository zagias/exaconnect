#!/usr/bin/env bash
# make lab-ci: bring the lab host to this commit and run every lab check.
# Used by the lab runner (lab/runner/) and safe to run by hand. Prints
# "ok", "FAIL" or "SKIP" lines; exits non-zero if any check failed.
set -uo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=lab/netem/netem.sh
source lab/netem/netem.sh
set +e
COMPOSE=(docker compose -f deploy/docker-compose.yml --env-file .env)
step() { printf '\n== %s (%s)\n' "$*" "$(date -u +%T)"; }
fail=0

echo "commit $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

# 1. Rebuild the lab only when its definition changed or it is not running.
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

step "Waiting for tunnels, BGP and BFD"
for _ in $(seq 1 24); do
  lab/scripts/check-routing.sh >/dev/null 2>&1 && break
  sleep 5
done

step "Checks"
for c in lab/scripts/check-routing.sh lab/ci/checks/*.sh; do
  [[ -x $c ]] || continue
  echo "-- $c"
  "$c" || fail=1
done

step "Diagnostics"
lab/scripts/agents.sh status
for n in pop-miami site-a site-b; do
  echo "-- agent log $n"
  lab/scripts/agents.sh logs "$n" 25
done
echo "-- controller log"
"${COMPOSE[@]}" logs --no-color --tail 60 controller 2>&1
echo "-- host"
uptime
vmstat 1 3

exit "$fail"
