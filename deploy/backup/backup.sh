#!/usr/bin/env bash
# Encrypted database backup (ADR 0017).
#   EXA_BACKUP_PASSPHRASE=... EXA_BACKUP_DATABASE_URL=postgresql://... deploy/backup/backup.sh
# Writes <dir>/exaconnect-<UTC time>.dump.enc (pg_dump -Fc, then openssl
# aes-256-cbc with PBKDF2), a .sha256 and a .counts manifest taken in the same
# snapshot as the dump. Keeps EXA_BACKUP_KEEP_DAYS days (default 14). With
# EXA_BACKUP_RCLONE_REMOTE (e.g. "offsite:exacarib-backups/controller") and
# rclone installed and configured, each backup is also copied off-site.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_passphrase
umask 077
mkdir -p "$EXA_BACKUP_DIR"
db="$(source_db)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="exaconnect-$stamp.dump.enc"
out="$EXA_BACKUP_DIR/$name"
tmp="$out.partial"
trap 'rm -f "$tmp"; [ -n "${PSQL_PID:-}" ] && kill "$PSQL_PID" 2>/dev/null || true' EXIT

# Hold one repeatable-read transaction open, export its snapshot, dump with it
# and count rows in it, so the manifest matches the dump exactly.
coproc PSQL { pgtool psql "$db" -XAtq -v ON_ERROR_STOP=1 2>&1; }
echo "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();" >&"${PSQL[1]}"
read -r -t 30 snapshot <&"${PSQL[0]}" || die "could not open a snapshot"
[[ "$snapshot" =~ ^[0-9A-F-]+$ ]] || die "could not open a snapshot: $snapshot"
log "dumping $db (snapshot $snapshot)"
pgtool pg_dump "$db" -Fc --snapshot="$snapshot" | encrypt -out "$tmp"
{ count_sql; echo "\\echo __END__"; } >&"${PSQL[1]}"
counts=""
while read -r -t 60 line <&"${PSQL[0]}"; do
  [ "$line" = "__END__" ] && break
  counts="$line"
done
echo "COMMIT;" >&"${PSQL[1]}"
exec {PSQL[1]}>&-
wait "$PSQL_PID" 2>/dev/null || true

[ -s "$tmp" ] || die "the dump is empty"
mv "$tmp" "$out"
sha256_of "$out" > "$out.sha256"
echo "$counts" > "$out.counts"
log "wrote $out ($(du -h "$out" | cut -f1)); rows: $counts"

keep="${EXA_BACKUP_KEEP_DAYS:-14}"
find "$EXA_BACKUP_DIR" -maxdepth 1 -name 'exaconnect-*.dump.enc*' -mtime +"$keep" -print -delete | sed 's/^/removed /'

if [ -n "${EXA_BACKUP_RCLONE_REMOTE:-}" ]; then
  command -v rclone >/dev/null || die "EXA_BACKUP_RCLONE_REMOTE is set but rclone is not installed"
  rclone copy "$out" "$EXA_BACKUP_RCLONE_REMOTE" && rclone copy "$out.sha256" "$EXA_BACKUP_RCLONE_REMOTE" \
    && rclone copy "$out.counts" "$EXA_BACKUP_RCLONE_REMOTE"
  log "copied off-site to $EXA_BACKUP_RCLONE_REMOTE"
fi
echo "$out"
