#!/usr/bin/env bash
# ExaConnect Fabric (ADR 0009) against two simulated cloud VPN gateways:
#  - a cloud circuit to "AWS" and one to "Azure": IPsec and BGP up, the sites
#    learn each cloud's network, and lan-a reaches the AWS VPC
#  - the cloud router: AWS reaches Azure through the PoP
#  - elastic bandwidth: a new speed reaches the PoP's shaper
#  - a layer 2 circuit: VLAN 100 at site A and VLAN 200 at site B are one segment
# The pre-shared key is made for this run and never printed.
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
cid=$(customer_id)
psk="k$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 30)"
POP_ASN=65000

# Circuits an earlier run left behind (the lab database persists).
for c in $(api GET "/customers/$cid/circuits" | jq -r '.[] | select(.name | startswith("lab ")) | .id'); do
  api DELETE "/customers/$cid/circuits/$c" >/dev/null
done

# cloud_gateway <node> <if_id> <inside address/30, cloud side> <our inside address> <asn> <vpc prefix>:
# a route-based IPsec gateway with BGP, as AWS and Azure run them.
cloud_gateway() {
  local n=$1 ifid=$2 inside=$3 ours=$4 asn=$5 vpc=$6 addr
  addr=$(docker exec "$(node "$n")" ip -4 -o addr show dev eth1 | awk '{print $4}' | cut -d/ -f1)
  docker exec -i "$(node "$n")" sh -c 'umask 077; mkdir -p /etc/swanctl/conf.d; cat > /etc/swanctl/conf.d/lab.conf' <<CONF
connections {
  lab {
    version = 2
    local_addrs = $addr
    remote_addrs = 100.64.0.2
    proposals = aes256-sha256-modp2048
    dpd_delay = 10s
    local { auth = psk
            id = $addr }
    remote { auth = psk
             id = 100.64.0.2 }
    children {
      lab {
        local_ts = 0.0.0.0/0
        remote_ts = 0.0.0.0/0
        if_id_in = $ifid
        if_id_out = $ifid
        esp_proposals = aes256-sha256-modp2048
        start_action = trap
      }
    }
  }
}
secrets {
  ike-lab {
    id-1 = $addr
    id-2 = 100.64.0.2
    secret = "$psk"
  }
}
CONF
  docker exec "$(node "$n")" sh -c "
    ip link show xfrm$ifid >/dev/null 2>&1 || ip link add xfrm$ifid type xfrm dev eth1 if_id $ifid
    ip addr replace $inside dev xfrm$ifid; ip link set xfrm$ifid mtu 1400 up
    mkdir -p /etc/strongswan.d
    printf 'charon {\n  install_routes = no\n}\n' > /etc/strongswan.d/lab.conf
    if ! swanctl --stats >/dev/null 2>&1; then
      ipsec start >/dev/null 2>&1 || for c in /usr/libexec/ipsec/charon /usr/lib/strongswan/charon; do
        [ -x \$c ] && { \$c >/dev/null 2>&1 & break; }; done
      for _ in 1 2 3 4 5 6 7 8 9 10; do swanctl --stats >/dev/null 2>&1 && break; sleep 0.5; done
    fi
    swanctl --load-all --clear >/dev/null" || return 1
  docker exec "$(node "$n")" vtysh -c 'configure terminal' -c "router bgp $asn" -c 'no bgp ebgp-requires-policy' \
    -c "neighbor $ours remote-as $POP_ASN" \
    -c 'address-family ipv4 unicast' -c "network $vpc" -c 'end' >/dev/null
}

echo "-- simulated cloud gateways"
if cloud_gateway cloud-aws 1 169.254.100.1/30 169.254.100.2 64512 10.100.0.0/16 &&
  cloud_gateway cloud-azure 2 169.254.100.5/30 169.254.100.6 65515 10.101.0.0/16; then
  ok "AWS and Azure gateways configured"
else
  bad "could not configure the simulated gateways"
  exit 1
fi

echo "-- cloud circuits"
mk() { api POST "/customers/$cid/circuits" "$1" | jq -r '.id // empty'; }
aws=$(mk "$(jq -n --arg k "$psk" '{name: "lab AWS", kind: "cloud", provider: "aws", region: "us-east-1",
  peer_address: "100.64.10.2", peer_asn: 64512, inside_cidr: "169.254.100.0/30", psk: $k,
  cloud_prefixes: ["10.100.0.0/16"], class_name: "business", bandwidth_mbps: 50}')")
azure=$(mk "$(jq -n --arg k "$psk" '{name: "lab Azure", kind: "cloud", provider: "azure", region: "eastus",
  peer_address: "100.64.11.2", peer_asn: 65515, inside_cidr: "169.254.100.4/30", psk: $k,
  cloud_prefixes: ["10.101.0.0/16"], bandwidth_mbps: 50}')")
