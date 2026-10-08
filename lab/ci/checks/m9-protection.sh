#!/usr/bin/env bash
# Step 5 of ExaConnect Fabric (ADR 0012):
#  - a resilient pair to "AWS": two tunnels to two gateway addresses; with
#    the first tunnel down, lan-a still reaches the VPC over the second
#  - the encryption report shows every tunnel encrypted and how
#  - DDoS protection at the PoP: a burst of new connections from the
#    internet gets the source blocked; the block list stops a source outright
# The pre-shared key is made for this run and never printed.
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
cid=$(customer_id)
psk="k$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 30)"
POP_ASN=65000
PUBLIC=100.64.0.2
IX=100.64.0.1

cleanup() {
  for c in $(api GET "/customers/$cid/circuits" | jq -r '.[] | select(.name | startswith("lab ")) | .id'); do
    api DELETE "/customers/$cid/circuits/$c" >/dev/null
  done
  for b in $(api GET /admin/protection | jq -r '.blocklist[] | select(.reason | startswith("lab")) | .id'); do
    api DELETE "/admin/protection/blocklist/$b" >/dev/null
  done
  docker exec "$(node pop-miami)" nft flush set ip exa_inet auto_block 2>/dev/null || true
}
cleanup

echo "-- a resilient pair to the simulated AWS"
# Two route-based tunnels from cloud-aws, from 100.64.10.2 (if_id 1) and 100.64.10.3 (if_id 3).
docker exec "$(node cloud-aws)" ip addr replace 100.64.10.3/24 dev eth1
conn() { # conn <name> <local address> <if_id>
  cat <<CONF
  $1 {
    version = 2
    local_addrs = $2
    remote_addrs = $PUBLIC
    proposals = aes256-sha256-modp2048
    dpd_delay = 10s
    local { auth = psk
            id = $2 }
    remote { auth = psk
             id = $PUBLIC }
    children {
      $1 {
        local_ts = 0.0.0.0/0
        remote_ts = 0.0.0.0/0
        if_id_in = $3
        if_id_out = $3
        esp_proposals = aes256-sha256-modp2048
        start_action = trap
      }
    }
  }
CONF
}
{
  echo "connections {"
  conn lab 100.64.10.2 1
  conn lab2 100.64.10.3 3
  echo "}"
  echo "secrets {"
  for a in 100.64.10.2 100.64.10.3; do
    printf '  ike-%s {\n    id-1 = %s\n    id-2 = %s\n    secret = "%s"\n  }\n' "${a//./-}" "$a" "$PUBLIC" "$psk"
  done
  echo "}"
} | docker exec -i "$(node cloud-aws)" sh -c 'umask 077; mkdir -p /etc/swanctl/conf.d; cat > /etc/swanctl/conf.d/lab.conf'
docker exec "$(node cloud-aws)" sh -c "
  for t in '1 169.254.100.1/30' '3 169.254.100.9/30'; do
    set -- \$t
    ip link show xfrm\$1 >/dev/null 2>&1 || ip link add xfrm\$1 type xfrm dev eth1 if_id \$1
    ip addr replace \$2 dev xfrm\$1; ip link set xfrm\$1 mtu 1400 up
  done
  mkdir -p /etc/strongswan.d
  printf 'charon {\n  install_routes = no\n}\n' > /etc/strongswan.d/lab.conf
  if ! swanctl --stats >/dev/null 2>&1; then
    ipsec start >/dev/null 2>&1 || for c in /usr/libexec/ipsec/charon /usr/lib/strongswan/charon; do
      [ -x \$c ] && { \$c >/dev/null 2>&1 & break; }; done
    for _ in 1 2 3 4 5 6 7 8 9 10; do swanctl --stats >/dev/null 2>&1 && break; sleep 0.5; done
  fi
  swanctl --load-all --clear >/dev/null" || bad "could not configure the AWS gateway pair"
