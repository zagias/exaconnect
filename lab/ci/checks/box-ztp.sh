#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# Zero-touch installation (ADR 0050), end to end: a fresh box from the lab node image,
# with only an internet port and no set-up at all, calls home to the live portal.
# deploy/uat/box-ztp.mjs then does what ExaCarib staff do: find the box by its serial
# number in Operations > Boxes and assign it to a test organisation's location. The box
# must join Connect by itself, apply its desired state and show Online. Nobody types a
# setup code or opens the box console. The test organisation is removed by the next
# set-up check, and the box's row here at the end.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
# shellcheck source=lab/ci/box-lib.sh
source "$(dirname "$0")/../box-lib.sh"

echo "-- zero-touch box"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "${EXA_PUBLIC_HOST:-}")
[[ -n $host ]] || { skip "no public address (EXA_PUBLIC_HOST)"; exit 0; }
[[ -x $REPO/bin/lab/exa-agent ]] || { bad "no lab agent build in bin/lab"; exit 1; }

box=exa-box-ztp
tag=$(openssl rand -hex 3)
serial="ZTP-CHECK-${tag^^}"
docker rm -f "$box" >/dev/null 2>&1
secrets=$(mktemp -d)
chmod 700 "$secrets"
trap 'docker rm -f "$box" >/dev/null 2>&1; rm -rf "$secrets"; sql "DELETE FROM boxes WHERE serial = '"'$serial'"'" >/dev/null' EXIT

box_location "$tag" 172.31.251.0/24 "$host" || exit 1
sed -i '/^JOIN_CODE=/d' "$secrets/env" # zero-touch needs no code

# A fresh box, plugged in: it calls home to the live portal by itself.
if ! docker run -d --name "$box" --hostname box-ztp --privileged --network bridge \
  --add-host "$host:host-gateway" -e EXA_BOX_SERIAL="$serial" -e EXA_ZTP_URL="https://$host" \
  -v "$REPO/bin/lab:/opt/exaconnect/bin:ro" exaconnect/node:dev >/dev/null; then
  bad "could not start the zero-touch box"
  exit 1
fi
docker exec -d "$box" sh -c 'exec exa-agent run --console :443 >>/var/log/exa-agent.log 2>&1'
if wait_for 90 test -n "$(sql "SELECT 1 FROM boxes WHERE serial = '$serial' AND public_key IS NOT NULL")"; then
  ok "a fresh box calls home with its serial number and key"
else
  bad "the box never called home: $(docker exec "$box" grep -i 'zero-touch' /var/log/exa-agent.log | tail -n 3 | tr '\n' ' ' | cut -c1-400)"
  exit 1
fi

# Staff assign it in the portal; the box must join on its own.
{
  echo "SERIAL=$serial"
} >>"$secrets/env"
base=mcr.microsoft.com/playwright:v1.48.0-noble image=exaconnect/phone-uat:1.48.0
docker image inspect "$image" >/dev/null 2>&1 ||
  printf 'FROM %s\nWORKDIR /uat\nRUN npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.48.0\n' "$base" |
  docker build -q -t "$image" - >/dev/null 2>&1
out=$(timeout 600 docker run --rm --ipc host --env-file "$secrets/env" --network host --add-host "$host:127.0.0.1" \
  -e PORTAL_BASE="https://$host" -e SITE_NAME="$name" -e ORG_NAME="$org" -e LOCATION_NAME="$place" \
  -v "$REPO/deploy/uat/box-ztp.mjs:/uat/box-ztp.mjs:ro" -w /uat "$image" node box-ztp.mjs 2>&1)
rm -f "$secrets/env"
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "zero-touch: ${line#PASS }" ;;
    "FAIL "*) bad "zero-touch: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
grep -q '^PASS ' <<<"$out" || bad "zero-touch test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"

node=$(sql "SELECT id FROM nodes WHERE site_id = '$site'")
v=$(sql "SELECT applied_version FROM nodes WHERE id = '$node'")
okv=$(sql "SELECT coalesce(apply_ok::text, 'none') FROM nodes WHERE id = '$node'")
if [[ -n $node ]] && ((${v:-0} >= 1)) && [[ $okv == true ]]; then ok "the zero-touch box applied its desired state (v$v)"; else bad "zero-touch box state: node ${node:-none}, version ${v:-none}, ok $okv"; fi
n=$(sql "SELECT count(*) FROM events WHERE node_id = '$node' AND kind = 'local_change' AND detail->>'what' LIKE 'Joined Connect by zero-touch%'")
if ((${n:-0} >= 1)); then ok "the box reports it joined by zero-touch"; else bad "no zero-touch join event from the box"; fi
if [[ $(docker exec "$box" sh -c 'test -s /var/lib/exaconnect/console.json && echo yes || echo no' 2>/dev/null) == no ]]; then
  ok "nobody set the box console up: no admin password on the box"
fi
if [[ $fail != 0 ]]; then
  note "agent log:"
  docker exec "$box" tail -n 15 /var/log/exa-agent.log 2>&1 | sed 's/^/      /'
fi
[[ -n $node ]] && api POST "/nodes/$node/revoke" >/dev/null
exit "$fail"
