#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# Jibsy Phone, the installable phone app, tested on this host as people use it
# (deploy/voice/uat/phone-app.mjs, ADR 0042): two test accounts in the demo business
# sign in at the app's own address, call each other with audio both ways, and go
# through contacts, recents, settings, offline and screen sizes. A third account has
# no extension. The accounts get fresh random passwords each run
# (deploy/voice/uat/live_users.py); the passwords pass through a private file that is
# deleted straight after and are never printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" --profile voice)

echo "-- phone app"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
phone=$(. deploy/public/site.env && echo "${EXA_PHONE_HOST:-}")
[[ -n $phone ]] || { skip "no phone app address (EXA_PHONE_HOST)"; exit 0; }
if [[ -z $("${COMPOSE[@]}" ps -q --status running freeswitch 2>/dev/null) ]]; then
  skip "phone system not running on this host"
  exit 0
fi

# The Playwright image has the browsers; the test needs the matching package.
base=mcr.microsoft.com/playwright:v1.48.0-noble
image=exaconnect/phone-uat:1.48.0
if ! docker image inspect "$image" >/dev/null 2>&1; then
  if ! printf 'FROM %s\nWORKDIR /uat\nRUN npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.48.0\n' "$base" |
    docker build -q -t "$image" - >/dev/null 2>&1; then
    bad "could not build the phone app test image from $base"
    exit 1
  fi
fi

users=$("${COMPOSE[@]}" exec -T controller python - <"$REPO/deploy/voice/uat/live_users.py" 2>/dev/null | tail -1)
if ! jq -e '.A_EXT and .B_EXT and .A_PW' >/dev/null 2>&1 <<<"$users"; then
  bad "could not set up the phone app's test accounts"
  exit 1
fi
ok "test accounts ready (extensions $(jq -r '"\(.A_EXT) and \(.B_EXT)"' <<<"$users"))"
secrets=$(mktemp -d)
chmod 700 "$secrets"
jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$users" >"$secrets/env"
unset users
out=$(timeout 900 docker run --rm --network host --add-host "$phone:127.0.0.1" --ipc host \
  --env-file "$secrets/env" -e UAT_BASE="https://$phone" -e UAT_SHOTS=/tmp/shots \
  -v "$REPO/deploy/voice/uat/phone-app.mjs:/uat/phone-app.mjs:ro" -w /uat "$image" \
  node phone-app.mjs 2>&1)
rm -rf "$secrets"
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "app: ${line#PASS }" ;;
    "FAIL "*) bad "app: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
if ! grep -q '^PASS ' <<<"$out"; then
  bad "phone app test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
elif ! grep -qE '^[0-9]+ passed, 0 failed' <<<"$out"; then
  note "last lines: $(tail -6 <<<"$out" | grep -v '^PASS' | tr '\n' ' ' | cut -c1-400)"
  fail=1
fi
exit "$fail"
