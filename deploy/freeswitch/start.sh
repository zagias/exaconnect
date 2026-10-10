#!/bin/sh
# FreeSWITCH with live config: the image's own start, plus a watcher that reloads the
# rendered config a few seconds after the controller writes it, so a person, number,
# menu or queue added in the portal works without a redeploy. Busybox sh: no bash.
# Directory, dial plan and menus reload in place; call queues only when the queue
# files changed, because reloading mod_callcenter drops calls waiting in a queue.
(
  last="" lastq=""
  while sleep 5; do
    now=$(cat /exacarib/freeswitch/*/*.xml 2>/dev/null | md5sum)
    [ "$now" = "$last" ] && continue
    fs_cli -x reloadxml 2>/dev/null | grep -q '^+OK' || continue
    q=$(cat /exacarib/freeswitch/*/queues.xml /exacarib/freeswitch/*/agents.xml /exacarib/freeswitch/*/tiers.xml 2>/dev/null | md5sum)
    if [ -n "$lastq" ] && [ "$q" != "$lastq" ]; then fs_cli -x "reload mod_callcenter" >/dev/null 2>&1; fi
    [ -n "$last" ] && echo "exacarib: rendered config changed, reloaded"
    last=$now lastq=$q
  done
) &
exec /docker-entrypoint.sh "$@"
