#!/usr/bin/env bash
# The SBC on the lab host (started by lab/scripts/up.sh, make voice-up): nothing is published
# on the host, both containers run, FreeSWITCH is
# up with its two SIP profiles, and Kamailio refuses a caller that is not a
# listed carrier (the controller's address is not on the allow-list).
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" --profile voice)

echo "-- voice SBC"
if [[ -z $("${COMPOSE[@]}" ps -q kamailio 2>/dev/null) ]]; then
  skip "voice SBC not started on this host (make voice-up)"
  exit 0
fi
for s in kamailio freeswitch; do
  if [[ -n $("${COMPOSE[@]}" ps -q --status running "$s" 2>/dev/null) ]]; then ok "$s running"; else bad "$s not running"; fi
  # Locked down: no SIP or media port reaches the host until a carrier exists.
  published=$("${COMPOSE[@]}" ps --format '{{json .Publishers}}' "$s" 2>/dev/null | grep -oE '"PublishedPort":[1-9][0-9]*' || true)
  if [[ -z $published ]]; then ok "$s publishes no port on the host"; else bad "$s publishes a host port: $published"; fi
done
# shellcheck disable=SC2317,SC2329  # called through wait_for
up() { "${COMPOSE[@]}" exec -T freeswitch fs_cli -x status 2>/dev/null | grep -q '^UP'; }
if wait_for 120 up; then ok "FreeSWITCH up"; else bad "FreeSWITCH not up"; fi
profiles=$("${COMPOSE[@]}" exec -T freeswitch fs_cli -x "sofia status" 2>/dev/null | grep -cE '(internal|external)[[:space:]]+profile.*RUNNING')
if [[ $profiles == 2 ]]; then ok "FreeSWITCH SIP profiles running"; else bad "FreeSWITCH SIP profiles running: $profiles of 2"; fi
answer=$("${COMPOSE[@]}" exec -T controller python - kamailio 5060 <"$REPO/deploy/voice/sip_probe.py" 2>&1)
if [[ $answer == "SIP/2.0 403"* ]]; then ok "Kamailio refuses an unlisted caller (403)"; else bad "Kamailio answer to an unlisted caller: $answer"; fi
exit $fail
