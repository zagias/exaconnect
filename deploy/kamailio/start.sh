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
while kill -0 "$pid" 2>/dev/null; do
  sleep 5
  now=$(sum)
  [ "$now" = "$loaded" ] && continue
  if kamcmd dispatcher.reload >/dev/null && kamcmd permissions.addressReload >/dev/null; then
    loaded=$now
    echo "exacarib: carrier lists reloaded"
  else
    echo "exacarib: carrier lists changed but the reload failed; trying again" >&2
  fi
done
wait "$pid"
