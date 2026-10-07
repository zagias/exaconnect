#!/usr/bin/env bash
# Controller outage (demo step 7). The agents lose the controller: sites keep
# forwarding on the last map, and BFD failover still works. Bring it back, and
# the agents reconcile.
#
# The lab shares its host with the live controller (connect.exacarib.com), so
# this never stops the controller or its proxy: that took the live portal down
# on every lab run. Instead each lab agent gets an unreachable route to the
# controller's lab address, which to an agent is the same as a dead controller.
# The portal, the public agent gateway and real sites carry on (ADR 0040).
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
VOICE=0x101 DST=192.168.20.10
# The controller's address on the lab's management network (the proxy).
CONTROLLER_IP=172.30.0.5
NODES=(site-a site-b pop-miami)
lab/faults/restore.sh >/dev/null

silences() { lab/scripts/agents.sh logs "$1" 100000 | grep -c "controller silent"; }
declare -A seen
for n in "${NODES[@]}"; do seen[$n]=$(silences "$n"); done

# cut_off add|del: the agents' route to the controller.
cut_off() {
  for n in "${NODES[@]}"; do
    docker exec "$(node "$n")" ip route "$1" unreachable "$CONTROLLER_IP/32" 2>/dev/null
  done
}
# Whatever happens below, the agents get the controller back.
trap 'cut_off del' EXIT

echo "-- cut the agents off from the controller (the live controller keeps running)"
cut_off add
t0=$(date +%s)
isolated() {
  for n in "${NODES[@]}"; do
    docker exec "$(node "$n")" ip route show "$CONTROLLER_IP/32" | grep -q '^unreachable' || return 1
  done
}
if isolated; then
  ok "the agents' route to the controller is cut"
else
  bad "could not cut the agents off from the controller"
fi
silent() { for n in "${NODES[@]}"; do (($(silences "$n") > seen[$n])) || return 1; done; }
if wait_for 100 silent; then
  ok "all three agents noticed after $(($(date +%s) - t0)) s and hold the last good state"
else
  bad "agents did not report the controller silent"
fi
if docker exec "$(node lan-a)" ping -c 20 -i 0.2 -W 1 -q "$DST" | grep -q ' 0% packet loss'; then
  ok "site A still reaches site B through the PoP"
else
  bad "site-to-site traffic stopped with the controller down"
fi

echo "-- BFD failover with the controller down"
before=$(path_of site-a "$VOICE" "$DST")
link=carrier-a
[[ $before == wg-b ]] && link=carrier-b
t1=$(date +%s%N)
lab/faults/cut.sh "$link" >/dev/null
moved() { p=$(path_of site-a "$VOICE" "$DST"); [[ -n $p && $p != "$before" ]]; }
if wait_for 10 moved; then
  ok "voice moved from $before to $(path_of site-a $VOICE $DST) in $(awk -v a="$t1" -v b="$(date +%s%N)" 'BEGIN { printf "%.1f", (b - a) / 1e9 }') s, without the controller"
else
  bad "voice stayed on ${before:-?} after $link was cut"
fi
lab/faults/restore.sh >/dev/null
# The point of cutting the agents off instead of stopping the controller.
if curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8000/healthz; then
  ok "the live controller stayed up throughout"
else
  bad "the live controller did not answer during the test"
fi

echo "-- give the agents the controller back"
t2=$(date +%s)
cut_off del
reconciled() {
  [[ $(sql "SELECT count(*) FROM nodes WHERE last_seen > to_timestamp($t2) AND apply_ok") == 3 ]]
}
if wait_for 90 reconciled; then
  ok "all three agents reconciled $(($(date +%s) - t2)) s after the controller came back"
else
  bad "agents did not all report back: $(sql "SELECT string_agg(name, ', ') FROM nodes WHERE NOT (last_seen > to_timestamp($t2) AND apply_ok)")"
fi
exit $fail
