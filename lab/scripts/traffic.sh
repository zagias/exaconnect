#!/usr/bin/env bash
# Lab traffic generators (CLAUDE.md §4.7), from each site's LAN host toward
# the other site's, through the PoP:
#   voice     160-byte UDP datagrams, 50 a second, to UDP 10000 (voice ports)
#   business  iperf3 TCP at BUSINESS_RATE (default 5M), DSCP AF31
#   bulk      iperf3 TCP at BULK_RATE (default 50M), DSCP CS1
#
#   traffic.sh start [voice|business|bulk ...]   (all three by default)
#   traffic.sh stop  [voice|business|bulk ...]
#   traffic.sh status
#
# Each sender and receiver runs in a loop, so a cut or a restored link only
# pauses it. iperf3's UDP mode stalled in this lab (see m9-traffic), so voice
# is plain bash writing to /dev/udp.
# shellcheck source=lab/netem/netem.sh
source "$(dirname "$0")/../netem/netem.sh"
set +e

BUSINESS_RATE="${BUSINESS_RATE:-5M}"
BULK_RATE="${BULK_RATE:-50M}"
VOICE_PORT=10000
BUSINESS_PORT=5301
BULK_PORT=5302
CLASSES=(voice business bulk)
# lan host, its address, and the peer it sends to
PAIRS=("lan-a 192.168.10.10 192.168.20.10" "lan-b 192.168.20.10 192.168.10.10")

usage() {
  echo "usage: $0 start|stop|status [voice|business|bulk ...]" >&2
  exit 2
}

# Every generator's command line starts with ": exa-traffic-<class>", so
# pkill -f finds it and nothing else. match is the pattern for it: "[:]"
# matches ":" but not itself, so a shell running pgrep is not counted.
tag() { echo ": exa-traffic-$1"; }
match() { echo "[:] exa-traffic-$1"; }

receiver() { # class
  case "$1" in
    voice) echo "$(tag voice)-rx; while :; do nc -u -l -p $VOICE_PORT >/dev/null 2>&1; sleep 1; done" ;;
    business) echo "$(tag business)-rx; while :; do iperf3 -s -1 -p $BUSINESS_PORT >/dev/null 2>&1; sleep 1; done" ;;
    bulk) echo "$(tag bulk)-rx; while :; do iperf3 -s -1 -p $BULK_PORT >/dev/null 2>&1; sleep 1; done" ;;
  esac
}

sender() { # class peer
  case "$1" in
    # About 50 a second: 20 ms between datagrams, less the loop's own time.
    voice) echo "$(tag voice)-tx; while :; do exec 3>/dev/udp/$2/$VOICE_PORT || { sleep 2; continue; }
             for _ in \$(seq 3000); do printf '%0160d' 0 >&3 2>/dev/null; sleep 0.019; done; exec 3>&-; done" ;;
    # DSCP 26 (AF31) is TOS 104; DSCP 8 (CS1) is TOS 32.
    business) echo "$(tag business)-tx; while :; do iperf3 -c $2 -p $BUSINESS_PORT -S 104 -b $BUSINESS_RATE -t 0 >/dev/null 2>&1; sleep 2; done" ;;
    bulk) echo "$(tag bulk)-tx; while :; do iperf3 -c $2 -p $BULK_PORT -S 32 -b $BULK_RATE -t 0 >/dev/null 2>&1; sleep 2; done" ;;
  esac
}

port() {
  case "$1" in voice) echo $VOICE_PORT ;; business) echo $BUSINESS_PORT ;; bulk) echo $BULK_PORT ;; esac
}

stop_class() { # host class
  local n
  n=$(node "$1")
  docker exec "$n" pkill -f "$(match "$2")" 2>/dev/null
  # The tools the loops started, by their port (BusyBox pkill: basic regex).
  docker exec "$n" pkill -f "[i]perf3 .*-p $(port "$2")" 2>/dev/null
  [[ $2 == voice ]] && docker exec "$n" pkill -f "[n]c -u -l -p $VOICE_PORT" 2>/dev/null
  return 0
}

running() { # host class -> number of loops running (receiver and sender)
  local n
  n=$(docker exec "$(node "$1")" sh -c "pgrep -f '$(match "$2")' | wc -l" 2>/dev/null)
  echo "${n:-0}"
}

cmd=${1:-}
[[ -n $cmd ]] || usage
shift
want=("$@")
((${#want[@]})) || want=("${CLASSES[@]}")
for c in "${want[@]}"; do
  [[ " ${CLASSES[*]} " == *" $c "* ]] || usage
done

case "$cmd" in
  start)
    for pair in "${PAIRS[@]}"; do
      read -r host _ _ <<<"$pair"
      docker ps --format '{{.Names}}' | grep -qx "$(node "$host")" || {
        echo "$(node "$host") is not running (make lab-up first)" >&2
        exit 1
      }
    done
    for c in "${want[@]}"; do
      # Start clean, so a second start does not double the traffic.
      for pair in "${PAIRS[@]}"; do
        read -r host _ _ <<<"$pair"
        stop_class "$host" "$c"
        docker exec -d "$(node "$host")" bash -c "$(receiver "$c")"
      done
    done
    sleep 1
    for c in "${want[@]}"; do
      for pair in "${PAIRS[@]}"; do
        read -r host addr peer <<<"$pair"
        docker exec -d "$(node "$host")" bash -c "$(sender "$c" "$peer")"
        printf 'started %-8s %s (%s) -> %s\n' "$c" "$host" "$addr" "$peer"
      done
    done
    echo "rates: voice 50 pps x 160 B, business $BUSINESS_RATE, bulk $BULK_RATE (each direction)"
    ;;
  stop)
    for c in "${want[@]}"; do
      for pair in "${PAIRS[@]}"; do
        read -r host _ _ <<<"$pair"
        stop_class "$host" "$c"
      done
      echo "stopped $c"
    done
    ;;
  status)
    rc=1
    for pair in "${PAIRS[@]}"; do
      read -r host _ peer <<<"$pair"
      for c in "${want[@]}"; do
        n=$(running "$host" "$c")
        if ((n >= 2)); then
          state="running"
          rc=0
        elif ((n == 1)); then
          state="partial"
        else
          state="stopped"
        fi
        printf '%-6s %-8s -> %-14s %s\n' "$host" "$c" "$peer" "$state"
      done
    done
    exit $rc
    ;;
  *) usage ;;
esac
