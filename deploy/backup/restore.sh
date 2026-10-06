#!/usr/bin/env bash
# Restore an encrypted backup into a database (ADR 0017).
#   deploy/backup/restore.sh <backup.dump.enc> <target database name>
# The target must exist and be empty (EXA_RESTORE_FORCE=1 restores over one
# that is not, with --clean). Uses the same connection settings as backup.sh.
# When the dump holds TimescaleDB, the extension is created first and
# timescaledb_pre_restore()/post_restore() wrap the restore.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_passphrase
file="${1:?usage: restore.sh <backup.dump.enc> <target database>}"
target="${2:?usage: restore.sh <backup.dump.enc> <target database>}"
[ -f "$file" ] || die "no such backup: $file"
if [ -f "$file.sha256" ] && [ "$(sha256_of "$file")" != "$(cat "$file.sha256")" ]; then
  die "checksum mismatch for $file"
fi
umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
decrypt -in "$file" -out "$work/dump" || die "could not decrypt (wrong EXA_BACKUP_PASSPHRASE?)"
pglist < "$work/dump" > "$work/toc"

tables="$(pgtool psql "$target" -XAtq -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')")"
opts=(--no-owner --no-privileges --exit-on-error)
if [ "$tables" != "0" ]; then
  [ "${EXA_RESTORE_FORCE:-0}" = "1" ] || die "$target is not empty; set EXA_RESTORE_FORCE=1 to restore over it"
  opts+=(--clean --if-exists)
fi

timescale=0
if grep -qE 'EXTENSION - timescaledb' "$work/toc"; then
  timescale=1
  log "TimescaleDB dump: pre-restore"
  pgtool psql "$target" -XAtq -v ON_ERROR_STOP=1 \
    -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" >/dev/null
  # The extension already exists in the target; skip its TOC entries.
  grep -vE 'EXTENSION - timescaledb|COMMENT - EXTENSION timescaledb' "$work/toc" > "$work/toc.use"
  opts+=(-L /dev/stdin)
fi

log "restoring into $target"
if [ "$timescale" = "1" ]; then
  # The list goes on stdin, so the dump is passed as a file the tool can read.
  if [ -n "${EXA_BACKUP_COMPOSE_FILE:-}" ]; then
    docker compose -f "$EXA_BACKUP_COMPOSE_FILE" cp "$work/dump" db:/tmp/exa-restore.dump
    pgtool pg_restore "$target" "${opts[@]}" /tmp/exa-restore.dump < "$work/toc.use"
    docker compose -f "$EXA_BACKUP_COMPOSE_FILE" exec -T db rm -f /tmp/exa-restore.dump
  else
    pgtool pg_restore "$target" "${opts[@]}" "$work/dump" < "$work/toc.use"
  fi
  pgtool psql "$target" -XAtq -v ON_ERROR_STOP=1 -c "SELECT timescaledb_post_restore()" >/dev/null
  log "TimescaleDB post-restore done"
else
  pgtool pg_restore "$target" "${opts[@]}" < "$work/dump"
fi
log "restored $file into $target"
