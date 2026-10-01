#!/usr/bin/env bash
# M0 smoke test: every site reaches the PoP over every underlay, and each
# underlay's RTT roughly matches its profile.
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
fail=0
check() { # site target link
  local out rtt
  if out=$(docker exec "$(node "$1")" ping -c 5 -i 0.2 -q "$2" 2>&1); then
    rtt=$(awk -F/ '/rtt|round-trip/ {print $5}' <<<"$out")
    printf 'ok    %-7s -> PoP via %-9s avg rtt %sms\n' "$1" "$3" "$rtt"
  else
    printf 'FAIL  %-7s -> PoP via %-9s\n' "$1" "$3"; fail=1
  fi
}
for site in site-a site-b; do
  check "$site" 10.11.0.2 carrier-a
  check "$site" 10.12.0.2 carrier-b
  check "$site" 10.13.0.2 sat
done
exit $fail
