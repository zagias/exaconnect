#!/usr/bin/env bash
# Controller outage (demo step 7). Stop the controller: sites keep
# forwarding on the last map, and BFD failover still works. Start it again,
# and the agents reconcile.
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
VOICE=0x101 DST=192.168.20.10
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env")
lab/faults/restore.sh >/dev/null

silences() { lab/scripts/agents.sh logs "$1" 100000 | grep -c "controller silent"; }
declare -A seen
for n in site-a site-b pop-miami; do seen[$n]=$(silences "$n"); done

echo "-- stop the controller and the agent proxy"
"${COMPOSE[@]}" stop controller proxy >/dev/null 2>&1
t0=$(date +%s)
silent() { for n in site-a site-b pop-miami; do (($(silences "$n") > seen[$n])) || return 1; done; }
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

echo "-- start the controller again"
t2=$(date +%s)
"${COMPOSE[@]}" start controller proxy >/dev/null 2>&1
"${COMPOSE[@]}" up -d --force-recreate --no-deps --wait proxy >/dev/null 2>&1
reconciled() {
  [[ $(sql "SELECT count(*) FROM nodes WHERE last_seen > to_timestamp($t2) AND apply_ok") == 3 ]]
}
if wait_for 90 reconciled; then
  ok "all three agents reconciled $(($(date +%s) - t2)) s after the restart"
else
  bad "agents did not all report back: $(sql "SELECT string_agg(name, ', ') FROM nodes WHERE NOT (last_seen > to_timestamp($t2) AND apply_ok)")"
fi
exit $fail
