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

lab/scripts/up.sh || exit 1

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
echo "-- controller containers"
"${COMPOSE[@]}" ps --format '{{.Name}} {{.Status}}' 2>&1
for c in $("${COMPOSE[@]}" ps -q 2>/dev/null); do
  docker inspect -f '{{.Name}} restarts {{.RestartCount}}, started {{.State.StartedAt}}, OOM killed {{.State.OOMKilled}}' "$c"
done
echo "-- host"
uptime
free -m
vmstat 1 3

exit "$fail"
