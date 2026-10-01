#!/usr/bin/env bash
# Prepares an Ubuntu 24.04 lab host (a cloud VM with root, arm64 first) to run the ExaConnect lab.
# Run on the host, never on macOS:   sudo lab/host/setup-ubuntu.sh
# Installs Docker (Ubuntu's docker.io), containerlab, make and the kernel
# modules the lab needs (WireGuard, netem). Safe to re-run.
set -euo pipefail
[[ $EUID == 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo "Linux only" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  docker.io docker-compose-v2 make git curl ca-certificates jq \
  wireguard-tools "linux-modules-extra-$(uname -r)" || \
apt-get install -y --no-install-recommends \
  docker.io docker-compose-v2 make git curl ca-certificates jq wireguard-tools

systemctl enable --now docker
if [[ -n ${SUDO_USER:-} ]]; then usermod -aG docker "$SUDO_USER"; fi

# Kernel modules: WireGuard for the tunnels, netem for the underlay faults.
cat > /etc/modules-load.d/exaconnect.conf <<'MODS'
wireguard
sch_netem
MODS
modprobe wireguard
modprobe sch_netem

if ! command -v containerlab >/dev/null; then
  # Official installer; adds the containerlab apt repository.
  bash -c "$(curl -sL https://get.containerlab.dev)"
fi

echo
docker --version
containerlab version | head -3
echo "Done. Log out and back in (docker group), then run: make lab-up"
