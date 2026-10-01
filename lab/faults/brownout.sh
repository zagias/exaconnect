#!/usr/bin/env bash
# Ramps loss on one underlay from its current value to <loss%> over <ramp-seconds>.
#   brownout.sh carrier-a 3 180      (demo step 2)
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
[[ $# == 3 ]] || { echo "usage: $0 <link> <loss%> <ramp-seconds>" >&2; exit 2; }
check_link "$1"
read -r _ _ from < <(current "$1")
ramp "$1" loss "$from" "$2" "$3"
