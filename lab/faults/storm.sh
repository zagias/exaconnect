#!/usr/bin/env bash
# Storm: cuts both terrestrial links in turn (demo step 5). Satellite survives.
#   storm.sh [seconds-between-cuts]   default 30
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
gap=${1:-30}
here="$(dirname "$0")"
echo "storm: cutting carrier-a"
"$here/cut.sh" carrier-a
echo "storm: cutting carrier-b in ${gap}s"
sleep "$gap"
"$here/cut.sh" carrier-b
echo "storm: both terrestrial links down; run faults/restore.sh to end it"
