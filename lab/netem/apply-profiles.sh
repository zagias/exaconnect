#!/usr/bin/env bash
# Puts every underlay back on its normal profile.
#   apply-profiles.sh            satellite as LEO (default)
#   SAT_PROFILE=geo apply-profiles.sh
#   LOSS_BOTH_WAYS=1 apply-profiles.sh   loss split across both directions
# The settings are saved, so later fault and restore scripts keep them.
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/netem.sh"
save_settings
echo "satellite profile: $SAT_PROFILE; loss both ways: $LOSS_BOTH_WAYS"
for l in "${LINKS[@]}"; do
  read -r d j loss < <(profile "$l")
  shape "$l" "$d" "$j" "$loss"
done
