#!/bin/sh
# Runs inside the Debian image while deploy/box/build-image.sh customises it
# (ADR 0047): FRR from the FRR project, networking by netplan and networkd,
# ports named eth0, eth1... in the order the hardware lists them, and the
# exacarib-box package with everything it needs.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# Use the 4 GB the image was resized to (build-image.sh); the first boot grows it to the disk.
apt-get install -y -qq cloud-guest-utils >/dev/null
root=$(findmnt -no SOURCE /)
growpart "/dev/$(lsblk -no PKNAME "$root")" "$(cat "/sys/class/block/${root##*/}/partition")" >/dev/null
resize2fs "$root" >/dev/null 2>&1
apt-get install -y -qq curl gnupg ca-certificates >/dev/null
curl -fsSL https://deb.frrouting.org/frr/keys.gpg -o /usr/share/keyrings/frrouting.gpg
echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr bookworm frr-10" >/etc/apt/sources.list.d/frr.list
apt-get update -qq
apt-get -y -qq dist-upgrade >/dev/null
apt-get install -y -qq netplan.io systemd-resolved frr frr-pythontools >/dev/null
apt-get install -y -qq /tmp/exacarib-box.deb >/dev/null
rm -f /tmp/exacarib-box.deb
# networkd and netplan only: no ifupdown, no cloud networking left over.
apt-get purge -y -qq ifupdown >/dev/null 2>&1 || true
rm -f /etc/network/interfaces /etc/netplan/*.yaml /etc/systemd/network/*.network
systemctl enable systemd-networkd systemd-resolved frr exa-box exacarib-box-ports exacarib-setup-dhcp exacarib-box-banner.timer >/dev/null
sed -i -e 's/^bgpd=no/bgpd=yes/' -e 's/^bfdd=no/bfdd=yes/' /etc/frr/daemons
# Port names eth0, eth1... (stable on one box; the console shows which is which).
mkdir -p /etc/default/grub.d
# shellcheck disable=SC2016 # expanded by grub, not here
echo 'GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX net.ifnames=0 biosdevname=0"' >/etc/default/grub.d/90-exacarib.cfg
update-grub >/dev/null 2>&1
echo "ExaCarib box" >/etc/motd
# Each box makes its own identity at first boot.
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_* /var/lib/exaconnect/* 2>/dev/null || true
apt-get clean
rm -rf /var/lib/apt/lists/*
