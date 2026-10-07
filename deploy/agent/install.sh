#!/usr/bin/env bash
# Installs the ExaCarib Connect edge agent on a real site (Debian or Ubuntu, arm64
# or amd64) and enrols it through the agent gateway (ADR 0024). Run as root:
#
#   sudo EXA_ENROL_TOKEN=<token> bash install.sh --controller https://connect.exacarib.com:8443 \
#        --ca-fingerprint <fingerprint> --name <site name> [--binary ./exa-agent]
#
# Admin > Sites > "Enrolment token" shows the controller URL, the fingerprint and the
# token (once). The token goes in the environment so it stays out of the process list
# and shell history; it works once and expires. Without --binary the script uses
# exa-agent from this directory, or bin/linux-<arch>/ in a checkout (make build-agent).
set -euo pipefail

ctrl="" fp="" name="$(hostname -s)" bin=""
while (($#)); do
  case $1 in
    --controller) ctrl=$2; shift 2 ;;
    --ca-fingerprint) fp=$2; shift 2 ;;
    --name) name=$2; shift 2 ;;
    --binary) bin=$2; shift 2 ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
  esac
done
[[ $(id -u) == 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ -n $ctrl && -n $fp ]] || { echo "--controller and --ca-fingerprint are required"; exit 2; }

here=$(cd "$(dirname "$0")" && pwd)
arch=$(dpkg --print-architecture)
if [[ -z $bin ]]; then
  for c in "$here/exa-agent" "$here/../../bin/linux-$arch/exa-agent"; do
    [[ -f $c ]] && { bin=$c; break; }
  done
fi
[[ -n $bin && -f $bin ]] || { echo "no exa-agent binary for $arch (pass --binary)"; exit 1; }

echo "== packages: FRR, WireGuard, nftables"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq frr wireguard-tools nftables iproute2 >/dev/null
sed -i -e 's/^bgpd=no/bgpd=yes/' -e 's/^bfdd=no/bfdd=yes/' /etc/frr/daemons
systemctl enable --now frr >/dev/null
systemctl restart frr

echo "== agent"
install -m 755 "$bin" /usr/local/bin/exa-agent
install -d -m 700 /var/lib/exaconnect
install -m 644 "$here/../systemd/exa-agent.service" /etc/systemd/system/exa-agent.service 2>/dev/null ||
  install -m 644 "$here/exa-agent.service" /etc/systemd/system/exa-agent.service
/usr/local/bin/exa-agent version

if [[ -s /var/lib/exaconnect/identity.json ]]; then
  echo "already enrolled; keeping the existing identity (delete /var/lib/exaconnect to enrol again)"
else
  [[ -n ${EXA_ENROL_TOKEN:-} ]] || { echo "set EXA_ENROL_TOKEN to the one-time token"; exit 2; }
  EXA_STATE_DIR=/var/lib/exaconnect /usr/local/bin/exa-agent enrol --controller "$ctrl" \
    --ca-fingerprint "$fp" --name "$name"
fi

systemctl daemon-reload
systemctl enable --now exa-agent
sleep 3
systemctl --no-pager --lines=5 status exa-agent || true
echo "Done. The site turns green in the portal once its tunnels are up."