if [[ -n $aws && -n $azure ]]; then ok "created circuits $aws (AWS) and $azure (Azure)"; else bad "could not create the circuits"; exit 1; fi
status_of() { api GET "/customers/$cid/circuits" | jq -r --argjson i "$1" '.[] | select(.id == $i) | .status'; }
both_up() { [[ $(status_of "$aws") == up && $(status_of "$azure") == up ]]; }
t0=$SECONDS
if wait_for 120 both_up; then
  ok "both circuits up (IPsec and BGP) $((SECONDS - t0)) s after they were created"
else
  bad "circuits not up: $(api GET "/customers/$cid/circuits" | jq -c '[.[] | {name, status, ike, bgp}]')"
  docker exec "$(node pop-miami)" swanctl --list-sas 2>&1 | grep -E 'ESTABLISHED|CONNECTING|INSTALLED' | head -4 | sed 's/^/      /'
fi
note "$(api GET "/customers/$cid/circuits" | jq -r --argjson i "$aws" '.[] | select(.id == $i) |
  "AWS: \(.status), BGP \(.bgp), routes \(.routes | join(" "))"')"
learned() { docker exec "$(node site-a)" ip route show 10.100.0.0/16 | grep -q .; }
if wait_for 30 learned; then ok "site-a learned the AWS VPC route from the PoP"; else bad "site-a has no route to 10.100.0.0/16"; fi
if docker exec "$(node lan-a)" ping -c 5 -i 0.2 -W 1 -q 10.100.0.1 | grep -q ' 0% packet loss'; then
  ok "lan-a reaches the AWS VPC (10.100.0.1) through the PoP"
else
  bad "lan-a cannot reach the AWS VPC"
fi
if docker exec "$(node site-a)" nft list ruleset 2>/dev/null | grep -q '10.100.0.0/16.*meta mark set 0x0*102'; then
  ok "site-a puts traffic for AWS in business"
else
  note "AWS traffic class not seen in site-a's classifier"
fi

echo "-- cloud router"
c2c() { docker exec "$(node cloud-aws)" ping -c 3 -i 0.2 -W 1 -q -I 10.100.0.1 10.101.0.1 | grep -q ' 0% packet loss'; }
if wait_for 30 c2c; then ok "AWS reaches Azure through the PoP"; else bad "AWS cannot reach Azure through the PoP"; fi

echo "-- elastic bandwidth"
api PATCH "/customers/$cid/circuits/$aws" '{"bandwidth_mbps": 20}' >/dev/null
shaped() { docker exec "$(node pop-miami)" tc qdisc show dev "vc$aws" 2>/dev/null | grep -Eq '(bandwidth|rate) 20Mbit'; }
if wait_for 30 shaped; then ok "20 Mbps reached the PoP's shaper on vc$aws"; else bad "shaper on vc$aws: $(docker exec "$(node pop-miami)" tc qdisc show dev "vc$aws" 2>&1 | head -1)"; fi
note "charges so far: $(api GET "/customers/$cid/circuits/$aws/charges" | jq -c '[.segments[] | {mbps, hours}]')"

echo "-- layer 2 circuit"
a=$(sql "SELECT id FROM sites WHERE name = 'site-a'")
b=$(sql "SELECT id FROM sites WHERE name = 'site-b'")
l2=$(mk "$(jq -n --arg a "$a" --arg b "$b" '{name: "lab L2", kind: "site", a_site_id: $a, b_site_id: $b,
  a_vlan: 100, b_vlan: 200, bandwidth_mbps: 20}')")
l2ping() { docker exec "$(node lan-a)" ping -c 3 -i 0.2 -W 1 -q 172.16.100.20 | grep -q ' 0% packet loss'; }
if [[ -n $l2 ]] && wait_for 60 l2ping; then
  ok "VLAN 100 at site A and VLAN 200 at site B are one segment (lan-a pings lan-b)"
else
  bad "layer 2 circuit: lan-a cannot reach 172.16.100.20"
  docker exec "$(node site-a)" ip -d link show "vx${l2:-0}" 2>&1 | head -3 | sed 's/^/      /'
fi
# Loss is over the last minute, so probes sent while the far end was still
# being set up count until they age out.
l2view() { api GET "/customers/$cid/circuits" | jq -r --argjson i "$l2" '.[] | select(.id == $i) | "\(.status) \(.rtt_ms) \(.loss_pct)"'; }
l2clean() { read -r st _ loss <<<"$(l2view)" && [[ $st == up && $loss != null ]] && awk -v l="$loss" 'BEGIN { exit !(l < 5) }'; }
if wait_for 90 l2clean; then
  read -r _ rtt loss <<<"$(l2view)"
  ok "layer 2 circuit up and monitored: round trip $rtt ms, $loss% loss over the last minute"
else
  bad "layer 2 circuit status, round trip, loss: $(l2view)"
fi

echo "-- clean up"
for c in $aws $azure $l2; do api DELETE "/customers/$cid/circuits/$c" >/dev/null; done
gone() { ! docker exec "$(node pop-miami)" ip link show "vc$aws" >/dev/null 2>&1; }
if wait_for 30 gone; then ok "deleted circuits are torn down at the PoP"; else bad "vc$aws still on the PoP after delete"; fi
exit "$fail"
