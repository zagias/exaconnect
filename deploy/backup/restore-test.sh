#!/usr/bin/env bash
# Prove a backup restores (ADR 0017): restore it into a scratch database,
# compare row counts of key tables with the counts taken in the dump's own
# snapshot, then drop the scratch database. Exit 0 only if every count matches.
#   deploy/backup/restore-test.sh [backup.dump.enc]   (default: the newest backup)
# Needs CREATEDB on the server. Same settings as backup.sh.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_passphrase
file="${1:-$(ls -1t "$EXA_BACKUP_DIR"/exaconnect-*.dump.enc 2>/dev/null | head -n1 || true)}"
[ -n "$file" ] && [ -f "$file" ] || die "no backup found in $EXA_BACKUP_DIR"
[ -f "$file.counts" ] || die "no row-count manifest next to $file"
src="$(source_db)"
scratch="exa_restore_test_$(date -u +%Y%m%d%H%M%S)_$$"
cleanup() { pgtool psql "$src" -XAtq -c "DROP DATABASE IF EXISTS \"$scratch\"" >/dev/null 2>&1 || true; }
trap cleanup EXIT
pgtool psql "$src" -XAtq -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$scratch\"" >/dev/null
log "scratch database $scratch"
"$here/restore.sh" "$file" "$scratch"

want="$(cat "$file.counts")"
got="$(pgtool psql "$scratch" -XAtq -v ON_ERROR_STOP=1 -c "$(count_sql)")"
[ -n "$want" ] || die "the manifest has no tables to check"
fail=0
for pair in $want; do
  t="${pair%%=*}"; n="${pair#*=}"
  m="$(tr ' ' '\n' <<<"$got" | sed -n "s/^$t=//p")"
  if [ "$m" = "$n" ]; then echo "ok    $t $n"; else echo "FAIL  $t backup=$n restored=${m:-missing}"; fail=1; fi
done
if [ "$fail" = "0" ]; then
  log "restore test passed: $(basename "$file")"
else
  log "restore test FAILED: $(basename "$file")"
  exit 1
fi
