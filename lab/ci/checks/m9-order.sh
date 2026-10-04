#!/usr/bin/env bash
# Plain-English ordering (ADR 0011), with the rules engine so the result
# doesn't depend on an AI service:
#  - "join Kingston and Port of Spain on VLAN 100 and 200" drafts a layer 2
#    circuit; confirming it brings the circuit up
#  - "increase ... to 30 Mbps" and "send Kingston's internet straight out",
#    in one order, apply together
#  - a draft with a problem can't be confirmed and changes nothing
# When an AI service is configured, one AI draft is noted (never confirmed).
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
cid=$(customer_id)
a=$(sql "SELECT id FROM sites WHERE name = 'site-a'")
NAME="lab order l2"

cleanup() {
  for c in $(api GET "/customers/$cid/circuits" | jq -r '.[] | select(.name | startswith("lab ")) | .id'); do
    api DELETE "/customers/$cid/circuits/$c" >/dev/null
  done
  api PATCH "/customers/$cid/internet/sites/$a" '{"mode": "pop"}' >/dev/null
}
cleanup
# draft <text>: a draft order from the rules engine; prints its JSON.
draft() { api POST "/customers/$cid/orders/draft" "$(jq -n --arg t "$1" '{text: $t, engine: "rules"}')"; }
confirm() { api POST "/customers/$cid/orders/$1/confirm" '{"inputs": []}'; }
circuit() { api GET "/customers/$cid/circuits" | jq -c --arg n "$NAME" '.[] | select(.name == $n)'; }

echo "-- a layer 2 circuit ordered in plain English"
o=$(draft "Join Kingston and Port of Spain on VLAN 100 and 200 called \"$NAME\" at 20 Mbps")
note "draft: $(jq -c '{actions: [.actions[].action], summary, problems, monthly_estimate}' <<<"$o")"
if [[ $(jq -r '[.actions[0].action, .actions[0].a_vlan, .actions[0].b_vlan, (.problems | length)] | join(" ")' <<<"$o") == "site_circuit 100 200 0" ]]; then
  ok "the draft reads a VLAN 100 to VLAN 200 circuit between site-a and site-b"
else
  bad "unexpected draft: $(jq -c '.actions' <<<"$o")"
fi
if [[ -z $(circuit) ]]; then ok "drafting changed nothing"; else bad "a circuit exists before confirming"; fi
done1=$(confirm "$(jq -r .id <<<"$o")")
if [[ $(jq -r .status <<<"$done1") == "done" ]]; then ok "confirmed: $(jq -r '.results[0].message' <<<"$done1")"; else bad "confirm: $done1"; fi
l2ping() { docker exec "$(node lan-a)" ping -c 3 -i 0.2 -W 1 -q 172.16.100.20 | grep -q ' 0% packet loss'; }
if wait_for 60 l2ping; then ok "the ordered circuit carries traffic (lan-a pings lan-b over VLAN 100/200)"; else bad "lan-a cannot reach 172.16.100.20 over the ordered circuit"; fi

echo "-- two changes in one order"
o=$(draft "Increase $NAME to 30 Mbps. Send Kingston's internet straight out")
note "draft: $(jq -c '{actions: [.actions[].action], summary, monthly_estimate}' <<<"$o")"
if [[ $(jq -r '[.actions[].action] | join(" ")' <<<"$o") == "bandwidth internet_mode" ]]; then
  ok "the draft has a bandwidth change and an internet change"
else
  bad "unexpected draft: $(jq -c '.actions, .problems' <<<"$o")"
fi
done2=$(confirm "$(jq -r .id <<<"$o")")
mode() { api GET "/customers/$cid/internet" | jq -r --arg a "$a" '.sites[] | select(.id == $a) | "\(.mode) \(.via)"'; }
if [[ $(jq -r .status <<<"$done2") == "done" && $(circuit | jq -r .bandwidth_mbps) == 30 ]]; then
  ok "the circuit is now 30 Mbps"
else
  bad "bandwidth change: $(jq -c '.status, .results' <<<"$done2")"
fi
local_out() { read -r m v <<<"$(mode)" && [[ $m == local && $v == eth* ]]; }
if wait_for 40 local_out; then ok "site-a's internet now goes straight out ($(mode))"; else bad "site-a's internet: $(mode)"; fi

echo "-- a draft with a problem"
o=$(draft "Join Kingston and Port of Spain on VLAN 5000")
oid=$(jq -r .id <<<"$o")
# The API answers 400, so the call fails; the order stays a draft.
if [[ $(jq -r '.problems | join(" ")' <<<"$o") == *"1 to 4094"* ]] && ! confirm "$oid" >/dev/null 2>&1 &&
  [[ $(api GET "/customers/$cid/orders/$oid" | jq -r .status) == draft ]]; then
  ok "a draft with VLAN 5000 says why and can't be confirmed"
else
  bad "problem draft: $(jq -c '.problems' <<<"$o")"
fi
api POST "/customers/$cid/orders/$oid/cancel" >/dev/null

if [[ $(api GET /ai/status | jq -r .ask_enabled) == true ]]; then
  ai=$(api POST "/customers/$cid/orders/draft" '{"text": "Connect our Kingston office to AWS us-east-1 at 50 Mbps for 10.100.0.0/16"}')
  note "AI draft (not confirmed): engine $(jq -r .engine <<<"$ai"), $(jq -c '[.actions[] | {action, provider, region, site, bandwidth_mbps}]' <<<"$ai")"
  api POST "/customers/$cid/orders/$(jq -r .id <<<"$ai")/cancel" >/dev/null
fi

echo "-- clean up"
cleanup
back() { [[ $(mode) == pop* ]]; }
if wait_for 30 back && [[ -z $(circuit) ]]; then ok "circuit deleted and site-a back through the PoP"; else bad "clean up: $(mode)"; fi
exit "$fail"
