# shellcheck shell=bash
# shellcheck disable=SC2034  # fail is read by the scripts that source this file
# Helpers for lab checks. Sourced, not executed.
# shellcheck source=lab/netem/netem.sh
source "$(dirname "${BASH_SOURCE[0]}")/../netem/netem.sh"
set +e
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

fail=0
ok() { printf 'ok    %s\n' "$*"; }
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }
skip() { printf 'SKIP  %s\n' "$*"; }
note() { printf '      %s\n' "$*"; }

# sql "<query>": one value per line, fields separated by |.
sql() {
  docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" exec -T db \
    psql -U exaconnect -d exaconnect -AtX -c "$1" 2>/dev/null
}

# The path a class takes from a node right now, read from the kernel.
# path_of <node> <mark> <dst>
path_of() {
  docker exec "$(node "$1")" ip -o route get "$3" mark "$2" 2>/dev/null | grep -o 'dev wg-[a-z]*' | cut -d' ' -f2
}

# wait_for <seconds> <command...>: polls every 2 s until the command succeeds.
wait_for() {
  local until=$((SECONDS + $1))
  shift
  while ((SECONDS < until)); do
    "$@" && return 0
    sleep 2
  done
  return 1
}

# Moves (not holds) recorded for site-a/<class> since <epoch>.
moves_since() {
  sql "SELECT count(*) FROM decisions d JOIN sites s ON s.id = d.site_id
       WHERE s.name = 'site-a' AND d.class_name = '$1' AND d.kind <> 'hold'
         AND d.time > to_timestamp($2)"
}
