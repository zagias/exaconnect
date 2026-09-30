#!/usr/bin/env bash
# Restores one underlay (or all, with no argument) to its normal profile.
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
links=("$@"); (( ${#links[@]} )) || links=("${LINKS[@]}")
for l in "${links[@]}"; do
  check_link "$l"
  read -r d j loss < <(profile "$l")
  shape "$l" "$d" "$j" "$loss"
done
