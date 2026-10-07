#!/usr/bin/env bash
# make demo: the acceptance test (CLAUDE.md section 5), end to end on the lab
# host. Each step prints what to look at in the portal. DEMO_PAUSE=1 waits
# for Enter between steps, for a live audience. Voice, business and bulk
# traffic runs between the sites through steps 2 to 5 (make traffic), so the
# portal shows real load; DEMO_TRAFFIC=0 leaves it off.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
results=()
pause() { [[ ${DEMO_PAUSE:-0} == 1 ]] && read -r -p "   Press Enter for the next step... " _ </dev/tty; return 0; }
run() {
  local n=$1 title=$2 look=$3 script=$4
  printf '\n\033[1m%s. %s\033[0m\n   In the portal: %s\n' "$n" "$title" "$look"
  if "$script"; then results+=("ok    $n. $title"); else results+=("FAIL  $n. $title"); fail=1; fi
  pause
}

printf '\033[1m1. Start\033[0m: two sites, one PoP, three paths, three classes\n'
lab/scripts/up.sh || exit 1
if lab/scripts/check-routing.sh; then results+=("ok    1. Start: everything green"); else results+=("FAIL  1. Start"); fail=1; fi
printf '   In the portal: Overview shows both sites and every path green.\n'
traffic() { [[ ${DEMO_TRAFFIC:-1} == 1 ]] && make -s "$1"; return 0; }
trap 'traffic traffic-stop >/dev/null' EXIT
traffic traffic
pause

run 2-4 "Brownout, hard cut and recovery" \
  "Decisions shows voice moving to Carrier B before its 1% loss SLA with the forecast reason, the BFD failover, and the moves back after the hold time." \
  lab/ci/checks/m4-steering.sh
run 5 "Storm Mode" \
  "The coral Storm Mode marker on site-a; voice and business on satellite (business only, on GEO), bulk paused; then all reversed." \
  lab/ci/checks/m5-storm.sh
# Metering checks the 95th percentile against a known 20 Mbit/s, so it runs alone.
traffic traffic-stop
run 6 "Metering" \
  "Metering shows the 95th percentile, commit and burst per carrier link; the carrier view shows one carrier's links and the CSV matches." \
  lab/ci/checks/m6-metering.sh
run 7 "Controller outage" \
  "The agents lose the controller; they keep forwarding and fail over on BFD, then reconcile. The live controller and portal stay up." \
  lab/ci/checks/m7-outage.sh
run + "AI insights and Ask your network" \
  "Insights shows the example hurricane warning for site-a; Ask answers from the decision log." \
  lab/ci/checks/m8-ai.sh

printf '\n\033[1mDemo summary\033[0m\n'
printf '%s\n' "${results[@]}"
exit $fail
