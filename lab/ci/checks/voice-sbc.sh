#!/usr/bin/env bash
# The SBC on the lab host (started by lab/scripts/up.sh, make voice-up): both containers
# run, they publish only the voice ports in deploy/voice/ports.yml (Kamailio's SIP,
# FreeSWITCH's call audio range) and nothing else, FreeSWITCH is up with its two SIP
# profiles and the browser phone's sign-in, and Kamailio refuses a caller that is not a
# listed carrier (the controller's address is not on the allow-list), also on the public address.
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
  # Only the voice ports (deploy/voice/ports.yml) reach the host: Kamailio's SIP and the audio range.
  ports=$("${COMPOSE[@]}" ps --format '{{json .Publishers}}' "$s" 2>/dev/null | grep -oE '"PublishedPort":[1-9][0-9]*' |
    cut -d: -f2 | sort -un | tr '\n' ' ' || true)
  extra=""
  for p in $ports; do
    case $s:$p in
      kamailio:5060 | kamailio:5061) ;;
      freeswitch:*) ((p >= 16384 && p <= 16483)) || extra+="$p " ;;
      *) extra+="$p " ;;
    esac
  done
  if [[ -n $extra ]]; then bad "$s publishes a port outside the voice ports: $extra"
  elif [[ -n $ports ]]; then ok "$s publishes only its voice ports"
  else ok "$s publishes no port on the host"; fi
done
# shellcheck disable=SC2317,SC2329  # called through wait_for
up() { "${COMPOSE[@]}" exec -T freeswitch fs_cli -x status 2>/dev/null | grep -q '^UP'; }
if wait_for 120 up; then ok "FreeSWITCH up"; else bad "FreeSWITCH not up"; fi
profiles=$("${COMPOSE[@]}" exec -T freeswitch fs_cli -x "sofia status" 2>/dev/null | grep -cE '(internal|external)[[:space:]]+profile.*RUNNING')
if [[ $profiles == 2 ]]; then ok "FreeSWITCH SIP profiles running"; else bad "FreeSWITCH SIP profiles running: $profiles of 2"; fi
answer=$("${COMPOSE[@]}" exec -T controller python - kamailio 5060 <"$REPO/deploy/voice/sip_probe.py" 2>&1)
if [[ $answer == "SIP/2.0 403"* ]]; then ok "Kamailio refuses an unlisted caller (403)"; else bad "Kamailio answer to an unlisted caller: $answer"; fi
if "${COMPOSE[@]}" exec -T freeswitch fs_cli -x "verto status" 2>/dev/null | grep -q "8081.*RUNNING"; then
  ok "browser phone sign-in listening (compose network only)"
else
  bad "browser phone sign-in not listening"
fi
public_ip=$(grep -m1 '^EXA_VOICE_PUBLIC_IP=' "$REPO/.env" 2>/dev/null | cut -d= -f2 || true)
if [[ -n $public_ip ]]; then
  answer=$(python3 "$REPO/deploy/voice/sip_probe.py" "$public_ip" 5060 2>&1)
  if [[ $answer == "SIP/2.0 403"* ]]; then ok "Kamailio on the public address refuses an unlisted caller (403)"
  else bad "Kamailio on the public address answered an unlisted caller: $answer"; fi
fi
exit $fail
