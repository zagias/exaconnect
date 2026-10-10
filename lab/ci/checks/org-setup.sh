#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# Organisation set-up on the live portal, tested as people use it (deploy/uat/org-setup.mjs,
# ADR 0043): an ExaCarib staff test account creates an organisation, its new owner sets it up
# (company, three locations, connecting one, inviting a colleague), and the colleague and
# staff each see the right screens. deploy/uat/setup_users.py first removes what the last run
# made and gives the staff account a fresh random password; the password passes through a
# private file that is deleted straight after and is never printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env")

echo "-- organisation set-up"
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

users=$("${COMPOSE[@]}" exec -T controller python - <"$REPO/deploy/uat/setup_users.py" 2>/dev/null | tail -1)
if ! jq -e '.STAFF_EMAIL and .STAFF_PW' >/dev/null 2>&1 <<<"$users"; then
  bad "could not set up the set-up test's staff account"
  exit 1
fi
ok "earlier test organisations removed; staff test account ready"
secrets=$(mktemp -d)
chmod 700 "$secrets"
jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$users" >"$secrets/env"
unset users
out=$(timeout 600 docker run --rm --network host --add-host "$host:127.0.0.1" --ipc host \
  --env-file "$secrets/env" -e UAT_BASE="https://$host" \
  -v "$REPO/deploy/uat/org-setup.mjs:/uat/org-setup.mjs:ro" -w /uat "$image" \
  node org-setup.mjs 2>&1)
rm -rf "$secrets"
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "set-up: ${line#PASS }" ;;
    "FAIL "*) bad "set-up: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
if ! grep -q '^PASS ' <<<"$out"; then
  bad "set-up test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
elif ! grep -qE '^[0-9]+ passed, 0 failed' <<<"$out"; then
  fail=1
fi
exit "$fail"
