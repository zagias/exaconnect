#!/usr/bin/env bash
# Bounded real FRR/WireGuard acceptance checks on an empty GitHub-hosted VM.
# Deliberately bypasses up.sh/release/public/voice and every non-routing check.
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ $(uname -s) == Linux && ${GITHUB_ACTIONS:-} == true && ${EXA_ROUTING_CI_RUNNER:-} == github-hosted &&
   ${EXA_EPHEMERAL_ROUTING_LAB:-} == 1 ]] || {
  echo "Refusing: requires explicit ephemeral GitHub-hosted Linux routing CI." >&2
  exit 2
}

export LAB_NAME=exa-routing-ci COMPOSE_PROJECT_NAME=exa-routing-ci CLAB_VERSION_CHECK=disable
marker=lab/.state/routing-ci-owned
topology=lab/.state/routing-ci.clab.yml
override=lab/.state/routing-ci.compose.yml
diagnostics=lab/.state/routing-ci-diagnostics
management=exa-routing-ci-mgmt
compose=(docker compose -f deploy/docker-compose.yml -f "$override" --env-file .env)

cleanup() {
  [[ -f $marker ]] || return 0
  set +e
  mkdir -p "$diagnostics"
  if [[ -f $override && -f .env ]]; then
    timeout 15s "${compose[@]}" logs --no-color --tail 100 controller proxy db >"$diagnostics/controller.log" 2>&1
    timeout 10s "${compose[@]}" ps >"$diagnostics/containers.txt" 2>&1
    timeout 10s docker compose -f deploy/docker-compose.yml --env-file .env exec -T db \
      psql -U exaconnect -d exaconnect -AtX -c \
      "SELECT time, class_name, kind, from_path, to_path, reason FROM decisions ORDER BY time DESC LIMIT 50" \
      >"$diagnostics/decisions.txt" 2>&1
  fi
  for name in site-a site-b pop-miami; do
    node="clab-$LAB_NAME-$name"
    {
      timeout 5s docker exec "$node" tail -n 100 /var/log/exa-agent.log
      timeout 5s docker exec "$node" vtysh -c 'show bfd peers json'
      timeout 5s docker exec "$node" vtysh -c 'show bgp summary json'
      timeout 5s docker exec "$node" ip -4 rule show
      timeout 5s docker exec "$node" ip -4 route show table all
      timeout 5s docker exec "$node" nft list ruleset
    } >"$diagnostics/$name.txt" 2>&1
  done
  if [[ -f $override && -f .env ]]; then
    timeout 30s "${compose[@]}" down --volumes --remove-orphans >"$diagnostics/compose-cleanup.txt" 2>&1
  fi
  if [[ -f $topology ]]; then
    timeout 45s sudo env CLAB_VERSION_CHECK=disable containerlab destroy -t "$topology" --cleanup >"$diagnostics/lab-cleanup.txt" 2>&1
  fi
  docker network rm "$management" >/dev/null 2>&1 || true
  if [[ -n $(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME") ||
        -n $(docker ps -aq --filter "name=^/clab-$LAB_NAME-") ]] ||
      docker network inspect "$management" >/dev/null 2>&1; then
    echo "FAIL isolated routing cleanup left owned resources; see diagnostics." >&2
    set -e
    return 1
  fi
  rm -f .env "$marker"
  set -e
}

if [[ ${1:-} == --cleanup ]]; then
  cleanup
  exit 0
fi
[[ ! -e .env && ! -e $marker ]] || { echo "Refusing an existing environment or lab run." >&2; exit 2; }
[[ -z $(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME") &&
   -z $(docker ps -aq --filter "name=^/clab-$LAB_NAME-") ]] || {
  echo "Refusing pre-existing CI-named containers." >&2; exit 2
}
if docker network inspect "$management" >/dev/null 2>&1; then
  echo "Refusing a pre-existing CI management network." >&2
  exit 2
fi
mkdir -p lab/.state "$diagnostics"
touch "$marker"
trap 'routing_code=$?; cleanup || routing_code=1; exit "$routing_code"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Reuse the production topology, changing only the CI name, management
# isolation and bind paths made absolute for the generated topology folder.
/usr/bin/python3 - <<'PY'
from pathlib import Path
import yaml

root = Path.cwd()
source = root / "lab/exaconnect.clab.yml"
topology = yaml.safe_load(source.read_text())
topology["name"] = "exa-routing-ci"
topology["mgmt"].update({"network": "exa-routing-ci-mgmt", "external-access": False})
topology["mgmt"]["driver-opts"] = {"com.docker.network.bridge.enable_ip_masquerade": "false"}
for node in topology["topology"]["nodes"].values():
    node["dns"] = {"servers": ["127.0.0.1"]}
    node["binds"] = [str((source.parent / bind.split(":", 1)[0]).resolve()) + ":" + bind.split(":", 1)[1]
                     for bind in node.get("binds", [])]
(root / "lab/.state/routing-ci.clab.yml").write_text(yaml.safe_dump(topology, sort_keys=False))
PY

cat >"$override" <<'YAML'
services:
  db:
    dns: [127.0.0.1]
  controller:
    dns: [127.0.0.1]
    environment:
      EXA_AGENT_PUBLIC_HOST: ""
      EXA_LLM_API_KEY: ""
      EXA_NHC_URL: ""
      EXA_USGS_URL: ""
      EXA_GDACS_URL: ""
      EXA_TSUNAMI_URLS: ""
      EXA_INTEGRATIONS_LIVE: "0"
      EXA_VOICE_TEST_MODE: "0"
  proxy:
    dns: [127.0.0.1]
    ports: !reset []
networks:
  default:
    internal: true
  exaconnect-mgmt:
    name: exa-routing-ci-mgmt
    external: true
YAML

# Only new disposable credentials are generated; they are never logged or
# uploaded. There are no external service keys in this job.
bash lab/scripts/init-env.sh
docker network create --internal --subnet 172.30.0.0/24 --gateway 172.30.0.1 "$management" >/dev/null
make lab-up LAB_TOPOLOGY=.state/routing-ci.clab.yml
"${compose[@]}" up -d --build --wait db controller proxy
for network in "$management" "${COMPOSE_PROJECT_NAME}_default"; do
  [[ $(docker network inspect "$network" -f '{{.Internal}}') == true ]] || {
    echo "Runtime network is not isolated: $network" >&2; exit 1
  }
done
proxy_ready=0
for _ in $(seq 1 30); do
  if docker exec "clab-$LAB_NAME-site-a" curl -fsSk --max-time 2 \
      https://172.30.0.5:8443/api/v1/ca.pem >/dev/null 2>&1; then
    proxy_ready=1; break
  fi
  sleep 1
done
[[ $proxy_ready == 1 ]] || { echo "FAIL isolated agent TLS proxy did not become ready" >&2; exit 1; }
make demo-seed

ready=0
for _ in $(seq 1 36); do
  if bash lab/scripts/check-routing.sh >"$diagnostics/initial-routing.txt" 2>&1; then
    ready=1; break
  fi
  sleep 5
done
[[ $ready == 1 ]] || { cat "$diagnostics/initial-routing.txt"; exit 1; }
cat "$diagnostics/initial-routing.txt"

# These are the existing acceptance checks, with real FRR BFD, WireGuard,
# netem faults, continuous marked packets, hold times and controller outage.
for check in m4-steering m5-storm m7-outage; do
  echo "=== isolated real routing: $check ==="
  bash "lab/ci/checks/$check.sh" | tee "$diagnostics/$check.txt"
done
echo "PASS real FRR/WireGuard/BFD routing, loss bounds, recovery and controller outage checks"
