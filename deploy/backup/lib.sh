# Shared by backup.sh, restore.sh and restore-test.sh. Sourced, not run.
#
# Where the database is (one of):
#   EXA_BACKUP_DATABASE_URL   postgresql://user@host:port/db for local client tools
#   EXA_BACKUP_COMPOSE_FILE   path to deploy/docker-compose.yml: run the tools inside
#                             the "db" container (user/db from EXA_BACKUP_DB_USER/_NAME)
# Secrets (from the environment or an EnvironmentFile, never the repo):
#   EXA_BACKUP_PASSPHRASE     encrypts every backup (openssl aes-256-cbc, PBKDF2)
#   PGPASSWORD                if the database needs a password
set -euo pipefail

EXA_BACKUP_DIR="${EXA_BACKUP_DIR:-/var/backups/exaconnect}"
EXA_BACKUP_DB_USER="${EXA_BACKUP_DB_USER:-exaconnect}"
EXA_BACKUP_DB_NAME="${EXA_BACKUP_DB_NAME:-exaconnect}"
PBKDF2_ITER=200000
# Tables whose row counts prove a restore (only those that exist are checked).
EXA_BACKUP_CHECK_TABLES="${EXA_BACKUP_CHECK_TABLES:-customers users sessions api_keys sites links nodes audit_log \
desired_states path_metrics link_usage commai_events contacts conversations messages jobs sso_connections scim_tokens}"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

need_passphrase() {
  [ -n "${EXA_BACKUP_PASSPHRASE:-}" ] || die "EXA_BACKUP_PASSPHRASE is not set"
  [ "${#EXA_BACKUP_PASSPHRASE}" -ge 16 ] || die "EXA_BACKUP_PASSPHRASE must be at least 16 characters"
}

source_db() {
  if [ -n "${EXA_BACKUP_COMPOSE_FILE:-}" ]; then echo "$EXA_BACKUP_DB_NAME"; return; fi
  [ -n "${EXA_BACKUP_DATABASE_URL:-}" ] || die "set EXA_BACKUP_DATABASE_URL or EXA_BACKUP_COMPOSE_FILE"
  local base="${EXA_BACKUP_DATABASE_URL%%\?*}"
  echo "${base##*/}"
}

# url_for <dbname>: the connection URL for another database on the same server.
url_for() {
  local base="${EXA_BACKUP_DATABASE_URL%%\?*}" query="${EXA_BACKUP_DATABASE_URL#*\?}"
  [ "$query" = "$EXA_BACKUP_DATABASE_URL" ] && query="" || query="?$query"
  echo "${base%/*}/$1$query"
}

# pgtool <tool> <dbname> [args...]: run a Postgres client tool against a database.
pgtool() {
  local tool="$1" db="$2"; shift 2
  if [ -n "${EXA_BACKUP_COMPOSE_FILE:-}" ]; then
    docker compose -f "$EXA_BACKUP_COMPOSE_FILE" exec -T db "$tool" -U "$EXA_BACKUP_DB_USER" -d "$db" "$@"
  else
    "$tool" -d "$(url_for "$db")" "$@"
  fi
}

# pg_restore -l (listing) reads the dump from stdin; it needs no database.
pglist() {
  if [ -n "${EXA_BACKUP_COMPOSE_FILE:-}" ]; then
    docker compose -f "$EXA_BACKUP_COMPOSE_FILE" exec -T db pg_restore -l
  else
    pg_restore -l
  fi
}

count_sql() {
  cat <<SQL
SELECT coalesce(string_agg(t || '=' || (xpath('/row/c/text()',
         query_to_xml(format('SELECT count(*) AS c FROM %I', t), false, true, '')))[1]::text, ' ' ORDER BY t), '')
FROM unnest(string_to_array('$EXA_BACKUP_CHECK_TABLES', ' ')) AS t
WHERE t <> '' AND to_regclass(format('public.%I', t)) IS NOT NULL;
SQL
}

encrypt() { openssl enc -aes-256-cbc -pbkdf2 -iter "$PBKDF2_ITER" -salt -pass env:EXA_BACKUP_PASSPHRASE "$@"; }
decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter "$PBKDF2_ITER" -pass env:EXA_BACKUP_PASSPHRASE "$@"; }

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
