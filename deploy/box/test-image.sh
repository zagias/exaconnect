#!/usr/bin/env bash
# shellcheck disable=SC2015  # pass never fails
# Boots the ExaCarib box image in a virtual machine and checks it as someone at a
# branch would use it (ADR 0047). The VM has two ports on their own user-mode
# networks: eth0 the internet (DHCP), eth1 the office (the image's default
# 192.168.50.1). This host stands in for a laptop on each side:
#   https://127.0.0.1:$LAN_PORT  reaches the box console on the office port
#   https://127.0.0.1:$WAN_PORT  reaches the box on its internet port
#   udp 127.0.0.1:$SNMP_PORT / $SNMP_WAN_PORT  the box's SNMP agent, office / internet side
# and 192.168.50.2 in the office is this host: traps and syslog land on its UDP $TRAP_PORT / $SYSLOG_PORT.
#
#   deploy/box/test-image.sh start <image.qcow2> <dir>  boot; <dir>/setup-code (0600) once shown
#   deploy/box/test-image.sh check <dir>                set-up checks through the console API
#   deploy/box/test-image.sh stop <dir>
# Uses KVM when /dev/kvm is there, and is slower without it. Prints PASS/FAIL lines.
set -uo pipefail
cmd=$1
LAN_PORT=${LAN_PORT:-9443} WAN_PORT=${WAN_PORT:-9444}
SNMP_PORT=${SNMP_PORT:-9161} SNMP_WAN_PORT=${SNMP_WAN_PORT:-9162} TRAP_PORT=${TRAP_PORT:-9163} SYSLOG_PORT=${SYSLOG_PORT:-9514}
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
    -netdev "user,id=wan,hostfwd=tcp:127.0.0.1:$WAN_PORT-:443,hostfwd=udp:127.0.0.1:$SNMP_WAN_PORT-:161" -device virtio-net-pci,netdev=wan \
    -netdev "user,id=lan,net=192.168.50.0/24,host=192.168.50.2,dhcpstart=192.168.50.240,hostfwd=tcp:127.0.0.1:$LAN_PORT-192.168.50.1:443,hostfwd=udp:127.0.0.1:$SNMP_PORT-192.168.50.1:161" \
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

  # Monitoring (ADR 0048): SNMP v2c and v3 polling from the office, none from the
  # internet, traps and syslog to a monitoring system in the office, a read-only API key.
  comm=$(openssl rand -hex 8) a3=$(openssl rand -hex 8) p3=$(openssl rand -hex 8)
  python3 - "$TRAP_PORT" "$dir/traps" "$SYSLOG_PORT" "$dir/syslog" <<'PY' &
import socket, sys, select, time
socks = []
for port, path in ((int(sys.argv[1]), sys.argv[2]), (int(sys.argv[3]), sys.argv[4])):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", port)); socks.append((s, open(path, "ab", 0)))
end = time.time() + 300
while time.time() < end:
    r, _, _ = select.select([s for s, _ in socks], [], [], 1)
    for s, f in socks:
        if s in r:
            f.write(s.recv(65535) + b"\n")
