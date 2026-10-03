#!/usr/bin/env bash
# The public portal end to end: sign in at the public name, read every
# screen's data through the API, then open every screen in a real browser
# (the Playwright image) and report errors. Admin credentials come from .env
# and are never printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "$EXA_PUBLIC_HOST")
loc=(--resolve "$host:443:127.0.0.1" --max-time 20 -sS)
email=$(grep '^EXA_ADMIN_EMAIL=' .env | cut -d= -f2-)
pass=$(grep '^EXA_ADMIN_PASSWORD=' .env | cut -d= -f2-)

echo "-- API through https://$host"
token=$(jq -n --arg e "$email" --arg p "$pass" '{email:$e, password:$p}' |
  curl "${loc[@]}" -H 'Content-Type: application/json' -d @- "https://$host/api/v1/auth/login" | jq -r '.token // empty')
if [[ -n $token ]]; then ok "admin signs in at https://$host"; else bad "admin sign-in failed at https://$host"; exit 1; fi
pub() { curl "${loc[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" "https://$host/api/v1$1"; }
pubj() { curl "${loc[@]}" -H "Authorization: Bearer $token" "https://$host/api/v1$1"; }
cid=$(customer_id)
site=$(pubj /sites | jq -r '[.[] | select(.name == "site-a")][0].id // empty')
fails=()
for p in /auth/me /overview /sites "/sites/$site" /decisions /metering/links /insights /events /carriers /users /audit \
  /nodes /ai/status /applications/catalogue "/customers/$cid/rules" "/customers/$cid/applications" "/customers/$cid/paths"; do
  code=$(pub "$p")
  [[ $code == 200 ]] || fails+=("$p=$code")
done
if ((${#fails[@]} == 0)); then ok "every portal screen's data answers 200 through the public site"
else bad "API through the public site: ${fails[*]}"; fi
csv=$(curl "${loc[@]}" -o /dev/null -w '%{http_code} %{content_type}' -H "Authorization: Bearer $token" \
  "https://$host/api/v1/metering/settlement.csv")
note "settlement CSV: $csv"

echo "-- browser"
image=mcr.microsoft.com/playwright/python:v1.48.0-noble
if ! docker image inspect "$image" >/dev/null 2>&1 && ! docker pull -q "$image" >/dev/null 2>&1; then
  skip "Playwright image not available on this host"
  exit 0
fi
out=$(E2E_EMAIL=$email E2E_PASSWORD=$pass docker run --rm --network host --add-host "$host:127.0.0.1" \
  -e E2E_HOST="$host" -e E2E_EMAIL -e E2E_PASSWORD -v "$PWD/lab/ci/e2e:/e2e:ro" "$image" \
  python /e2e/portal.py 2>&1)
grep -E '^(ok|FAIL) ' <<<"$out"
if ! grep -q '^ok    signed in' <<<"$out"; then
  bad "browser run did not start: $(tail -3 <<<"$out" | tr '\n' ' ' | cut -c1-300)"
fi
grep -q '^FAIL ' <<<"$out" && fail=1
exit "$fail"
