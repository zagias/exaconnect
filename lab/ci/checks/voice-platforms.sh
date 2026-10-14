#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# Calling platforms (Microsoft Teams, Zoom Phone and the rest; ADR 0056), tested on this host
# as a voice admin uses them (deploy/voice/uat/platforms.mjs): "Platform check organisation"
# (made fresh by deploy/voice/uat/platform_users.py) joins Teams, adds a person with a number
# and an extension, reads the set-up guide, places test calls both ways, joins Zoom with its
# addresses, and makes a real browser-phone call to the Teams person, which the simulated link
# answers with an echo. The organisation is removed at the end. The owner's fresh random
# password passes through a private file that is deleted straight after; never printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" --profile voice)

echo "-- Calling platforms (Teams, Zoom and others)"
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

users=$("${COMPOSE[@]}" exec -T controller python - <"$REPO/deploy/voice/uat/platform_users.py" 2>/dev/null | tail -1)
if ! jq -e '.OWNER_EMAIL and .OWNER_PW and .OWNER_EXT and .NUMBER' >/dev/null 2>&1 <<<"$users"; then
  bad "could not set up the platform test organisation"
  exit 1
fi
ok "platform test organisation ready"
secrets=$(mktemp -d)
chmod 700 "$secrets"
jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$users" >"$secrets/env"
unset users
out=$(timeout 600 docker run --rm --network host --add-host "$host:127.0.0.1" --ipc host \
  --env-file "$secrets/env" -e UAT_BASE="https://$host" \
  -v "$REPO/deploy/voice/uat/platforms.mjs:/uat/platforms.mjs:ro" -w /uat "$image" \
  node platforms.mjs 2>&1)
rm -rf "$secrets"
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "platforms: ${line#PASS }" ;;
    "FAIL "*) bad "platforms: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
if ! grep -q '^PASS ' <<<"$out"; then
  bad "platform test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
elif ! grep -qE '^[0-9]+ passed, 0 failed' <<<"$out"; then
  fail=1
fi

# Leave nothing behind: the test organisation, its people and its numbers.
if "${COMPOSE[@]}" exec -T -e CLEAN_ONLY=1 controller python - <"$REPO/deploy/voice/uat/platform_users.py" >/dev/null 2>&1; then
  ok "platform test organisation removed"
else
  bad "could not remove the platform test organisation"
fi
exit "$fail"
