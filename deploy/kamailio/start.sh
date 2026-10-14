#!/bin/sh
# Kamailio plus a watcher (ADR 0044): when the controller renders new carrier lists
# (dispatcher.list, address.list; organisations add their own trunks at any time),
# reload them into the running proxy without dropping calls. platforms.list (ADR 0056:
# each organisation's calling-platform SIP address) goes into the `platforms` htable.
set -u
DIR=/exacarib/kamailio
sum() { cat "$DIR/dispatcher.list" "$DIR/address.list" "$DIR/platforms.list" 2>/dev/null | md5sum; }

# Replace the platforms htable with what platforms.list says ("sip-address platform:key" lines).
load_platforms() {
  kamcmd htable.flush platforms >/dev/null || return 1
  [ -f "$DIR/platforms.list" ] || return 0
  grep -v '^#' "$DIR/platforms.list" | while read -r host link; do
    [ -n "$host" ] && [ -n "$link" ] || continue
    kamcmd htable.sets platforms "$host" "$link" >/dev/null || exit 1
  done
}

kamailio -DD -E -f /etc/kamailio/kamailio.cfg &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null; wait "$pid"; exit 0' TERM INT
loaded=""
echo "exacarib: watching carrier lists"
while kill -0 "$pid" 2>/dev/null; do
  sleep 5
  now=$(sum)
  [ "$now" = "$loaded" ] && continue
  # Each list on its own, so one that fails doesn't hold back the other; say why.
  ok=1
  if [ -n "$loaded" ]; then  # Kamailio read the files itself when it started
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
