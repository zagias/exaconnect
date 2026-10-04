#!/usr/bin/env bash
# Internet breakout, NAT gateway and firewall (ADR 0010). 198.51.100.1 stands
# in for the internet on every carrier router and behind the PoP (ix).
#  - through the PoP: lan-a reaches it, NATed to the PoP's address
#  - a firewall rule at the PoP blocks it and counts the hits
#  - a port forward on the PoP's address reaches a server on lan-a
#  - straight out at the site: NATed to site-a's carrier A address, and on to
#    carrier B when carrier A is cut
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
cid=$(customer_id)
a=$(sql "SELECT id FROM sites WHERE name = 'site-a'")
INET=198.51.100.1

# What an earlier run may have left behind (the lab database persists).
cleanup() {
  for r in $(api GET "/customers/$cid/internet" | jq -r '.rules[] | select(.description | startswith("lab ")) | .id'); do
    api DELETE "/customers/$cid/firewall/rules/$r" >/dev/null
  done
  for f in $(api GET "/customers/$cid/internet" | jq -r '.forwards[] | select(.description | startswith("lab ")) | .id'); do
    api DELETE "/customers/$cid/port-forwards/$f" >/dev/null
  done
  api PATCH "/customers/$cid/internet/sites/$a" '{"mode": "pop"}' >/dev/null
}
cleanup
reaches() { docker exec "$(node lan-a)" ping -c 3 -i 0.2 -W 1 -q "$INET" | grep -q ' 0% packet loss'; }
# nat_seen <node> <address>: conntrack on <node> shows lan-a's ping to the internet translated to <address>.
nat_seen() { docker exec "$(node "$1")" conntrack -L -p icmp -s 192.168.10.10 -d "$INET" 2>/dev/null | grep -q "dst=$2 "; }
via() { api GET "/customers/$cid/internet" | jq -r --arg a "$a" '.sites[] | select(.id == $a) | .via'; }

echo "-- through the PoP (the default)"
if wait_for 30 reaches; then ok "lan-a reaches the internet through the PoP"; else bad "lan-a cannot reach $INET through the PoP"; fi
if nat_seen pop-miami 100.64.0.2; then ok "the PoP translated it to its own address"; else bad "no NAT entry at the PoP for lan-a"; fi
note "site-a exits by: $(via)"

echo "-- firewall at the PoP"
rule=$(api POST "/customers/$cid/firewall/rules" \
  "{\"action\": \"deny\", \"site_id\": \"$a\", \"dst\": [\"$INET/32\"], \"protocol\": \"icmp\", \"description\": \"lab no pings\"}" |
  jq -r '.id // empty')
blocked() { ! docker exec "$(node lan-a)" ping -c 2 -i 0.2 -W 1 -q "$INET" >/dev/null 2>&1; }
if [[ -n $rule ]] && wait_for 30 blocked; then ok "a deny rule stops lan-a's pings at the PoP"; else bad "the deny rule did not block lan-a"; fi
hits() { (($(api GET "/customers/$cid/internet" | jq --argjson r "${rule:-0}" '.rules[] | select(.id == $r) | .packets') > 0)); }
if wait_for 30 hits; then ok "the rule's hits reach the portal"; else bad "no hits reported for the rule"; fi
api DELETE "/customers/$cid/firewall/rules/$rule" >/dev/null
if wait_for 30 reaches; then ok "deleting the rule lets the pings through again"; else bad "still blocked after deleting the rule"; fi

echo "-- port forward on the PoP's address"
# iperf3 makes a TCP server and client that behave the same on every node.
serve() {
  docker exec "$(node lan-a)" pkill -f "iperf3 -s -p 8080" 2>/dev/null
  docker exec -d "$(node lan-a)" iperf3 -s -p 8080
  sleep 1
}
# connects <from node> <address>: a 1-second iperf3 run to <address>:8080 completes.
connects() { docker exec "$(node "$1")" iperf3 -c "$2" -p 8080 -t 1 --connect-timeout 3000 -J 2>/dev/null | jq -e '.end.sum_received.bytes > 0' >/dev/null; }
serve
if connects site-a 192.168.10.10; then note "server on lan-a:8080 answers locally"; else note "server on lan-a:8080 does not answer even from site-a"; fi
fwd=$(api POST "/customers/$cid/port-forwards" \
  "{\"protocol\": \"tcp\", \"port\": 8080, \"to_site_id\": \"$a\", \"to_address\": \"192.168.10.10\", \"description\": \"lab web\"}" |
  jq -r '.id // empty')
answered() { connects ix 100.64.0.2; }
if [[ -n $fwd ]] && wait_for 30 answered; then
  ok "a connection to 100.64.0.2:8080 from the internet reaches lan-a"
else
  bad "port forward 8080 to lan-a did not answer"
  docker exec "$(node pop-miami)" nft list chain ip exa_inet pre 2>&1 | grep -E 'dnat|Error' | sed 's/^/      /'
  docker exec "$(node pop-miami)" nft list chain ip exa_inet filter_fwd 2>&1 | grep -E 'pf|inbound' | sed 's/^/      /'
  docker exec "$(node pop-miami)" conntrack -L -p tcp --orig-port-dst 8080 2>/dev/null | head -3 | sed 's/^/      /'
  docker exec "$(node site-a)" ip route get 100.64.0.1 from 192.168.10.10 iif eth4 2>&1 | head -1 | sed 's/^/      /'
fi
docker exec "$(node ix)" ip route replace 192.168.10.0/24 via 100.64.0.2
if ! connects ix 192.168.10.10; then ok "the internet cannot reach lan-a directly through the PoP"; else bad "lan-a answered a connection that was not forwarded"; fi
docker exec "$(node ix)" ip route del 192.168.10.0/24 via 100.64.0.2
docker exec "$(node lan-a)" pkill -f "iperf3 -s -p 8080" 2>/dev/null
api DELETE "/customers/$cid/port-forwards/$fwd" >/dev/null

echo "-- straight out at the site"
api PATCH "/customers/$cid/internet/sites/$a" '{"mode": "local"}' >/dev/null
local_a() { reaches && nat_seen site-a 10.11.1.2; }
if wait_for 40 local_a; then ok "lan-a reaches the internet over carrier A, translated to site-a's address"; else bad "no local breakout over carrier A (exits by $(via))"; fi
lab/faults/cut.sh carrier-a >/dev/null
t0=$SECONDS
local_b() { reaches && nat_seen site-a 10.12.1.2; }
if wait_for 30 local_b; then ok "with carrier A cut, it leaves by carrier B within $((SECONDS - t0)) s"; else bad "no failover to carrier B (exits by $(via))"; fi
lab/faults/restore.sh carrier-a >/dev/null
viaeth() { [[ $(via) == eth* ]]; }
if wait_for 30 viaeth; then ok "site-a reports it exits by $(via)"; else bad "site-a reports exit '$(via)'"; fi

echo "-- clean up"
cleanup
back() { reaches && [[ $(via) == wg-* ]]; }
if wait_for 40 back; then ok "back through the PoP"; else bad "site-a did not go back through the PoP (exits by $(via))"; fi
exit "$fail"
