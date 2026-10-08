#!/usr/bin/env bash
# Safer releases with rollback (ADR 0025). On the server, from the repository:
#
#   deploy/release/release.sh            build and start this commit, check it, roll back if unhealthy
#   deploy/release/release.sh rollback [commit]   go back to the previous (or a named) release
#   deploy/release/release.sh list       releases kept on this server
#
# A release: keep the running images under rel-<commit>, take an encrypted database
# backup (when /etc/exaconnect/backup.env is set up), build and start this commit
# (controller, agent gateway, public portal), then check it. If a check fails, the
# previous images come back and the release is marked rolled back. Rollback swaps
# code only: schema changes are additive, so the database stays; the backup taken
# before the release is there if data must go back too (deploy/backup/restore.sh).
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
REPO=$(pwd)
STATE=${EXA_RELEASE_STATE:-$REPO/lab/.state/releases}
KEEP=${EXA_RELEASE_KEEP:-5}
IMAGES=(exaconnect-controller exaconnect-web)
COMPOSE=(docker compose -f deploy/docker-compose.yml --env-file .env)
PUBLIC=(docker compose -f deploy/docker-compose.yml -f deploy/public/docker-compose.public.yml --env-file .env)
mkdir -p "$STATE"
umask 077

log() { printf '[release %s] %s\n' "$(date -u +%T)" "$*"; }
# sqlq <sql> [-v name=value ...]: one query, values passed as psql variables (:'name'), never spliced in.
sqlq() {
  local q=$1
  shift
  printf '%s\n' "$q" | "${COMPOSE[@]}" exec -T db psql -U exaconnect -d exaconnect -AtqX -v ON_ERROR_STOP=1 "$@" 2>/dev/null
}
# record: best effort; a release never fails because its log row could not be written.
record() { sqlq "$@" >/dev/null || true; }
public_on() { [[ -s deploy/public/site.env ]]; }
site_env() { if public_on; then set -a; # shellcheck disable=SC1091
  . deploy/public/site.env; set +a; fi; }

keep_running_as() {  # tag the images now running as rel-<commit>
  local img
  for img in "${IMAGES[@]}"; do
    docker image inspect "$img:latest" >/dev/null 2>&1 && docker tag "$img:latest" "$img:rel-$1"
  done
}

start() {  # start the given images (already tagged latest) or build this commit
  site_env
  local build=(--build)
  [[ ${1:-} == --no-build ]] && build=(--no-build)
  lab/scripts/init-env.sh >/dev/null
  "${COMPOSE[@]}" up -d "${build[@]}" --wait || return 1
  "${COMPOSE[@]}" up -d --force-recreate --no-deps --wait proxy || return 1
  if public_on; then
    "${PUBLIC[@]}" up -d "${build[@]}" --wait web || return 1
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
      ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ufw allow 443/udp >/dev/null &&
        ufw allow 8443/tcp >/dev/null
    fi
  fi
}

