#!/usr/bin/env bash
# shellcheck disable=SC2015  # pass never fails
# Boots the ExaCarib box image in a virtual machine and checks it as someone at a
# branch would use it (ADR 0047). The VM has two ports on their own user-mode
# networks: eth0 the internet (DHCP), eth1 the office (the image's default
# 192.168.50.1). This host stands in for a laptop on each side:
#   https://127.0.0.1:$LAN_PORT  reaches the box console on the office port
#   https://127.0.0.1:$WAN_PORT  reaches the box on its internet port
#
#   deploy/box/test-image.sh start <image.qcow2> <dir>  boot; <dir>/setup-code (0600) once shown
#   deploy/box/test-image.sh check <dir>                set-up checks through the console API
#   deploy/box/test-image.sh stop <dir>
# Uses KVM when /dev/kvm is there, and is slower without it. Prints PASS/FAIL lines.
set -uo pipefail
cmd=$1
LAN_PORT=${LAN_PORT:-9443} WAN_PORT=${WAN_PORT:-9444}
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; FAILS=$((${FAILS:-0} + 1)); }

case $cmd in
start)
  img=$2 dir=$3
  mkdir -p "$dir" && chmod 700 "$dir"
  qemu-img create -q -f qcow2 -F qcow2 -b "$(realpath "$img")" "$dir/disk.qcow2"
  accel=tcg
  [[ -w /dev/kvm ]] && accel=kvm
  qemu-system-x86_64 -machine q35,accel=$accel -cpu max -m 1024 -smp 2 \
    -display none -monitor none -serial "file:$dir/serial.log" -daemonize -pidfile "$dir/qemu.pid" \
    -drive "file=$dir/disk.qcow2,if=virtio" \
    -netdev "user,id=wan,hostfwd=tcp:127.0.0.1:$WAN_PORT-:443" -device virtio-net-pci,netdev=wan \
    -netdev "user,id=lan,net=192.168.50.0/24,host=192.168.50.2,dhcpstart=192.168.50.240,hostfwd=tcp:127.0.0.1:$LAN_PORT-192.168.50.1:443" \
    -device virtio-net-pci,netdev=lan || { fail "the VM did not start"; exit 1; }
  echo "booting ($accel)"
  for _ in $(seq 1 180); do
    code=$(tr -d '\r' <"$dir/serial.log" 2>/dev/null | sed -n 's/^ *Setup code: *\([A-Z0-9-]*\).*/\1/p' | tail -n1)
    [[ -n $code ]] && break
    sleep 5
  done
  if [[ -z ${code:-} ]]; then
    fail "the screen never showed a setup code: $(tr -d '\r' <"$dir/serial.log" | grep -v '^\s*$' | tail -n 5 | tr '\n' ' ' | cut -c1-300)"
    exit 1
  fi
  (umask 077 && echo "$code" >"$dir/setup-code")
  pass "the image boots and its screen shows the setup code"
  tr -d '\r' <"$dir/serial.log" | grep -q 'Box console: https://192.168.50.1' &&
    pass "the screen shows the console address https://192.168.50.1" || fail "the screen has no console address"
  for _ in $(seq 1 30); do
    curl -sk -o /dev/null --max-time 3 "https://127.0.0.1:$LAN_PORT/api/state" && break
    sleep 2
  done
  if curl -sk --max-time 5 "https://127.0.0.1:$LAN_PORT/api/state" | grep -q '"stage"'; then
    pass "the box console answers on the office port"
  else
    fail "no box console on the office port"
    exit 1
  fi
  exit 0
  ;;
check)
  dir=$2 B="https://127.0.0.1:$LAN_PORT" W="https://127.0.0.1:$WAN_PORT"
  jar="$dir/cookies" pw=$(openssl rand -base64 18)
  c=(curl -sk --max-time 60 -b "$jar" -c "$jar" -H 'X-Exa-Box: 1' -H 'Content-Type: application/json')
  code=$(cat "$dir/setup-code")
  [[ $("${c[@]}" -o /dev/null -w '%{http_code}' "$W/api/state") == 200 ]] &&
    pass "before set-up the console answers on every port" || fail "a fresh box does not answer on its first port"
  r=$("${c[@]}" -X POST "$B/api/claim" -d "{\"code\": \"$code\", \"password\": \"$pw\"}")
  grep -q '"ready"' <<<"$r" && pass "the setup code sets the admin password" || fail "claim: ${r:0:200}"
  r=$("${c[@]}" "$B/api/network")
  grep -q '"eth0"' <<<"$r" && grep -q '"eth1"' <<<"$r" && pass "the console lists ports eth0 and eth1" || fail "ports: ${r:0:200}"
  grep -q '"netplan":true' <<<"$r" && grep -q '"dhcp_server":true' <<<"$r" && grep -q '"pppoe":true' <<<"$r" &&
    pass "netplan, PPPoE and office DHCP are there" || fail "caps: $(grep -o '"caps":{[^}]*}' <<<"$r")"
  r=$("${c[@]}" -X POST "$B/api/network" -d '{"wan": [{"iface": "eth0", "path": "carrier-a", "mode": "dhcp"}], "lan": {"iface": "eth1", "mode": "static", "address": "192.168.50.1/24"}, "lan_dhcp": true}')
  grep -q '"pending"' <<<"$r" && pass "ports applied: eth0 internet by DHCP, eth1 office with DHCP" || fail "ports: ${r:0:300}"
  r=$("${c[@]}" -X POST "$B/api/network/confirm" -d '{}')
  grep -q '"confirmed":true' <<<"$r" && pass "port change kept" || fail "confirm: ${r:0:200}"
  sleep 3
  r=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 6 "$W/api/state")
  [[ $r == 000 ]] && pass "the internet port now drops the console (box firewall)" || fail "the internet port answered $r"
  r=$("${c[@]}" "$B/api/overview")
  grep -q '"eth0"' <<<"$r" && pass "still reachable from the office after the change" || fail "overview: ${r:0:200}"
  r=$("${c[@]}" -X POST "$B/api/diag" -d '{"tool": "dns", "target": "deb.debian.org"}')
  grep -q 'answered in' <<<"$r" && pass "the box resolves names through its internet port" || fail "dns: ${r:0:200}"
  exit "${FAILS:-0}"
  ;;
stop)
  dir=$2
  [[ -s $dir/qemu.pid ]] && kill "$(cat "$dir/qemu.pid")" 2>/dev/null
  rm -f "$dir/disk.qcow2" "$dir/cookies" "$dir/setup-code"
  ;;
*)
  sed -n '2,15p' "$0"
  exit 2
  ;;
esac
