#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# The box console (ADR 0046), end to end on a fresh box: a throwaway container from
# the lab node image runs `exa-agent run --console :443` with an internet port on
# Docker's bridge and an office port on its own network. deploy/uat/box-console.mjs
# then does what someone at the branch does: the setup code, the port wizard, joining
# with the box setup code the portal gives for a new location, status and
# diagnostics. Then the portal shows the box online and managed locally, and the
# controller follows the box's port choice. The box joins a test organisation of its
# own ("Set-up check box …", no hub), so the lab PoP and live organisations are
# untouched; deploy/uat/setup_users.py removes it on the next set-up check.
# The join code, setup code and admin password pass through a private file that is
# deleted straight after; none is printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"

echo "-- box console"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "${EXA_PUBLIC_HOST:-}")
[[ -n $host ]] || { skip "no public address (EXA_PUBLIC_HOST)"; exit 0; }
[[ -x $REPO/bin/lab/exa-agent ]] || { bad "no lab agent build in bin/lab"; exit 1; }

box=exa-box-check
lan=exa-box-lan
lan_ip=172.31.250.2
tag=$(openssl rand -hex 3)
org="Set-up check box $tag"
place="Branch box check $tag"
docker rm -f "$box" >/dev/null 2>&1
docker network rm "$lan" >/dev/null 2>&1
secrets=$(mktemp -d)
chmod 700 "$secrets"
trap 'docker rm -f "$box" >/dev/null 2>&1; docker network rm "$lan" >/dev/null 2>&1; rm -rf "$secrets"' EXIT

# 1. A test organisation with one location, connected: what the owner does in the portal.
cid=$(api POST /admin/organisations "{\"name\": \"$org\", \"products\": [\"connect\"], \"owner_email\": \"box-$tag@setup-check.example\", \"country\": \"JM\"}" | jq -r '.id // empty')
[[ -n $cid ]] || { bad "could not create the test organisation"; exit 1; }
loc=$(api POST "/orgs/$cid/locations" "{\"name\": \"$place\", \"city\": \"Kingston\", \"country\": \"JM\", \"timezone\": \"America/Jamaica\"}" | jq -r '.id // empty')
conn=$([[ -n $loc ]] && api POST "/orgs/$cid/locations/$loc/connect" '{"links": [{"carrier": "Flow", "underlay_type": "fibre", "commit_mbps": 100}], "lan_prefixes": ["172.31.250.0/24"]}')
site=$(jq -r '.site_id // empty' <<<"$conn")
name=$(jq -r '.name // empty' <<<"$conn")
if [[ -z $site || -z $name ]]; then bad "could not connect the test location"; exit 1; fi
ok "test location connected as $name"
url=$(jq -r '.install.public_agent_url // empty' <<<"$conn")
[[ -n $url ]] || url="https://$host:8443"
jq -r --arg u "$url" '.install | {c: $u, t: .token, f: .ca_fingerprint, n: .site} | @json' <<<"$conn" |
  tr -d '\n' | base64 -w0 | tr '+/' '-_' | tr -d '=' | sed 's/^/JOIN_CODE=EXA1./' >"$secrets/env"
echo >>"$secrets/env"
unset conn

# 2. A fresh box: internet port on Docker's bridge (eth0), office port on its own network (eth1).
docker network create --subnet 172.31.250.0/24 "$lan" >/dev/null 2>&1 || true
if ! docker run -d --name "$box" --hostname box-check --privileged --network bridge \
  --add-host "$host:host-gateway" -v "$REPO/bin/lab:/opt/exaconnect/bin:ro" exaconnect/node:dev >/dev/null ||
  ! docker network connect --ip "$lan_ip" "$lan" "$box"; then
  bad "could not start the test box"
  exit 1