check() {  # the release is healthy: API, sign-in, portal, gateway, agents
  local why=() email pass token host since n now
  now=$(date +%s)
  curl -fsS --max-time 10 http://127.0.0.1:8000/healthz | grep -q ok || why+=("controller not healthy")
  email=$(grep '^EXA_ADMIN_EMAIL=' .env | cut -d= -f2-)
  pass=$(grep '^EXA_ADMIN_PASSWORD=' .env | cut -d= -f2-)
  token=$(jq -n --arg e "$email" --arg p "$pass" '{email:$e, password:$p}' |
    curl -fsS --max-time 10 -H 'Content-Type: application/json' -d @- http://127.0.0.1:8000/api/v1/auth/login 2>/dev/null |
    jq -r '.token // empty')
  if [[ -z $token ]]; then
    why+=("sign-in failed")
  elif ! curl -fsS --max-time 15 -o /dev/null -H "Authorization: Bearer $token" http://127.0.0.1:8000/api/v1/overview; then
    why+=("overview failed")
  fi
  if public_on; then
    host=$EXA_PUBLIC_HOST
    curl -fsS --max-time 15 -k --resolve "$host:443:127.0.0.1" "https://$host/healthz" | grep -q ok || why+=("public site not answering")
    curl -fsS --max-time 15 -k --resolve "$host:443:127.0.0.1" "https://$host/" | grep -q '<div id="root">' || why+=("portal not served")
    curl -fsS --max-time 15 -k --resolve "$host:8443:127.0.0.1" -o /dev/null "https://$host:8443/api/v1/ca.pem" || why+=("agent gateway not answering")
  fi
  # Agents that were reporting before the release must report again.
  since=$(cat "$STATE/started")
  n=0
  [[ ${EXA_RELEASE_SKIP_AGENTS:-0} == 1 ]] ||
    n=$(sqlq "SELECT count(*) FROM nodes WHERE cert_serial NOT LIKE 'revoked:%' AND last_seen > to_timestamp($since) - interval '5 minutes'")
  if [[ ${n:-0} -gt 0 ]]; then
    local back=0
    for _ in $(seq 1 30); do
      back=$(sqlq "SELECT count(*) FROM nodes WHERE last_seen > to_timestamp($now)")
      [[ ${back:-0} -ge $n ]] && break
      sleep 3
    done
    [[ ${back:-0} -ge $n ]] || why+=("only ${back:-0} of $n agents reported back")
  fi
  unset token pass
  printf '%s\n' "${why[@]}"
  [[ ${#why[@]} == 0 ]]
}

prune() {  # keep the last $KEEP release images
  local img
  for img in "${IMAGES[@]}"; do
    docker image ls "$img" --format '{{.CreatedAt}}\t{{.Tag}}' | grep -P '\trel-' | sort -r |
      tail -n "+$((KEEP + 1))" | cut -f2 | while read -r t; do docker rmi "$img:$t" >/dev/null 2>&1; done
  done
}

swap_to() {  # make rel-<commit> the latest images
  local img ok=0
  for img in "${IMAGES[@]}"; do
    docker image inspect "$img:rel-$1" >/dev/null 2>&1 && docker tag "$img:rel-$1" "$img:latest" && ok=1
  done
  [[ $ok == 1 ]]
}

release() {
  local commit prev backup="" problems id
  commit=$(git rev-parse --short=12 HEAD)
  prev=$(cat "$STATE/current" 2>/dev/null)
  date +%s >"$STATE/started"
  [[ -n $prev ]] && keep_running_as "$prev"

  if [[ -r /etc/exaconnect/backup.env ]] && docker ps --format '{{.Names}}' | grep -q '^exaconnect-db-1$'; then
    log "backing up the database before the release"
    if (set -a; # shellcheck disable=SC1091
        . /etc/exaconnect/backup.env; set +a
        EXA_BACKUP_COMPOSE_FILE=$REPO/deploy/docker-compose.yml deploy/backup/backup.sh) >"$STATE/backup.log" 2>&1; then
      backup=$(find "${EXA_BACKUP_DIR:-/var/backups/exaconnect}" -maxdepth 1 -name 'exaconnect-*.dump.enc' \
        -printf '%T@ %f\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2)
      log "backup ${backup:-written}"
    else
      log "backup failed (see $STATE/backup.log); releasing without one"
    fi
  else
    log "no backup before this release (deploy/backup is not set up on this server)"
  fi

  log "releasing $commit (previous ${prev:-none})"
  export EXA_BUILD_COMMIT=$commit
  if ! start; then
    problems="the new version did not start"
  else
    problems=$(check)
  fi
  id=$(sqlq "INSERT INTO releases (commit, previous, status, backup)
             VALUES (:'c', NULLIF(:'p', ''), 'deploying', NULLIF(:'b', '')) RETURNING id" \
    -v c="$commit" -v p="${prev:-}" -v b="$backup")

  if [[ -z $problems ]]; then
    echo "$commit" >"$STATE/current"
    keep_running_as "$commit"
    echo "$(date -u +%FT%TZ) $commit live" >>"$STATE/history"
    [[ -n $id ]] && record "UPDATE releases SET status = 'live', finished_at = now() WHERE id = $id"
    prune
    log "$commit is live"
    return 0
  fi

  log "release failed: ${problems//$'\n'/; }"
  echo "$(date -u +%FT%TZ) $commit failed: ${problems//$'\n'/; }" >>"$STATE/history"
  if [[ -n $prev ]] && swap_to "$prev"; then
    log "rolling back to $prev"
    export EXA_BUILD_COMMIT=$prev
    if start --no-build && check >/dev/null; then
      [[ -n $id ]] && record "UPDATE releases SET status = 'rolled_back', finished_at = now(), detail = :'d' WHERE id = $id" -v d="$problems"
      echo "$(date -u +%FT%TZ) $prev restored" >>"$STATE/history"
      log "$prev is live again"
    else
      [[ -n $id ]] && record "UPDATE releases SET status = 'failed', finished_at = now(), detail = :'d' WHERE id = $id" -v d="$problems; rollback also unhealthy"
      log "rollback to $prev is unhealthy too; look at: docker compose logs controller"
    fi
  else
    [[ -n $id ]] && record "UPDATE releases SET status = 'failed', finished_at = now(), detail = :'d' WHERE id = $id" -v d="$problems; no earlier release to go back to"
    log "no earlier release on this server to go back to"
  fi
  return 1
}

rollback() {
  local to=${1:-} cur
  cur=$(cat "$STATE/current" 2>/dev/null)
  if [[ -z $to ]]; then
    to=$(grep ' live$' "$STATE/history" 2>/dev/null | awk '{print $2}' | grep -vx "$cur" | tail -1)
  fi
  [[ -n $to ]] || { log "no earlier release to go back to"; return 1; }
  swap_to "$to" || { log "no images kept for $to (kept: $(list | tr '\n' ' '))"; return 1; }
  date +%s >"$STATE/started"
  log "rolling back from ${cur:-?} to $to"
  export EXA_BUILD_COMMIT=$to
  start --no-build || { log "$to did not start"; return 1; }
  local problems
  problems=$(check)
  record "INSERT INTO releases (commit, previous, status, kind, finished_at, detail)
          VALUES (:'c', NULLIF(:'p', ''), :'s', 'rollback', now(), :'d')" \
    -v c="$to" -v p="$cur" -v s="$([[ -z $problems ]] && echo live || echo failed)" -v d="$problems"
  echo "$to" >"$STATE/current"
  echo "$(date -u +%FT%TZ) $to live (rollback)" >>"$STATE/history"
  if [[ -n $problems ]]; then
    log "$to started but: ${problems//$'\n'/; }"
    return 1
  fi
  log "$to is live"
}

list() {
  docker image ls exaconnect-controller --format '{{.Tag}}' | grep '^rel-' | sed 's/^rel-//'
}

case "${1:-release}" in
  release) release ;;
  rollback) rollback "${2:-}" ;;
  list) echo "current: $(cat "$STATE/current" 2>/dev/null || echo none)"; echo "kept:"; list | sed 's/^/  /' ;;
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac
