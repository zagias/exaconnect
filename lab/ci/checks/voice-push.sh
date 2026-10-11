#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# Jibsy Phone rings when it's closed (ADR 0045), tested on this host end to end
# (deploy/voice/uat/push.mjs): a device turns ringing on, the app closes, a call comes
# in, the phone system sends the device an encrypted push message, the app's service
# worker shows the call, and opening it answers with audio both ways.
#
# Test browsers have no Apple or Google push service, so the message goes to a stand-in,
# http://push-check:8099 on the compose network (voice/push.py allows that one name),
# which saves what it gets for the test to decrypt. Runs after voice-phone-app.sh, with the
# same test accounts (deploy/voice/uat/live_users.py, fresh passwords, never printed).
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" --profile voice)

echo "-- phone app rings when closed"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
phone=$(. deploy/public/site.env && echo "${EXA_PHONE_HOST:-}")
[[ -n $phone ]] || { skip "no phone app address (EXA_PHONE_HOST)"; exit 0; }
if [[ -z $("${COMPOSE[@]}" ps -q --status running freeswitch 2>/dev/null) ]]; then
  skip "phone system not running on this host"
  exit 0
fi
image=exaconnect/phone-uat:1.48.0
docker image inspect "$image" >/dev/null 2>&1 || { bad "no phone test image (voice-phone-app.sh builds it)"; exit 1; }
net=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$("${COMPOSE[@]}" ps -q controller)" | awk '{print $1}')

users=$("${COMPOSE[@]}" exec -T controller python - <"$REPO/deploy/voice/uat/live_users.py" 2>/dev/null | tail -1)
if ! jq -e '.A_EXT and .B_EXT and .A_PW' >/dev/null 2>&1 <<<"$users"; then
  bad "could not set up the test accounts"
  exit 1
fi
work=$(mktemp -d)
chmod 700 "$work"
mkdir "$work/push"
chmod 777 "$work/push"
jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$users" >"$work/env"
unset users

# The stand-in push service: saves each message and its headers, answers 201 like the real ones.
docker rm -f exa-push-check >/dev/null 2>&1 || true
docker run -d --rm --name exa-push-check --network "$net" --network-alias push-check \
  -v "$work/push:/out" "$image" node -e '
    const http = require("http"), fs = require("fs");
    let n = 0;
    http.createServer((req, res) => {
      const parts = [];
      req.on("data", (c) => parts.push(c));
      req.on("end", () => {
        const id = req.url.slice(1).replace(/[^a-z0-9]/g, "") + "-" + String(++n).padStart(4, "0");
        fs.writeFileSync(`/out/${id}.json`, JSON.stringify(req.headers));
        fs.writeFileSync(`/out/${id}.bin`, Buffer.concat(parts));
        res.writeHead(201).end();
      });
    }).listen(8099);' >/dev/null
out=$(timeout 600 docker run --rm --network host --add-host "$phone:127.0.0.1" --ipc host \
  --env-file "$work/env" -e UAT_BASE="https://$phone" -e PUSH_DIR=/push -e UAT_SHOTS=/tmp/shots \
  -v "$work/push:/push:ro" -v "$REPO/deploy/voice/uat/push.mjs:/uat/push.mjs:ro" -w /uat "$image" \
  node push.mjs 2>&1)
docker rm -f exa-push-check >/dev/null 2>&1 || true
rm -rf "$work"
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "rings closed: ${line#PASS }" ;;
    "FAIL "*) bad "rings closed: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
if ! grep -q '^PASS ' <<<"$out"; then
  bad "closed-app test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
elif ! grep -qE '^[0-9]+ passed, 0 failed' <<<"$out"; then
  note "phone system says: $("${COMPOSE[@]}" logs --since 10m freeswitch 2>/dev/null | grep -E 'exacarib:|exa_ring' | tail -4 | tr '\n' ' ' | cut -c1-400)"
  fail=1
fi
exit "$fail"