fi
docker exec -d "$box" sh -c 'exec exa-agent run --console :443 >>/var/log/exa-agent.log 2>&1'
wan_ip=$(docker inspect -f '{{with index .NetworkSettings.Networks "bridge"}}{{.IPAddress}}/{{.IPPrefixLen}} {{.Gateway}}{{end}}' "$box")
wan_addr=${wan_ip% *} wan_gw=${wan_ip#* }
if wait_for 20 curl -sk -o /dev/null --max-time 3 "https://$lan_ip/api/state"; then
  ok "fresh box serves its console on the office port"
else
  bad "box console not answering: $(docker exec "$box" tail -n 3 /var/log/exa-agent.log | tr '\n' ' ' | cut -c1-300)"
  exit 1
fi
c=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "https://${wan_addr%/*}/api/state")
note "before set-up the internet port answers $c (no port is marked internet yet)"
code=$(docker exec "$box" exa-agent console-code | sed -n 's/^Box console setup code: //p')
[[ -n $code ]] || { bad "no setup code from exa-agent console-code"; exit 1; }
{
  echo "BOX_SETUP_CODE=$code"
  echo "PORTAL_EMAIL=$(grep '^EXA_ADMIN_EMAIL=' "$REPO/.env" | cut -d= -f2-)"
  echo "PORTAL_PW=$(grep '^EXA_ADMIN_PASSWORD=' "$REPO/.env" | cut -d= -f2-)"
} >>"$secrets/env"
unset code

# 3. The walk-through, in a browser on the office network.
base=mcr.microsoft.com/playwright:v1.48.0-noble
image=exaconnect/phone-uat:1.48.0
if ! docker image inspect "$image" >/dev/null 2>&1; then
  if ! printf 'FROM %s\nWORKDIR /uat\nRUN npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.48.0\n' "$base" |
    docker build -q -t "$image" - >/dev/null 2>&1; then
    bad "could not build the browser test image from $base"
    exit 1
  fi
fi
out=$(timeout 600 docker run --rm --network host --add-host "$host:127.0.0.1" --ipc host --env-file "$secrets/env" \
  -e BOX_BASE="https://$lan_ip" -e WAN_URL="https://${wan_addr%/*}" -e WAN_IFACE=eth0 -e WAN_MODE=static \
  -e WAN_ADDR="$wan_addr" -e WAN_GW="$wan_gw" -e LAN_IFACE=eth1 -e LAN_ADDR="$lan_ip/24" -e PING_TARGET="$wan_gw" \
  -e PORTAL_BASE="https://$host" -e SITE_NAME="$name" -e SITE_ID="$site" -e ORG_NAME="$org" \
  -e LOCATION_NAME="$place" \
  -v "$REPO/deploy/uat/box-console.mjs:/uat/box-console.mjs:ro" -w /uat "$image" node box-console.mjs 2>&1)
rm -f "$secrets/env"
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "box: ${line#PASS }" ;;
    "FAIL "*) bad "box: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
grep -q '^PASS ' <<<"$out" || bad "box console test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"

# 4. What the controller knows: the box, its console, its ports and the change made on it.
node=$(sql "SELECT id FROM nodes WHERE site_id = '$site'")
if [[ -n $node && $(sql "SELECT box->>'console' FROM nodes WHERE id = '$node'") == true ]]; then
  ok "the controller has the box's console report"
else
  bad "no box console report for $name"
fi
iface=$(sql "SELECT underlay_interface FROM links WHERE site_id = '$site' AND path = 'carrier-a'")
if [[ $iface == eth0 ]]; then ok "the link follows the port chosen on the box (eth0)"; else bad "link underlay is '$iface', not eth0"; fi
n=$(sql "SELECT count(*) FROM events WHERE node_id = '$node' AND kind = 'local_change'")
if ((${n:-0} >= 1)); then ok "changes made on the box reach the controller ($n)"; else bad "no local_change events from the box"; fi
v=$(sql "SELECT applied_version FROM nodes WHERE id = '$node'")
okv=$(sql "SELECT coalesce(apply_ok::text, 'none') FROM nodes WHERE id = '$node'")
if ((${v:-0} >= 1)) && [[ $okv == true ]]; then
  ok "the box applied its desired state (v$v)"
else
  bad "box desired state: version ${v:-none}, ok $okv: $(sql "SELECT left(apply_error, 200) FROM nodes WHERE id = '$node'")"
fi
if docker exec "$box" ip -4 addr show dev eth1 | grep -q "$lan_ip/24"; then ok "office port kept its address"; else bad "office port lost its address"; fi
if [[ $fail != 0 ]]; then
  note "agent log:"
  docker exec "$box" tail -n 15 /var/log/exa-agent.log 2>&1 | sed 's/^/      /'
fi

# The box goes; its organisation, location and site are removed by the next set-up check.
[[ -n $node ]] && api POST "/nodes/$node/revoke" >/dev/null
exit "$fail"
