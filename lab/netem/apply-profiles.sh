#!/usr/bin/env bash
# Puts every underlay back on its normal profile.
#   apply-profiles.sh            satellite as LEO (default)
#   SAT_PROFILE=geo apply-profiles.sh
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/netem.sh"
for l in "${LINKS[@]}"; do
  read -r d j loss < <(profile "$l")
  shape "$l" "$d" "$j" "$loss"
done
