#!/usr/bin/env bash
# Ramps round-trip delay on one underlay up by <extra-ms> over <ramp-seconds>.
#   latency-creep.sh carrier-a 150 120
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
[[ $# == 3 ]] || { echo "usage: $0 <link> <extra-ms> <ramp-seconds>" >&2; exit 2; }
check_link "$1"
read -r from _ _ < <(current "$1")
ramp "$1" delay "$from" "$(awk "BEGIN{print $from + $2}")" "$3"
