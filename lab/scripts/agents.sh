#!/usr/bin/env bash
# Enrols and runs exa-agent on the lab's sites and PoP.
#   agents.sh enrol <seed.json>   enrol every node with the tokens from the seed
#   agents.sh start | stop | restart | status
#   agents.sh logs <node>
# The seed JSON comes from `python -m exaconnect_controller.seed --lab` (make demo-seed).
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
NODES=(pop-miami site-a site-b)

running() { docker exec "$(node "$1")" pgrep -f "exa-agent run" >/dev/null 2>&1; }

start() {
  local n
  for n in "${NODES[@]}"; do
    if running "$n"; then echo "$n: agent already running"; continue; fi
    docker exec -d "$(node "$n")" sh -c 'exec exa-agent run >>/var/log/exa-agent.log 2>&1'
    echo "$n: agent started"
  done
}

stop() {
  local n
  for n in "${NODES[@]}"; do
    docker exec "$(node "$n")" pkill -f "exa-agent run" 2>/dev/null && echo "$n: agent stopped" || true
  done
}

case "${1:-}" in
  enrol)
    seed=${2:?seed json}
    fp=$(jq -r .ca_fingerprint "$seed")
    url=$(jq -r .agent_url "$seed")
    stop
    for n in "${NODES[@]}"; do
      tok=$(jq -r --arg n "$n" '.tokens[$n]' "$seed")
      docker exec "$(node "$n")" exa-agent enrol --controller "$url" --token "$tok" \
        --ca-fingerprint "$fp" --name "$n"
    done
    start ;;
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status)
    for n in "${NODES[@]}"; do
      if running "$n"; then echo "$n: running"; else echo "$n: stopped"; fi
    done ;;
  logs) docker exec "$(node "${2:?node}")" tail -n "${3:-50}" /var/log/exa-agent.log ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
