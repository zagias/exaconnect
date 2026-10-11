#!/usr/bin/env bash
# Builds the ExaCarib box image for x86 boxes (ADR 0047) from Debian 12's
# official cloud image: Debian with FRR 10, WireGuard, nftables, the Connect
# agent and the box console, ready to boot into set-up. Output, in <output dir>:
#   exacarib-box-<version>-amd64.img.xz  for a USB stick or the box's own disk
#   exacarib-box-<version>-amd64.qcow2   for a virtual machine
# The image is booted once in QEMU (KVM when there is /dev/kvm) to install
# everything with its own apt; libguestfs only puts the files in. Needs
# libguestfs-tools, qemu-utils and qemu-system-x86 (CI installs them on Ubuntu).
#
#   deploy/box/build-image.sh <exacarib-box_*_amd64.deb> <output dir> [version]
set -euo pipefail
deb=$1 out=$2 ver=${3:-0.0.0-dev}
here=$(cd "$(dirname "$0")" && pwd)
base_url=https://cloud.debian.org/images/cloud/bookworm/latest
base=debian-12-nocloud-amd64.qcow2
[[ -f $deb ]] || { echo "no package at $deb"; exit 2; }
mkdir -p "$out"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "== Debian 12 cloud image"
curl -fsSL -o "$work/$base" "$base_url/$base"
curl -fsSL -o "$work/SHA512SUMS" "$base_url/SHA512SUMS"
(cd "$work" && grep " $base\$" SHA512SUMS | sha512sum -c --quiet)
# The 3 GB disk is room enough here; each box grows it to its own disk at first boot.

echo "== files for the one-off build boot"
export LIBGUESTFS_BACKEND=direct
virt-customize -q -a "$work/$base" --no-network \
  --copy-in "$deb:/root" --run-command "mv /root/$(basename "$deb") /root/exacarib-box.deb" \
  --upload "$here/files/image-setup.sh:/root/exacarib-build.sh" --chmod 0755:/root/exacarib-build.sh \
  --mkdir /usr/lib/exacarib \
  --upload "$here/files/growroot:/usr/lib/exacarib/growroot" --chmod 0755:/usr/lib/exacarib/growroot \
  --upload "$here/files/exacarib-growroot.service:/etc/systemd/system/exacarib-growroot.service" \
  --upload "$here/files/exacarib-build.service:/etc/systemd/system/exacarib-build.service" \
  --link /etc/systemd/system/exacarib-build.service:/etc/systemd/system/multi-user.target.wants/exacarib-build.service \
  --link /dev/null:/etc/systemd/system/serial-getty@ttyS0.service \
  --write '/etc/systemd/network/99-exacarib-build.network:[Match]
Name=e*

[Network]
DHCP=yes
'

echo "== build boot (installs and updates with the image's own apt)"
accel=tcg
[[ -w /dev/kvm ]] && accel=kvm
timeout 3600 qemu-system-x86_64 -machine "q35,accel=$accel" -cpu max -m 2048 -smp 2 -no-reboot \
  -display none -monitor none -serial "file:$work/build.log" \
  -drive "file=$work/$base,if=virtio" -netdev user,id=n -device virtio-net-pci,netdev=n || true
if ! tr -d '\r' <"$work/build.log" | grep -q '^EXACARIB-BUILD-OK'; then
  echo "the build boot did not finish; the end of its set-up log:"
  tr -d '\r' <"$work/build.log" | grep -av '^\[' | tail -n 80
  exit 1
fi

name=exacarib-box-$ver-amd64
echo "== $name"
qemu-img convert -c -O qcow2 "$work/$base" "$out/$name.qcow2"
qemu-img convert -O raw "$work/$base" "$work/$name.img"
xz -T0 -6 -c "$work/$name.img" >"$out/$name.img.xz"
(cd "$out" && sha256sum "$name.qcow2" "$name.img.xz" >"$name.sha256")
ls -l "$out"
