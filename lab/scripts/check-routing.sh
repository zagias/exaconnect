#!/usr/bin/env bash
# M1 check: WireGuard up on all three paths, BGP and BFD established, and
# site-to-site traffic flowing through the PoP.
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
fail=0
ok() { printf 'ok    %s\n' "$*"; }
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }

for n in site-a site-b; do
  for t in wg-a wg-b wg-sat; do
    age=$(docker exec "$(node "$n")" wg show "$t" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -n | tail -1)
    if [[ -n $age && $age != 0 ]]; then ok "$n $t handshake $(( $(date +%s) - age ))s ago"; else bad "$n $t no handshake"; fi
  done
done

for n in pop-miami site-a site-b; do
  est=$(docker exec "$(node "$n")" vtysh -c "show bgp summary json" 2>/dev/null |
        jq '[.ipv4Unicast.peers[]? | select(.state == "Established")] | length')
  want=3; [[ $n == pop-miami ]] && want=6
  if [[ ${est:-0} -ge $want ]]; then ok "$n BGP $est/$want established"; else bad "$n BGP ${est:-0}/$want established"; fi
  up=$(docker exec "$(node "$n")" vtysh -c "show bfd peers json" 2>/dev/null | jq '[.[] | select(.status == "up")] | length')
  if [[ ${up:-0} -ge $want ]]; then ok "$n BFD $up/$want up"; else bad "$n BFD ${up:-0}/$want up"; fi
done

ping_check() { # from to label
  if docker exec "$(node "$1")" ping -c 3 -i 0.2 -W 2 -q "$2" >/dev/null 2>&1; then ok "$3"; else bad "$3"; fi
}
ping_check lan-a 192.168.20.10 "lan-a -> lan-b (site-a to site-b via the PoP)"
ping_check lan-b 192.168.10.10 "lan-b -> lan-a"
ping_check lan-a 10.200.0.10 "lan-a -> lan-pop"
echo
docker exec "$(node lan-a)" traceroute -n -w 1 -q 1 192.168.20.10 2>/dev/null | sed 's/^/  /' || true
exit $fail
