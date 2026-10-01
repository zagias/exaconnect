#!/usr/bin/env bash
# M4: per-class steering and AI SLA routing (demo steps 2 to 4).
#  - steering maps applied on every node (nftables, ip rules, path tables)
#  - brownout on carrier A: voice moves to B before its SLA is breached,
#    with a forecast reason; bulk stays on A
#  - hard cut of the path voice is on: local failover in under 3 s and a
#    continuous voice stream loses under 3 s of packets
#  - recovery: classes move back only after the hold time, no flapping
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
VOICE=0x101 BULK=0x103 DST=192.168.20.10

echo "-- steering applied"
for n in pop-miami site-a site-b; do
  if docker exec "$(node "$n")" nft list table ip exaconnect >/dev/null 2>&1; then ok "$n nftables classifier"; else bad "$n nftables classifier missing"; fi
  rules=$(docker exec "$(node "$n")" ip rule show | grep -c fwmark)
  if ((rules >= 3)); then ok "$n $rules steering rules"; else bad "$n steering rules: $rules"; fi
done
v=$(path_of site-a $VOICE $DST)
if [[ $v == wg-a ]]; then ok "site-a voice on wg-a at start"; else bad "site-a voice on '${v:-none}' at start, want wg-a"; fi
note "$(sql "SELECT 'maps: ' || string_agg(n.name || ' v' || m.v, ', ') FROM nodes n
             JOIN (SELECT node_id, max(version) v FROM steering_maps GROUP BY node_id) m ON m.node_id = n.id")"

echo "-- brownout: carrier A loss 0 -> 3% over 180 s (demo step 2)"
lab/faults/restore.sh >/dev/null
t0=$(date +%s)
lab/faults/brownout.sh carrier-a 3 180 >/dev/null &
ramp_pid=$!
voice_moved() { (($(moves_since voice "$t0") > 0)); }
voice_on_b() { [[ $(path_of site-a "$VOICE" "$DST") == wg-b ]]; }
if wait_for 330 voice_moved; then
  at=$(($(date +%s) - t0))
  reason=$(sql "SELECT reason FROM decisions WHERE class_name = 'voice' AND kind = 'move'
                AND time > to_timestamp($t0) ORDER BY time LIMIT 1")
  ok "voice moved $at s into the ramp"
  note "$reason"
  [[ $reason == *forecast* || $reason == *"is "* ]] || bad "reason has no measurement: $reason"
  # The SLA window (3 min of probes) when it moved, from the stored inputs.
  measured=$(sql "SELECT round((inputs->'paths'->'carrier-a'->'metrics'->'loss'->>'now')::numeric, 2) FROM decisions
                  WHERE class_name = 'voice' AND kind = 'move' AND time > to_timestamp($t0) ORDER BY time LIMIT 1")
  if [[ -n $measured ]] && awk -v m="$measured" 'BEGIN { exit !(m < 1) }'; then
    ok "moved before the 1% SLA was breached (measured ${measured}%)"
  else
    bad "moved at measured loss ${measured:-?}%, not before the 1% SLA"
    # Why it waited: the first hold note and how each path looked then.
    note "$(sql "SELECT reason || ' | ' || coalesce(inputs->'paths'::text, '') FROM decisions
                 WHERE class_name = 'voice' AND kind = 'hold' AND time > to_timestamp($t0) ORDER BY time LIMIT 1" | cut -c1-1500)"
  fi
  if wait_for 15 voice_on_b; then
    ok "site-a kernel sends voice on wg-b"
  else
    bad "site-a voice still on $(path_of site-a $VOICE $DST)"
  fi
else
  bad "voice did not move within 330 s of the brownout"
fi
wait "$ramp_pid" 2>/dev/null
sleep 20
if (($(moves_since bulk "$t0") == 0)) && [[ $(path_of site-a $BULK $DST) == wg-a ]]; then
  ok "bulk stayed on carrier A (3% is under its 5% threshold)"
else
  bad "bulk moved during the brownout"
fi

echo "-- hard cut of carrier B while voice is on it (demo step 3)"
lab/faults/restore.sh carrier-a >/dev/null
docker exec -d "$(node lan-a)" sh -c "ping -Q 184 -i 0.02 -c 1500 -W 1 $DST > /tmp/voice.txt 2>&1"
sleep 5
t1=$(date +%s)
lab/faults/cut.sh carrier-b >/dev/null
sleep 25
# The stream runs 30 s; wait for ping's summary line before reading it.
ping_done() { docker exec "$(node lan-a)" grep -q ' received' /tmp/voice.txt; }
wait_for 30 ping_done || true
lost=$(docker exec "$(node lan-a)" sh -c "grep -o '[0-9]* received' /tmp/voice.txt" | cut -d' ' -f1)
note "voice probe stream: ${lost:-?} of 1500 received at 50 packets/s"
if [[ -n $lost ]] && ((1500 - lost < 150)); then ok "voice lost $(( (1500 - lost) * 20 )) ms of packets (< 3 s)"; else bad "voice lost too much: received ${lost:-?}/1500"; fi
fo=$(sql "SELECT extract(epoch FROM min(time))::int - $t1 FROM events WHERE kind = 'class_moved'
          AND detail->>'class' = 'voice' AND detail->>'why' = 'bfd' AND time > to_timestamp($t1)")
if [[ -n $fo ]] && ((fo <= 3)); then ok "agent failover on BFD after ${fo} s"; else bad "no BFD failover event within 3 s (got '${fo}')"; fi
v=$(path_of site-a $VOICE $DST)
if [[ $v == wg-a ]]; then ok "voice on wg-a after the cut"; else bad "voice on '${v:-none}' after the cut"; fi

echo "-- recovery: restore B, classes move back only after the hold time (demo step 4)"
t2=$(date +%s)
lab/faults/restore.sh >/dev/null
sleep 60
flaps=$(sql "SELECT count(*) FROM decisions WHERE kind <> 'hold' AND time > to_timestamp($t2)")
if [[ $flaps == 0 ]]; then ok "no moves in the first minute after recovery"; else bad "$flaps moves within a minute of recovery"; fi
total=$(sql "SELECT count(*) FROM decisions WHERE kind <> 'hold' AND time > to_timestamp($t0)")
note "moves since the brownout began: $total"
sql "SELECT to_char(d.time, 'HH24:MI:SS') || ' ' || s.name || ' ' || d.class_name || ' ' || d.kind || ': ' || d.reason
     FROM decisions d JOIN sites s ON s.id = d.site_id WHERE d.time > to_timestamp($t0) ORDER BY d.time" |
  sed 's/^/      /'
exit $fail
