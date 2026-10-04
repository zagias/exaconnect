#!/usr/bin/env bash
# Diagnostics only: how much latency and jitter the lab host itself adds.
# Probe jitter in the lab reads far above what netem is asked for (carrier A
# 25 ms with 2 ms jitter), which stalls voice's move back to carrier A. This
# pings each layer of one path to see where the extra comes from: no netem
# (site to its LAN), one carrier (underlay to the PoP), and the tunnel.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"

echo "-- host timing"
note "clocksource $(cat /sys/devices/system/clocksource/clocksource0/current_clocksource 2>/dev/null)," \
  "available: $(cat /sys/devices/system/clocksource/clocksource0/available_clocksource 2>/dev/null)"
note "kernel $(uname -r), timers: $(grep -m1 -o 'hres_active *: [01]' /proc/timer_list 2>/dev/null)," \
  "$(grep -m1 -o 'resolution: [0-9]* nsecs' /proc/timer_list 2>/dev/null)"
note "HZ $(grep -m1 '^CONFIG_HZ=' "/boot/config-$(uname -r)" 2>/dev/null | cut -d= -f2), $(nproc) CPUs, load $(cut -d' ' -f1-3 /proc/loadavg)"
note "netem on carrier-a: $(docker exec "$(node carrier-a)" tc qdisc show dev eth1 | head -1)"

# rtt <node> <target> [ping options]: "min/avg/max/mdev" over 100 pings at 20 a second.
rtt() {
  local n=$1 t=$2
  shift 2
  docker exec "$(node "$n")" ping -q -c 100 -i 0.05 -W 1 "$@" "$t" 2>/dev/null | awk -F' = ' '/^rtt|^round-trip/ {print $2}'
}
layers() {
  note "site-a to lan-a (no netem):              $(rtt site-a 192.168.10.10)"
  note "site-a to carrier-a router (one netem):  $(rtt site-a 10.11.1.1)"
  note "site-a to PoP underlay via carrier A:    $(rtt site-a 10.11.0.2)"
  note "site-a to PoP through wg-a:              $(rtt site-a 100.64.1.1 -I wg-a)"
  note "site-a to PoP underlay via carrier B:    $(rtt site-a 10.12.0.2)"
}
note "cpuidle driver $(cat /sys/devices/system/cpu/cpuidle/current_driver 2>/dev/null)," \
  "governor $(cat /sys/devices/system/cpu/cpuidle/current_governor_ro /sys/devices/system/cpu/cpuidle/current_governor 2>/dev/null | head -1)"
note "(netem asks for 25 ms on A and 35 ms on B round trip, with 2 and 4 ms jitter; min/avg/max/mdev)"
layers
# Where the spikes come from: the same carrier A path with the agents frozen
# (SIGSTOP for about 6 s; FRR and BFD keep running), then with the controller
# paused. Forwarding never depends on either.
a="site-a to PoP underlay via carrier A:   "
note "$a $(rtt site-a 10.11.0.2)   (as is)"
for n in pop-miami site-a site-b; do docker exec "$(node "$n")" pkill -STOP -f "exa-agent run"; done
note "$a $(rtt site-a 10.11.0.2)   (agents frozen)"
for n in pop-miami site-a site-b; do docker exec "$(node "$n")" pkill -CONT -f "exa-agent run"; done
ctl=$(docker ps -q -f name=exaconnect-controller)
if [[ -n $ctl ]]; then
  docker pause "$ctl" >/dev/null
  note "$a $(rtt site-a 10.11.0.2)   (controller paused)"
  docker unpause "$ctl" >/dev/null
fi
note "$a $(rtt site-a 10.11.0.2)   (as is)"

echo "-- busiest containers"
docker stats --no-stream --format '{{.CPUPerc}} {{.Name}}' 2>/dev/null | sort -rn | head -8 | sed 's/^/      /'
echo "-- busiest processes"
ps -eo pcpu,comm --sort=-pcpu | sed -n 2,9p | sed 's/^/      /'
exit 0
