#!/usr/bin/env bash
# M5: Storm Mode (demo step 5). Switch on: satellite warm; cut both
# terrestrial links: voice and business continue on satellite and bulk
# pauses. Switch off: it all reverses.
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
VOICE=0x101 BUSINESS=0x102 BULK=0x103 DST=192.168.20.10
cid=$(customer_id)
lab/faults/restore.sh >/dev/null

echo "-- Storm Mode on"
if api POST "/customers/$cid/storm" '{"on": true}' | jq -e '.storm_mode == true' >/dev/null; then
  ok "Storm Mode switched on through the API"
else
  bad "could not switch Storm Mode on"
fi
storm_applied() { docker exec "$(node site-a)" cat /var/lib/exaconnect/steering.json 2>/dev/null | jq -e .storm >/dev/null; }
if wait_for 30 storm_applied; then ok "site-a agent has the Storm Mode steering map"; else bad "site-a agent did not get the Storm Mode map"; fi
sleep 20
sat_rate=$(sql "SELECT round(avg(sent)) FROM path_metrics pm JOIN nodes n ON n.id = pm.node_id
                WHERE n.name = 'site-a' AND pm.path = 'sat' AND pm.time > now() - interval '15 seconds'")
if [[ -n $sat_rate ]] && ((sat_rate >= 40)); then ok "satellite warm: $sat_rate probes per 10 s"; else bad "satellite probes per 10 s: ${sat_rate:-none}"; fi
bgp_sat=$(docker exec "$(node site-a)" vtysh -c 'show bgp summary json' | jq '[.ipv4Unicast.peers | to_entries[] | select(.key | startswith("100.64.3.")) | select(.value.state == "Established")] | length')
if ((bgp_sat >= 1)); then ok "satellite BGP established"; else bad "satellite BGP not established"; fi

echo "-- both terrestrial links cut"
docker exec -d "$(node lan-a)" sh -c "ping -Q 184 -i 0.02 -c 1500 -W 1 $DST > /tmp/storm-voice.txt 2>&1"
sleep 3
lab/faults/cut.sh carrier-a >/dev/null
lab/faults/cut.sh carrier-b >/dev/null
sleep 15
for c in "voice $VOICE" "business $BUSINESS"; do
  read -r name mark <<<"$c"
  p=$(path_of site-a "$mark" "$DST")
  if [[ $p == wg-sat ]]; then ok "$name continues on satellite"; else bad "$name on '${p:-none}', want wg-sat"; fi
done
if docker exec "$(node site-a)" ip rule show | grep -q "fwmark 0x103.*blackhole"; then
  ok "bulk paused (blackhole rule)"
else
  bad "bulk not paused: $(path_of site-a $BULK $DST)"
fi
sleep 15
got=$(docker exec "$(node lan-a)" sh -c "grep -o '[0-9]* received' /tmp/storm-voice.txt" | cut -d' ' -f1)
if [[ -n $got ]] && ((1500 - got < 150)); then ok "voice stream lost $(((1500 - got) * 20)) ms across the double cut"; else bad "voice stream received ${got:-?}/1500"; fi

echo "-- restore and switch Storm Mode off"
lab/faults/restore.sh >/dev/null
sleep 10
if api POST "/customers/$cid/storm" '{"on": false}' | jq -e '.storm_mode == false' >/dev/null; then
  ok "Storm Mode switched off"
else
  bad "could not switch Storm Mode off"
fi
off_terrestrial() {
  [[ $(path_of site-a "$VOICE" "$DST") == wg-[ab] && $(path_of site-a "$BUSINESS" "$DST") == wg-[ab] ]] &&
    ! docker exec "$(node site-a)" ip rule show | grep -q blackhole
}
if wait_for 60 off_terrestrial; then ok "voice and business back on terrestrial paths, bulk resumed"; else bad "classes did not leave satellite"; fi
by=$(sql "SELECT storm_by FROM customers WHERE id = '$cid'")
note "Storm Mode last switched by ${by:-?}"
sql "SELECT to_char(time, 'HH24:MI:SS') || ' ' || kind FROM events WHERE kind LIKE 'storm_%' ORDER BY time DESC LIMIT 2" | sed 's/^/      /'
exit $fail
