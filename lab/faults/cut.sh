#!/usr/bin/env bash
# Hard cut: 100 % loss both ways on one underlay. The interfaces stay up, as in
# a real carrier outage, so detection has to come from BFD and the probes.
#   cut.sh carrier-b      (demo step 3)
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
[[ $# == 1 ]] || { echo "usage: $0 <link>" >&2; exit 2; }
check_link "$1"
n=$(node "$1")
for dev in eth1 eth2 eth3; do
  docker exec "$n" tc qdisc replace dev "$dev" root netem loss 100%
done
read -r d j _ < <(current "$1")
echo "$d $j 100" > "$STATE_DIR/$1"
echo "$1 cut"
