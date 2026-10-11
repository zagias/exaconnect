#!/usr/bin/env bash
# Builds the ExaCarib box image for x86 boxes (ADR 0047) from Debian 12's
# official cloud image: Debian with FRR 10, WireGuard, nftables, the Connect
# agent and the box console, ready to boot into set-up. Output, in <output dir>:
#   exacarib-box-<version>-amd64.img.xz  for a USB stick or the box's own disk
#   exacarib-box-<version>-amd64.qcow2   for a virtual machine
# Needs root, libguestfs-tools and qemu-utils (CI installs them on Ubuntu).
#
#   sudo deploy/box/build-image.sh <exacarib-box_*_amd64.deb> <output dir> [version]
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
# Room for updates and logs; the root file system grows to the whole disk at first boot.
qemu-img resize -q "$work/$base" 4G
cp "$deb" "$work/exacarib-box.deb"

echo "== ExaCarib box on top"
export LIBGUESTFS_BACKEND=direct
virt-customize -a "$work/$base" --network --memsize 2048 \
  --hostname exacarib-box --timezone UTC --root-password disabled \
  --copy-in "$work/exacarib-box.deb:/tmp" \
  --run "$here/files/image-setup.sh" \
  --upload "$here/files/growroot:/usr/lib/exacarib/growroot" \
  --chmod 0755:/usr/lib/exacarib/growroot \
  --firstboot-command /usr/lib/exacarib/growroot

name=exacarib-box-$ver-amd64
echo "== $name"
qemu-img convert -c -O qcow2 "$work/$base" "$out/$name.qcow2"
qemu-img convert -O raw "$work/$base" "$work/$name.img"
xz -T0 -6 -c "$work/$name.img" >"$out/$name.img.xz"
(cd "$out" && sha256sum "$name.qcow2" "$name.img.xz" >"$name.sha256")
ls -l "$out"
