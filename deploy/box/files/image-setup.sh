#!/bin/sh
# Runs once inside the Debian image, booted in a VM by deploy/box/build-image.sh
# (ADR 0047): FRR from the FRR project, networking by netplan and networkd,
# ports named eth0, eth1... in the order the hardware lists them, and the
# exacarib-box package with everything it needs. Then it tidies up so each box
# makes its own identity at first boot.
set -eux
export DEBIAN_FRONTEND=noninteractive
# The image's own daily apt runs must not hold the package lock while this runs.
systemctl stop apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service unattended-upgrades.service 2>/dev/null || true
echo 'DPkg::Lock::Timeout "600";' >/etc/apt/apt.conf.d/10exacarib-build
# The build VM's network (99-exacarib-build.network, put there by build-image.sh).
systemctl start systemd-networkd
for _ in $(seq 1 60); do ip -4 route show default | grep -q . && break; sleep 1; done
rm -f /etc/resolv.conf
echo "nameserver 10.0.2.3" >/etc/resolv.conf
# Plain http until ca-certificates is in (apt checks every package's signature anyway).
sed -i 's#https://#http://#' /etc/apt/mirrors/*.list
apt-get update -q
apt-get install -y curl gnupg ca-certificates
sed -i 's#http://#https://#' /etc/apt/mirrors/*.list
curl -fsSL https://deb.frrouting.org/frr/keys.gpg -o /usr/share/keyrings/frrouting.gpg
echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr bookworm frr-10" >/etc/apt/sources.list.d/frr.list
apt-get update -qq
apt-get -y -qq dist-upgrade >/dev/null
apt-get install -y -qq linux-image-amd64 netplan.io systemd-resolved frr frr-pythontools >/dev/null
# The image disk: two root slots and GRUB for BIOS and UEFI (ADR 0049). The
# cloud kernel lacks drivers for real boxes, and grub-pc would reinstall GRUB
# into one slot on its upgrades: both go; GRUB stays as installed on EXABOOT.
apt-get install -y -qq gdisk dosfstools rsync parted grub-pc-bin grub-efi-amd64-bin grub2-common zstd >/dev/null
echo "linux-base linux-base/removing-running-kernel boolean false" | debconf-set-selections
apt-get purge -y -qq grub-pc grub-efi-amd64 grub-efi-amd64-signed shim-signed cloud-guest-utils 'linux-image-*cloud*' >/dev/null 2>&1 || true
apt-mark manual grub2-common grub-pc-bin grub-efi-amd64-bin >/dev/null
echo "MODULES=most" >/etc/initramfs-tools/conf.d/90-exacarib
printf 'do_symlinks = yes\nlink_in_boot = no\n' >/etc/kernel-img.conf
kv=$(find /boot -maxdepth 1 -name 'vmlinuz-*-amd64' ! -name '*cloud*' -printf '%f\n' | sed 's/^vmlinuz-//' | sort -V | tail -n1)
update-initramfs -c -k "$kv" 2>/dev/null || update-initramfs -u -k "$kv"
ln -sfn "boot/vmlinuz-$kv" /vmlinuz
ln -sfn "boot/initrd.img-$kv" /initrd.img
rm -rf /boot/grub /etc/default/grub.d
apt-get install -y -qq /root/exacarib-box.deb >/dev/null
rm -f /root/exacarib-box.deb
systemctl stop exa-box exacarib-box-banner.timer || true
# networkd and netplan only: no ifupdown, no cloud networking left over.
apt-get purge -y -qq ifupdown >/dev/null 2>&1 || true
rm -f /etc/network/interfaces /etc/netplan/*.yaml /etc/systemd/network/*.network
systemctl enable systemd-networkd systemd-resolved frr exa-box exacarib-box-ports exacarib-setup-dhcp exacarib-box-banner.timer exacarib-update-confirm
sed -i -e 's/^bgpd=no/bgpd=yes/' -e 's/^bfdd=no/bfdd=yes/' /etc/frr/daemons
# Port names eth0, eth1... come from net.ifnames=0 in grub-ab.cfg.
mkdir -p /etc/exacarib
dpkg-query -W -f='${Version}\n' exacarib-box >/etc/exacarib/release
echo "ExaCarib box" >/etc/motd
# No password logins on the screen or anywhere else: the box console is the way in.
passwd -l root
hostnamectl set-hostname exacarib-box 2>/dev/null || echo exacarib-box >/etc/hostname
# Each box makes its own identity at first boot.
systemctl disable exacarib-build.service
# The serial login was held off while the build wrote to the serial console.
rm -f /etc/systemd/system/exacarib-build.service /etc/systemd/system/serial-getty@ttyS0.service
rm -rf /var/lib/exaconnect/* /etc/ssh/ssh_host_* /var/lib/dbus/machine-id
truncate -s 0 /etc/machine-id
apt-get clean
rm -f /etc/apt/apt.conf.d/10exacarib-build /etc/issue.d/50-exacarib.issue
rm -rf /var/lib/apt/lists/* /var/log/journal/* /var/log/exacarib-build.log
# Last: copy this system onto the image disk as slot A.
sh /root/ab-disk.sh
