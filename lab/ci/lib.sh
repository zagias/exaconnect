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

# api <method> <path> [json]: calls the controller API as the lab admin. The
# password is read from .env on this host and never printed.
api() {
  local email pass token
  email=$(grep '^EXA_ADMIN_EMAIL=' "$REPO/.env" | cut -d= -f2-)
  pass=$(grep '^EXA_ADMIN_PASSWORD=' "$REPO/.env" | cut -d= -f2-)
  token=$(jq -n --arg e "$email" --arg p "$pass" '{email:$e, password:$p}' |
    curl -fsS -H 'Content-Type: application/json' -d @- http://127.0.0.1:8000/api/v1/auth/login | jq -r .token)
  curl -fsS -X "$1" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    ${3:+-d "$3"} "http://127.0.0.1:8000/api/v1$2"
}

# api_detail <method> <path> [json]: like api, but prints the status and the
# error message instead of failing quietly.
api_detail() {
  local email pass token
  email=$(grep '^EXA_ADMIN_EMAIL=' "$REPO/.env" | cut -d= -f2-)
  pass=$(grep '^EXA_ADMIN_PASSWORD=' "$REPO/.env" | cut -d= -f2-)
  token=$(jq -n --arg e "$email" --arg p "$pass" '{email:$e, password:$p}' |
    curl -fsS -H 'Content-Type: application/json' -d @- http://127.0.0.1:8000/api/v1/auth/login | jq -r .token)
  local out
  out=$(curl -sS -w '\n%{http_code}' -X "$1" -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' ${3:+-d "$3"} "http://127.0.0.1:8000/api/v1$2")
  echo "${out##*$'\n'}: $(jq -r '.detail // .' <<<"${out%$'\n'*}" 2>/dev/null | cut -c1-300)"
}

customer_id() { sql "SELECT id FROM customers ORDER BY name LIMIT 1"; }
