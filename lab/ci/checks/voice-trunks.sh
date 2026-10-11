#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# An organisation's own SIP trunks, tested on this host as a voice admin uses them
# (deploy/voice/uat/trunks.mjs, ADR 0044): "Trunk check organisation" (made fresh by
# deploy/voice/uat/trunk_users.py) adds three simulated trunks, gives a location a primary
# and a secondary and the organisation a default, and places test calls while trunks refuse
# calls or go down, checking each call fails over in order. The owner's fresh random
# password passes through a private file that is deleted straight after; never printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" --profile voice)

echo "-- SIP trunks per location"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "${EXA_PUBLIC_HOST:-}")
[[ -n $host ]] || { skip "no public address (EXA_PUBLIC_HOST)"; exit 0; }

# The same test image as the phone app check: Playwright's browsers plus its package.
base=mcr.microsoft.com/playwright:v1.48.0-noble
image=exaconnect/phone-uat:1.48.0
if ! docker image inspect "$image" >/dev/null 2>&1; then
  if ! printf 'FROM %s\nWORKDIR /uat\nRUN npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.48.0\n' "$base" |
    docker build -q -t "$image" - >/dev/null 2>&1; then
    bad "could not build the browser test image from $base"
    exit 1
  fi
fi

users=$("${COMPOSE[@]}" exec -T controller python - <"$REPO/deploy/voice/uat/trunk_users.py" 2>/dev/null | tail -1)
if ! jq -e '.OWNER_EMAIL and .OWNER_PW and .HEAD_EXT and .BRANCH_EXT' >/dev/null 2>&1 <<<"$users"; then
  bad "could not set up the trunk test organisation"
  exit 1
fi
ok "trunk test organisation ready"
secrets=$(mktemp -d)
chmod 700 "$secrets"
jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$users" >"$secrets/env"
unset users
out=$(timeout 600 docker run --rm --network host --add-host "$host:127.0.0.1" --ipc host \
  --env-file "$secrets/env" -e UAT_BASE="https://$host" \
  -v "$REPO/deploy/voice/uat/trunks.mjs:/uat/trunks.mjs:ro" -w /uat "$image" \
  node trunks.mjs 2>&1)
rm -rf "$secrets"
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "trunks: ${line#PASS }" ;;
    "FAIL "*) bad "trunks: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
if ! grep -q '^PASS ' <<<"$out"; then
  bad "trunk test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
elif ! grep -qE '^[0-9]+ passed, 0 failed' <<<"$out"; then
  fail=1
fi

# The edge proxy picked up the new trunk lists without a restart.
if [[ -n $("${COMPOSE[@]}" ps -q --status running kamailio 2>/dev/null) ]]; then
  sleep 8
  if "${COMPOSE[@]}" logs --since 15m kamailio 2>/dev/null | grep -q "exacarib: carrier lists reloaded"; then
    ok "edge proxy reloaded the carrier lists by itself"
  else
    bad "edge proxy did not reload the carrier lists"
    # What to look at: the watcher's process, the list each side sees, and the proxy's own words.
    note "proxy start: $("${COMPOSE[@]}" exec -T kamailio sh -c 'tr "\0" " " </proc/1/cmdline' 2>&1 | cut -c1-120)"
    note "list in proxy: $("${COMPOSE[@]}" exec -T kamailio md5sum /exacarib/kamailio/dispatcher.list 2>&1 | cut -c1-80)"
    note "list written: $("${COMPOSE[@]}" exec -T controller md5sum /data/kamailio/dispatcher.list 2>&1 | cut -c1-80)"
    note "sets loaded: $("${COMPOSE[@]}" exec -T kamailio kamcmd dispatcher.list 2>&1 | grep -c 'ID:')"
    note "reload by hand: $("${COMPOSE[@]}" exec -T kamailio kamcmd dispatcher.reload 2>&1 | tr '\n' ' ' | cut -c1-200)"
    note "address reload by hand: $("${COMPOSE[@]}" exec -T kamailio kamcmd permissions.addressReload 2>&1 | tr '\n' ' ' | cut -c1-200)"
    note "watcher says: $("${COMPOSE[@]}" logs kamailio 2>/dev/null | grep 'exacarib:' | tail -4 | tr '\n' ' ' | cut -c1-400)"
  fi
else
  skip "edge proxy not running on this host"
fi
exit "$fail"