docker exec "$(node cloud-aws)" vtysh -c 'configure terminal' -c 'router bgp 64512' -c 'no bgp ebgp-requires-policy' \
  -c "neighbor 169.254.100.2 remote-as $POP_ASN" -c "neighbor 169.254.100.10 remote-as $POP_ASN" \
  -c 'address-family ipv4 unicast' -c 'network 10.100.0.0/16' -c 'end' >/dev/null

pair=$(api POST "/customers/$cid/circuits" "$(jq -n --arg k "$psk" '{name: "lab AWS pair", kind: "cloud",
  provider: "aws", region: "us-east-1", peer_address: "100.64.10.2", inside_cidr: "169.254.100.0/30",
  secondary_peer_address: "100.64.10.3", secondary_inside_cidr: "169.254.100.8/30", peer_asn: 64512, psk: $k,
  cloud_prefixes: ["10.100.0.0/16"], bandwidth_mbps: 50}')" | jq -r '.id // empty')
[[ -n $pair ]] || { bad "could not create the resilient circuit"; exit 1; }
tunnels() { api GET "/customers/$cid/circuits" | jq -r --argjson i "$pair" '.[] | select(.id == $i) | [.tunnels[].status] | join(" ")'; }
both() { [[ $(tunnels) == "up up" ]]; }
if wait_for 120 both; then ok "both tunnels of the pair up (IPsec and BGP)"; else bad "pair tunnels: $(tunnels)"; fi
vpc() { docker exec "$(node lan-a)" ping -c 3 -i 0.2 -W 1 -q 10.100.0.1 | grep -q ' 0% packet loss'; }
if wait_for 30 vpc; then ok "lan-a reaches the AWS VPC over the pair"; else bad "lan-a cannot reach 10.100.0.1"; fi

echo "-- encryption report"
rep=$(api GET "/customers/$cid/encryption")
note "summary: $(jq -c .summary <<<"$rep")"
rows=$(jq -c --argjson i "$pair" '[.circuits[] | select(.id == $i)]' <<<"$rep")
if [[ $(jq -r '[.[] | .status] | join(" ")' <<<"$rows") == "encrypted encrypted" ]] &&
  jq -e 'all(.[]; (.ike_cipher | test("AES")) and (.esp_cipher | test("AES")) and (.notes | length == 0))' <<<"$rows" >/dev/null; then
  ok "both tunnels encrypted: IKE $(jq -r '.[0].ike_cipher' <<<"$rows"), ESP $(jq -r '.[0].esp_cipher' <<<"$rows")"
else
  bad "circuit encryption rows: $rows"
fi
if jq -e '[.paths[] | select(.status == "down")] | length == 0' <<<"$rep" >/dev/null && (($(jq '.paths | length' <<<"$rep") >= 6)); then
  ok "every site path is WireGuard-encrypted ($(jq -r '[.paths[] | .status] | group_by(.) | map("\(length) \(.[0])") | join(", ")' <<<"$rep"))"
else
  bad "path encryption: $(jq -c '[.paths[] | {site, path, status}]' <<<"$rep")"
fi

echo "-- the first tunnel fails"
docker exec -d "$(node lan-a)" sh -c 'ping -c 300 -i 0.2 -W 1 10.100.0.1 > /tmp/pair-ping.txt 2>&1'
sleep 3
docker exec "$(node cloud-aws)" ip link set xfrm1 down
primary_down() { [[ $(tunnels) == "down up" || $(tunnels) == "provisioning up" ]]; }
if wait_for 60 primary_down; then ok "the portal shows the first tunnel down and the second up"; else bad "pair tunnels after the cut: $(tunnels)"; fi
st=$(api GET "/customers/$cid/circuits" | jq -r --argjson i "$pair" '.[] | select(.id == $i) | .status')
if [[ $st == up ]]; then ok "the circuit stays up on one tunnel"; else bad "circuit status with one tunnel: $st"; fi
# The 60-second ping run ends with its summary line.
finished() { docker exec "$(node lan-a)" grep -q 'packets transmitted' /tmp/pair-ping.txt; }
wait_for 90 finished || note "the ping run did not finish"
read -r tx rx <<<"$(docker exec "$(node lan-a)" sh -c "grep -Eo '[0-9]+ packets transmitted, [0-9]+ received' /tmp/pair-ping.txt" | awk '{print $1, $4}')"
lost_s=$(awk -v t="${tx:-0}" -v r="${rx:-0}" 'BEGIN { printf "%.1f", (t - r) * 0.2 }')
if [[ -n $tx ]] && awk -v l="$lost_s" 'BEGIN { exit !(l <= 35) }'; then
  ok "lan-a kept reaching the VPC: $rx of $tx pings answered (about $lost_s s lost; the BGP hold time is 30 s)"
