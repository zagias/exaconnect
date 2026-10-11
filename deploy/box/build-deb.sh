#!/usr/bin/env bash
# Builds the exacarib-box Debian package (ADR 0047): the Connect agent with the
# box console, the box's defaults and the packages it needs, for Debian 12+ and
# Ubuntu 22.04+. Install it on a box with `apt install ./exacarib-box_*.deb`.
#
#   deploy/box/build-deb.sh <arm64|amd64> <exa-agent binary> <output dir> [version]
set -euo pipefail
arch=$1 bin=$2 out=$3 ver=${4:-0.0.0~dev}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
[[ $arch == arm64 || $arch == amd64 ]] || { echo "arch must be arm64 or amd64"; exit 2; }
[[ -f $bin ]] || { echo "no binary at $bin"; exit 2; }
ver=${ver#v}
root=$(mktemp -d)
chmod 755 "$root"
trap 'rm -rf "$root"' EXIT

install -D -m 755 "$bin" "$root/usr/bin/exa-agent"
for u in exa-agent exa-box; do
  sed 's#/usr/local/bin/exa-agent#/usr/bin/exa-agent#g' "$repo/deploy/systemd/$u.service" |
    install -D -m 644 /dev/stdin "$root/lib/systemd/system/$u.service"
done
for u in exacarib-box-ports.service exacarib-setup-dhcp.service exacarib-box-banner.service exacarib-box-banner.timer; do
  install -D -m 644 "$here/files/$u" "$root/lib/systemd/system/$u"
done
install -D -m 755 "$here/files/exacarib-box-ports" "$root/usr/lib/exacarib/exacarib-box-ports"
install -D -m 755 "$here/files/exacarib-box-banner" "$root/usr/lib/exacarib/exacarib-box-banner"
install -D -m 644 "$here/files/90-exacarib-box.conf" "$root/usr/lib/sysctl.d/90-exacarib-box.conf"
install -D -m 644 "$here/files/networkd-wait-online.conf" "$root/lib/systemd/system/systemd-networkd-wait-online.service.d/exacarib.conf"
install -D -m 644 "$here/files/52exacarib-box-upgrades" "$root/etc/apt/apt.conf.d/52exacarib-box-upgrades"
install -d -m 755 "$root/usr/share/doc/exacarib-box"
cat >"$root/usr/share/doc/exacarib-box/copyright" <<'C'
ExaCarib box: the ExaCarib Connect agent and box console.
Copyright ExaCarib. See https://www.exacarib.com.
C

install -d "$root/DEBIAN"
size=$(du -sk "$root" | cut -f1)
cat >"$root/DEBIAN/control" <<C
Package: exacarib-box
Version: $ver
Architecture: $arch
Maintainer: ExaCarib <support@exacarib.com>
Installed-Size: $size
Depends: frr (>= 8.4), wireguard-tools, nftables, iproute2, netplan.io | ifupdown, ppp, dnsmasq-base, ca-certificates, iputils-ping, traceroute, unattended-upgrades
Section: net
Priority: optional
Homepage: https://www.exacarib.com
Description: ExaCarib Connect agent and box console for branch boxes
 Turns a Debian or Ubuntu box into an ExaCarib branch box: SD-WAN tunnels over
 every internet link with the Connect agent, a local web console on the office
 network to set the ports and join Connect, the box's own firewall and automatic
 security updates.
C
cat >"$root/DEBIAN/conffiles" <<C
/etc/apt/apt.conf.d/52exacarib-box-upgrades
C
install -m 755 "$here/files/postinst" "$root/DEBIAN/postinst"
install -m 755 "$here/files/prerm" "$root/DEBIAN/prerm"

mkdir -p "$out"
deb="$out/exacarib-box_${ver}_${arch}.deb"
dpkg-deb --root-owner-group -Zxz --build "$root" "$deb" >/dev/null
echo "$deb"
