#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# Quote to invoice on the live portal, tested as people use it (deploy/uat/commercial.mjs,
# ADR 0055): staff build, approve and send a quote; the owner accepts it; the order sets
# itself up; staff finish the manual items; usage is rated; the draft invoice carries it all.
# deploy/uat/commercial_users.py first removes what the last run made, then makes a fresh
# example organisation and owner and gives the staff test account a fresh random password;
# the passwords pass through a private file that is deleted straight after and never printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env")

echo "-- quote to invoice"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "${EXA_PUBLIC_HOST:-}")
[[ -n $host ]] || { skip "no public address (EXA_PUBLIC_HOST)"; exit 0; }

# The same test image as the phone app and set-up checks.
base=mcr.microsoft.com/playwright:v1.48.0-noble
image=exaconnect/phone-uat:1.48.0
if ! docker image inspect "$image" >/dev/null 2>&1; then
  if ! printf 'FROM %s\nWORKDIR /uat\nRUN npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.48.0\n' "$base" |
    docker build -q -t "$image" - >/dev/null 2>&1; then
    bad "could not build the browser test image from $base"
    exit 1
  fi
fi

users=$("${COMPOSE[@]}" exec -T controller python - <"$REPO/deploy/uat/commercial_users.py" 2>/dev/null | tail -1)
if ! jq -e '.STAFF_EMAIL and .STAFF_PW and .ORG_ID' >/dev/null 2>&1 <<<"$users"; then
  bad "could not set up the quote test's organisation and accounts"
  exit 1
fi
ok "last run's test organisation removed; fresh one ready"
secrets=$(mktemp -d)
chmod 700 "$secrets"
jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$users" >"$secrets/env"
unset users
out=$(timeout 600 docker run --rm --network host --add-host "$host:127.0.0.1" --ipc host \
  --env-file "$secrets/env" -e UAT_BASE="https://$host" \
  -v "$REPO/deploy/uat/commercial.mjs:/uat/commercial.mjs:ro" -w /uat "$image" \
  node commercial.mjs 2>&1)
rm -rf "$secrets"
# Remove the test organisation straight away, so no later check or staff screen meets it.
if "${COMPOSE[@]}" exec -T -e CLEAN_ONLY=1 controller python - <"$REPO/deploy/uat/commercial_users.py" 2>/dev/null |
  tail -1 | jq -e '.removed >= 1' >/dev/null; then
  ok "test organisation removed after the run"
else
  bad "could not remove the test organisation after the run"
fi
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "quote to invoice: ${line#PASS }" ;;
    "FAIL "*) bad "quote to invoice: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
if ! grep -q '^PASS ' <<<"$out"; then
  bad "quote test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
elif ! grep -qE '^[0-9]+ passed, 0 failed' <<<"$out"; then
  fail=1
fi
exit "$fail"
