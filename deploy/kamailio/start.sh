#!/bin/sh
# Kamailio plus a watcher (ADR 0044): when the controller renders new carrier lists
# (dispatcher.list, address.list; organisations add their own trunks at any time),
# reload them into the running proxy without dropping calls.
set -u
DIR=/exacarib/kamailio
sum() { cat "$DIR/dispatcher.list" "$DIR/address.list" 2>/dev/null | md5sum; }

kamailio -DD -E -f /etc/kamailio/kamailio.cfg &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null; wait "$pid"; exit 0' TERM INT
loaded=$(sum)
echo "exacarib: watching carrier lists"
while kill -0 "$pid" 2>/dev/null; do
  sleep 5
  now=$(sum)
  [ "$now" = "$loaded" ] && continue
  # Each list on its own, so one that fails doesn't hold back the other; say why.
  ok=1
  for rpc in dispatcher.reload permissions.addressReload; do
    if ! out=$(kamcmd "$rpc" 2>&1); then
      ok=""
      echo "exacarib: $rpc failed: $out" >&2
    fi
  done
  if [ -n "$ok" ]; then
    loaded=$now
    echo "exacarib: carrier lists reloaded"
  fi
done
wait "$pid"
