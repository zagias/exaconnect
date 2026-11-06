#!/bin/sh
# Legacy carrier watcher plus verification of the persistent approved ingress
# generation. Controller candidates never activate or revoke approved policy.
set -u
DIR=/exacarib/kamailio
APPROVED=/exacarib/voice-approved
sum() { cat "$DIR/dispatcher.list" "$DIR/address.list" "$DIR/platforms.list" 2>/dev/null | md5sum; }

approved_digest() {
  if [ ! -e "$APPROVED/current" ] && [ ! -L "$APPROVED/current" ] && [ ! -e "$APPROVED/active-revision" ]; then
    echo none
    return 0
  fi
  [ "$(stat -c %u "$APPROVED")" = 0 ] || return 1
  mode=$(stat -c %a "$APPROVED") || return 1
  case "$mode" in 755|750|700|555|550|500) ;; *) return 1 ;; esac
  path=$(readlink -f "$APPROVED/current") || return 1
  case "$path" in "$APPROVED"/generations/*) ;; *) return 1 ;; esac
  generation=$(basename "$path")
  [ "${#generation}" -eq 64 ] || return 1
  case "$generation" in *[!0-9a-f]*) return 1 ;; esac
  [ "$(sha256sum "$path/SHA256SUMS" | cut -d ' ' -f 1)" = "$generation" ] || return 1
  expected_names=$(printf '%s\n' policy.json edge.list peers.list public/routes.xml protected/routes.xml contexts/routes.xml ivr/routes.xml)
  [ "$(awk '{print $2}' "$path/SHA256SUMS")" = "$expected_names" ] || return 1
  (cd "$path" && sha256sum -c SHA256SUMS >/dev/null 2>&1) || return 1
  [ "$(cat "$APPROVED/active-revision")" = "$generation" ] || return 1
  echo "$generation"
}

load_approved() {
  generation=$1
  kamcmd htable.sets protectedin gate closed >/dev/null || return 1
  [ "$generation" != none ] || { kamcmd htable.sets protectedin gate open >/dev/null; return; }
  read -r sbc_ip pbx_ip < "$APPROVED/current/peers.list" || return 1
  kamcmd htable.flush protectedin >/dev/null || return 1
  kamcmd htable.sets protectedin gate closed >/dev/null || return 1
  kamcmd htable.sets protectedin pbx_ip "$pbx_ip" >/dev/null || return 1
  kamcmd htable.sets protectedin sbc_ip "$sbc_ip" >/dev/null || return 1
  kamcmd htable.sets protectedin pbx_uri "sip:$pbx_ip:5092;transport=tls" >/dev/null || return 1
  kamcmd htable.sets protectedin sbc_route "$sbc_ip:5091;transport=tls;r2=on" >/dev/null || return 1
  while read -r did state group revision sources carrier_set destination; do
    [ -n "$did" ] || continue
    kamcmd htable.sets protectedin "$generation:$did" "$group:$revision:$carrier_set" >/dev/null || return 1
    if [ "$state" = protected ]; then
      kamcmd htable.sets protectedin "$generation:acl:$group" "$sources" >/dev/null || return 1
      kamcmd htable.sets protectedin "$generation:set:$carrier_set" "$group" >/dev/null || return 1
      kamcmd htable.sets protectedin "$generation:destination:$carrier_set" "$destination" >/dev/null || return 1
    fi
  done < "$APPROVED/current/edge.list"
  [ "$(approved_digest)" = "$generation" ] || return 1
  kamcmd htable.sets protectedin active "$generation" >/dev/null || return 1
  kamcmd htable.sets protectedin gate open >/dev/null
}

load_platforms() {
  kamcmd htable.flush platforms >/dev/null || return 1
  [ -f "$DIR/platforms.list" ] || return 0
  grep -v '^#' "$DIR/platforms.list" | while read -r host link; do
    if [ -z "$host" ] || [ -z "$link" ]; then continue; fi
    kamcmd htable.sets platforms "$host" "$link" >/dev/null || exit 1
  done
}

kamailio -DD -E -f /etc/kamailio/kamailio.cfg &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null; wait "$pid"; exit 0' TERM INT
loaded="" protected_started=0
echo "exacarib: watching carrier lists and approved local policy"
while kill -0 "$pid" 2>/dev/null; do
  sleep 5
  if approved_now=$(approved_digest); then
    if [ "$protected_started" -eq 0 ]; then
      if ! load_approved "$approved_now"; then
        kamcmd htable.sets protectedin gate closed >/dev/null 2>&1 || { kill -TERM "$pid"; exit 1; }
        continue
      fi
      protected_started=1
    fi
  else
    # Integrity or receipt mismatch cannot activate a partial/stale generation.
    # The explicit loader may complete activation later; this watcher never
    # opens an activation gate merely because files changed.
    kamcmd htable.sets protectedin gate closed >/dev/null 2>&1 || { kill -TERM "$pid"; exit 1; }
  fi
  now=$(sum)
  [ "$now" = "$loaded" ] && continue
  ok=1
  if [ -n "$loaded" ]; then
    for rpc in dispatcher.reload permissions.addressReload; do
      if ! out=$(kamcmd "$rpc" 2>&1); then
        ok=""
        echo "exacarib: $rpc failed: $out" >&2
      fi
    done
  fi
  if ! load_platforms; then
    ok=""
    echo "exacarib: loading platforms.list failed" >&2
  fi
  if [ -n "$ok" ]; then
    [ -n "$loaded" ] && echo "exacarib: carrier lists reloaded"
    loaded=$now
  fi
done
wait "$pid"