else
  bad "traffic over the pair during the cut: ${rx:-?} of ${tx:-?} answered"
fi
docker exec "$(node cloud-aws)" ip link set xfrm1 up
if wait_for 90 both; then ok "the first tunnel recovers"; else note "after restore: $(tunnels)"; fi

echo "-- DDoS protection at the PoP"
reach() { docker exec "$(node ix)" ping -c 2 -i 0.2 -W 1 -q "$PUBLIC" | grep -q ' 0% packet loss'; }
if reach; then ok "the internet reaches the PoP's public address"; else bad "ix cannot ping $PUBLIC before the test"; fi
# A burst of new TCP connections from one source, well over 50 a second.
docker exec "$(node ix)" bash -c "for i in \$(seq 400); do timeout 0.05 bash -c 'echo > /dev/tcp/$PUBLIC/9' 2>/dev/null & done; wait" || true
prot() { api GET /admin/protection; }
blocked() { prot | jq -e --arg a "$IX" 'any(.auto_blocked[]; .address == $a) and .dropped.flood > 0' >/dev/null; }
if wait_for 30 blocked; then
  ok "the flooding source was blocked automatically ($(prot | jq -c '.dropped'))"
else
  bad "no automatic block: $(prot | jq -c '{dropped, auto_blocked}')"
fi
if ! reach; then ok "the blocked source can't reach the public address"; else bad "the blocked source still reaches $PUBLIC"; fi
docker exec "$(node pop-miami)" nft flush set ip exa_inet auto_block
if wait_for 10 reach; then ok "lifting the block lets it back in"; else bad "still blocked after flushing"; fi
cust=$(api GET "/customers/$cid/internet" | jq -c .protection)
if jq -e '.dropped.flood > 0 and (tostring | contains("100.64.0.1") | not)' <<<"$cust" >/dev/null; then
  ok "customers see the drop counts, not the addresses"
else
  bad "customer protection view: $cust"
fi

echo "-- the block list"
bid=$(api POST /admin/protection/blocklist "{\"prefix\": \"$IX/32\", \"reason\": \"lab block\", \"hours\": 1}" | jq -r '.id // empty')
unreachable() { ! reach; }
if [[ -n $bid ]] && wait_for 30 unreachable; then ok "a block list entry stops the source"; else bad "block list entry did not stop $IX"; fi
api DELETE "/admin/protection/blocklist/$bid" >/dev/null
if wait_for 30 reach; then ok "removing it lets the source back in"; else bad "still blocked after removing the entry"; fi

echo "-- clean up"
cleanup
docker exec "$(node cloud-aws)" sh -c 'ip link del xfrm3 2>/dev/null; ip addr del 100.64.10.3/24 dev eth1 2>/dev/null' || true
docker exec "$(node cloud-aws)" vtysh -c 'configure terminal' -c 'router bgp 64512' -c 'no neighbor 169.254.100.10' -c 'end' >/dev/null 2>&1 || true
gone() { ! docker exec "$(node pop-miami)" ip link show "vc$pair" >/dev/null 2>&1 && ! docker exec "$(node pop-miami)" ip link show "vc$((1000000 + pair))" >/dev/null 2>&1; }
if wait_for 30 gone; then ok "both tunnels torn down at the PoP"; else bad "pair tunnels still on the PoP after delete"; fi
exit "$fail"
