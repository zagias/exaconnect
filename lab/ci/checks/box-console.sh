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
# shellcheck source=lab/ci/box-lib.sh
source "$(dirname "$0")/../box-lib.sh"

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
docker rm -f "$box" >/dev/null 2>&1
docker network rm "$lan" >/dev/null 2>&1
secrets=$(mktemp -d)
chmod 700 "$secrets"
trap 'docker rm -f "$box" >/dev/null 2>&1; docker network rm "$lan" >/dev/null 2>&1; rm -rf "$secrets"' EXIT

# 1. A test organisation with one location, connected: what the owner does in the portal.
box_location "$tag" 172.31.250.0/24 "$host" || exit 1

# 2. A fresh box: internet port on Docker's bridge (eth0), office port on its own network (eth1).
docker network create --subnet 172.31.250.0/24 "$lan" >/dev/null 2>&1 || true
if ! docker run -d --name "$box" --hostname box-check --privileged --network bridge \
  --add-host "$host:host-gateway" -e EXA_ZTP_URL=off -v "$REPO/bin/lab:/opt/exaconnect/bin:ro" exaconnect/node:dev >/dev/null ||
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
if docker exec "$box" nft list table inet exa_box >/dev/null 2>&1; then ok "a fresh box has its own firewall"; else bad "a fresh box has no exa_box firewall"; fi
code=$(docker exec "$box" exa-agent console-code | sed -n 's/^Box console setup code: //p')
[[ -n $code ]] || { bad "no setup code from exa-agent console-code"; exit 1; }
echo "BOX_SETUP_CODE=$code" >>"$secrets/env"
unset code

# 3. The walk-through, in a browser on the office network.
box_walkthrough box --network host --add-host "$host:127.0.0.1" \
  -e BOX_BASE="https://$lan_ip" -e WAN_URL="https://${wan_addr%/*}" -e WAN_IFACE=eth0 -e WAN_MODE=static \
  -e WAN_ADDR="$wan_addr" -e WAN_GW="$wan_gw" -e LAN_IFACE=eth1 -e LAN_ADDR="$lan_ip/24" -e PING_TARGET="$wan_gw" \
  -e PORTAL_BASE="https://$host"

# 4. What the controller knows: the box, its console, its ports and the change made on it.
box_controller_checks eth0
# Cloud management (ADR 0049, 0051): the switches and the portal's ports are audited, and the box kept them.
n=$(sql "SELECT count(*) FROM audit_log WHERE action = 'box.managed' AND target = (SELECT name FROM nodes WHERE id = '$node')")
if ((${n:-0} >= 3)); then ok "managed-from switches from the portal and the box are audited ($n)"; else bad "box.managed audit rows: ${n:-0}, want 3"; fi
n=$(sql "SELECT count(*) FROM events WHERE node_id = '$node' AND kind = 'box_portal_applied'")
if ((${n:-0} >= 1)); then ok "the box reported keeping the ports set in the portal"; else bad "no box_portal_applied event from the box"; fi
if [[ $(sql "SELECT box_template_id IS NOT NULL FROM nodes WHERE id = '$node'") == t ]]; then
  ok "the box follows the template made from its ports"
else
  bad "the box does not follow a template"
fi
if [[ $(sql "SELECT box_network->'network'->'wan'->0->'dns'->>0 FROM nodes WHERE id = '$node'") == 9.9.9.9 ]]; then
  ok "the controller has the portal's port set-up"
else
  bad "no portal port set-up on the node"
fi
if docker exec "$box" ip -4 addr show dev eth1 | grep -q "$lan_ip/24"; then ok "office port kept its address"; else bad "office port lost its address"; fi
# Port changes reload the firewall and zero its counters: knock on the internet port once more.
curl -sk -o /dev/null --max-time 3 "https://${wan_addr%/*}/api/state" || true
fw=$(docker exec "$box" nft list table inet exa_box 2>&1)
if grep -q 'policy drop' <<<"$fw" && grep -Eq 'iifname "eth0" counter packets [1-9][0-9]* .*comment "inbound"' <<<"$fw"; then
  ok "the box's firewall drops what arrives on the internet port"
else
  bad "box firewall: $(grep -E 'policy|inbound' <<<"$fw" | tr -s ' \t\n' ' ' | cut -c1-200)"
fi
if [[ $fail != 0 ]]; then
  note "agent log:"
  docker exec "$box" tail -n 15 /var/log/exa-agent.log 2>&1 | sed 's/^/      /'
fi

# The box goes; its organisation, location and site are removed by the next set-up check.
[[ -n $node ]] && api POST "/nodes/$node/revoke" >/dev/null
exit "$fail"
