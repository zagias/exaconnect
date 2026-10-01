#!/usr/bin/env bash
# Traffic rules, priority queues and application detection (ADR 0007).
# Unmarked Zoom-like traffic leaves site A; the agent reports it, the
# controller recognises Zoom and suggests voice; applying the suggestion
# puts Zoom's ports in site A's classifier and the flows come back marked
# voice. A customer rule for a subnet and port reaches the classifier too,
# and every tunnel has a CAKE queue shaped under the link speed.
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
DST=192.168.20.10
lab/faults/restore.sh >/dev/null
cid=$(customer_id)

echo "-- priority queues"
for t in wg-a wg-b; do
  q=$(docker exec "$(node site-a)" tc qdisc show dev "$t" 2>&1 | head -1)
  if grep -q 'cake' <<<"$q" && grep -q 'diffserv4' <<<"$q"; then ok "site-a $t: ${q:0:90}"; else bad "site-a $t has no CAKE queue: $q"; fi
done

echo "-- application detection: Zoom-like traffic on UDP 8801 for 4 minutes"
# 160-byte datagrams from one socket, about 50 a second (iperf3's UDP mode
# stalled in this lab, so plain bash).
docker exec -d "$(node lan-b)" sh -c 'nc -u -l -p 8801 > /dev/null 2>&1'
docker exec -d "$(node lan-a)" bash -c "exec 3>/dev/udp/$DST/8801; for i in \$(seq 12000); do printf '%0160d' 0 >&3 2>/dev/null; sleep 0.02; done"
reported() { [[ $(sql "SELECT count(*) FROM flow_stats WHERE dport = 8801 AND time > now() - interval '5 minutes'") -gt 0 ]]; }
if wait_for 200 reported; then ok "site-a reported its flows to UDP 8801"; else bad "no flow telemetry for UDP 8801"; fi
detected() {
  api POST "/customers/$cid/applications/detect" >/dev/null
  det=$(api GET "/customers/$cid/applications" | jq -c '[.[] | select(.key == "app:zoom" and .site == "site-a")][0] // empty')
  [[ -n $det ]]
}
if wait_for 120 detected; then
  ok "recognised: $(jq -r .reason <<<"$det")"
else
  bad "Zoom was not detected"
  note "$(sql "SELECT proto, dport, class_name, sum(pkts_out), sum(pkts_in) FROM flow_stats WHERE time > now() - interval '10 minutes' GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 5" | tr '\n' ' ')"
fi
if [[ -n ${det:-} ]]; then
  if [[ $(jq -r .suggested_class <<<"$det") == voice ]]; then ok "suggests voice"; else bad "suggests $(jq -r .suggested_class <<<"$det")"; fi
  rule=$(api POST "/applications/$(jq -r .id <<<"$det")/apply" | jq -r .rule_id)
  if [[ $rule =~ ^[0-9]+$ ]]; then ok "applied as rule $rule"; else bad "apply failed: $rule"; fi
  # nft lists a one-element set without braces and marks in full width.
  classified() { docker exec "$(node site-a)" nft list ruleset 2>/dev/null | grep -Eq 'udp dport \{? ?8801-8810 \}? ?meta mark set 0x0*101 .*ip dscp set (ef|46)'; }
  if wait_for 30 classified; then ok "site-a classifies UDP 8801-8810 as voice"; else bad "Zoom ports not in site-a's classifier"; fi
  marked() { [[ $(sql "SELECT count(*) FROM flow_stats WHERE dport = 8801 AND class_name = 'voice' AND time > now() - interval '3 minutes'") -gt 0 ]]; }
  if wait_for 150 marked; then ok "Zoom flows now report as voice"; else bad "Zoom flows are not marked voice"; fi
  if docker exec "$(node site-b)" nft list ruleset 2>/dev/null | grep -q '8801-8810'; then
    bad "the site-a rule reached site-b"
  else
    ok "site-b is unchanged (the rule is for site-a)"
  fi
  api DELETE "/customers/$cid/rules/$rule" >/dev/null
fi

echo "-- a customer rule: a subnet and port into business"
body=$(jq -n '{name: "Lab ERP", class_name: "business", dst_subnets: ["192.168.20.0/24"], ports: "tcp:5201"}')
rule=$(api POST "/customers/$cid/rules" "$body" | jq -r .id)
erp() { docker exec "$(node site-a)" nft list ruleset 2>/dev/null | grep -Eq 'ip daddr \{? ?192.168.20.0/24 \}? ?tcp dport \{? ?5201 \}? ?meta mark set 0x0*102'; }
if wait_for 30 erp; then ok "site-a classifies TCP 5201 to site B as business"; else bad "the ERP rule did not reach site-a"; fi
pop() { docker exec "$(node pop-miami)" nft list ruleset 2>/dev/null | grep -Eq 'ip saddr \{? ?192.168.20.0/24 \}? ?tcp sport \{? ?5201 \}? ?meta mark set 0x0*102'; }
if wait_for 30 pop; then ok "the PoP classifies the return direction"; else bad "no return-direction rule at the PoP"; fi
api DELETE "/customers/$cid/rules/$rule" >/dev/null
exit $fail