PY
  listener=$!
  r=$("${c[@]}" -X POST "$B/api/monitoring" -d "{\"snmp\": {\"v2c\": true, \"community\": \"$comm\", \"v3\": true, \"v3_user\": \"nms\", \"v3_auth\": \"$a3\", \"v3_priv\": \"$p3\", \"location\": \"Test bench\"},
    \"traps\": [{\"host\": \"192.168.50.2\", \"port\": $TRAP_PORT, \"version\": \"v2c\", \"community\": \"$comm\"}], \"syslog\": {\"host\": \"192.168.50.2\", \"port\": $SYSLOG_PORT, \"proto\": \"udp\"}}")
  grep -q '"engine_id"' <<<"$r" && pass "monitoring saved: SNMP v2c and v3, a trap destination, syslog" || fail "monitoring: ${r:0:300}"
  grep -q "$comm\|$a3" <<<"$r" && fail "the console sent back an SNMP secret"
  if command -v snmpget >/dev/null; then
    sleep 2
    r=$(snmpget -v2c -c "$comm" -t 3 -r 2 -Oqv "127.0.0.1:$SNMP_PORT" 1.3.6.1.2.1.1.5.0 1.3.6.1.2.1.1.6.0 2>&1)
    grep -q exacarib-box <<<"$r" && grep -q 'Test bench' <<<"$r" && pass "SNMP v2c answers from the office (sysName, sysLocation)" || fail "snmp v2c: ${r:0:200}"
    r=$(snmpget -v3 -l authPriv -u nms -a SHA -A "$a3" -x AES -X "$p3" -t 3 -r 2 -Oqv "127.0.0.1:$SNMP_PORT" 1.3.6.1.2.1.1.5.0 2>&1)
    grep -q exacarib-box <<<"$r" && pass "SNMPv3 (SHA, AES) answers from the office" || fail "snmp v3: ${r:0:200}"
    r=$(snmpwalk -v2c -c "$comm" -t 3 -r 1 -Oqn "127.0.0.1:$SNMP_PORT" 1.3.6.1.2.1.2.2.1.2 2>&1)
    grep -q eth0 <<<"$r" && grep -q eth1 <<<"$r" && pass "SNMP lists the ports (IF-MIB)" || fail "snmp ifDescr: ${r:0:200}"
    r=$(snmpget -v2c -c wrong-community -t 2 -r 0 "127.0.0.1:$SNMP_PORT" 1.3.6.1.2.1.1.5.0 2>&1)
    grep -qi timeout <<<"$r" && pass "a wrong community gets no answer" || fail "wrong community: ${r:0:200}"
    r=$(snmpget -v2c -c "$comm" -t 2 -r 0 "127.0.0.1:$SNMP_WAN_PORT" 1.3.6.1.2.1.1.5.0 2>&1)
    grep -qi timeout <<<"$r" && pass "the internet port drops SNMP" || fail "SNMP from the internet port: ${r:0:200}"
  else
    fail "this host has no snmpget (the snmp package) to check SNMP"
  fi
  r=$("${c[@]}" -X POST "$B/api/monitoring/test" -d '{}')
  grep -q 'test trap' <<<"$r" || fail "test message: ${r:0:200}"
  for _ in $(seq 1 15); do grep -aq 'ExaCarib box test message' "$dir/syslog" 2>/dev/null && grep -aq exacarib-box "$dir/traps" 2>/dev/null && break; sleep 1; done
  grep -aq 'ExaCarib box test message' "$dir/syslog" 2>/dev/null && pass "the box's log reaches the syslog collector" || fail "no syslog message arrived"
  grep -aq "$comm" "$dir/traps" 2>/dev/null && grep -aq exacarib-box "$dir/traps" && pass "the test trap reaches the monitoring system" || fail "no trap arrived"
  kill "$listener" 2>/dev/null
  r=$("${c[@]}" -X POST "$B/api/apikeys" -d '{"name": "Test NMS"}')
  key=$(sed -n 's/.*"key":"\([^"]*\)".*/\1/p' <<<"$r")
  if [[ -n $key ]]; then
    r=$(curl -sk --max-time 20 -H "Authorization: Bearer $key" "$B/api/overview")
    grep -q '"hostname"' <<<"$r" && pass "a read-only API key reads the box's status" || fail "API key read: ${r:0:200}"
    r=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 20 -H "Authorization: Bearer $key" -H 'X-Exa-Box: 1' -X POST "$B/api/network/confirm" -d '{}')
    [[ $r == 401 ]] && pass "an API key cannot change anything" || fail "API key write answered $r"
  else
    fail "no API key: ${r:0:200}"
  fi
  unset key comm a3 p3
  exit "${FAILS:-0}"
  ;;
stop)
  dir=$2
  [[ -s $dir/qemu.pid ]] && kill "$(cat "$dir/qemu.pid")" 2>/dev/null
  rm -f "$dir/disk.qcow2" "$dir/cookies" "$dir/setup-code" "$dir/traps" "$dir/syslog"
  ;;
*)
  sed -n '2,17p' "$0"
  exit 2
  ;;
esac
