# 0047 The ExaCarib box image, package and the box's own firewall

Date: 2026-10-11
Status: accepted

## Context

After the box console (ADR 0046) Dudley asked whether it "can be packaged with
Linux", and whether it covers SD-WAN, basic security and DDoS. A box bought as
bare hardware needed Debian or Ubuntu installed by hand, then FRR, WireGuard and
the agent, before anyone could open the console. And nothing protected the box
itself: the agent's firewall (`exa_inet`, ADR 0012) covers traffic going through
the box, not traffic to it.

## Decision

1. **A Debian package, `exacarib-box`** (`deploy/box/build-deb.sh`, arm64 and
   amd64): the agent in `/usr/bin`, the `exa-box` and `exa-agent` units, the box's
   defaults and firewall settings, and dependencies on FRR, WireGuard, nftables,
   netplan, PPPoE, dnsmasq and unattended-upgrades. `apt install ./exacarib-box_*.deb`
   on Debian 12 or Ubuntu 22.04+ turns the box console on (or leaves a running
   `exa-agent` site alone).
2. **An image for x86 boxes** (`deploy/box/build-image.sh`): Debian 12's official
   cloud image (BIOS and UEFI, 3 GB, grown to the disk at first boot), booted once in QEMU to install with its own apt
   (libguestfs only puts the files in; its own networking does not run on GitHub's
   runners), updated, with FRR 10 from the FRR project and the
   package, networking by netplan and systemd-networkd, ports named eth0, eth1… in
   hardware order, root password locked and no SSH. Each box makes its own machine
   id, setup code and console certificate at first boot, and grows its disk.
   Published as `.img.xz` (USB stick or the box's disk) and `.qcow2` (VMs).
3. **First boot needs no screen or keyboard.** eth0 takes an internet address by
   DHCP; on a box with two or more ports the last is the office port, 192.168.50.1/24,
   handing out addresses until the console is set up. Someone plugs a laptop into it
   and opens https://192.168.50.1. The box's screen shows that address and, until the
   admin password is set, the setup code. The console's own port settings
   (`90-exacarib.yaml`) take over from these defaults (`80-exacarib-default.yaml`).
4. **The box's own firewall, `inet exa_box`**, written by the console on every
   start and port change: input policy drop; tunnels (`wg-*`), loopback and replies
   allowed; everything new on an internet port dropped; the console, DHCP and DNS on
   the other ports only. As on the PoP (ADR 0012): an address opening console
   connections faster than 50 a second is dropped for 10 minutes, TCP SYNs are
   limited to 2,000 a second overall, pings to 10 a second, and SYN cookies are on.
5. **Security updates install by themselves** every day (unattended-upgrades,
   Debian security only). The box does not reboot by itself.
6. **Built, tested and published by GitHub Actions** (`box-image.yml`): the
   package installs on clean Debian 12 and Ubuntu 24.04, the image boots in QEMU and
   is set up through the console API (`deploy/box/test-image.sh`), then a release
   `box-v1.0.<run>` carries the image, the packages and checksums. GitHub Releases is
   free for a public repository. The lab (`lab/ci/checks/zz-box-image.sh`) boots the
   published image on the lab host and joins it to live Connect through the browser.

## DDoS, honestly

The box and the PoP shrug off small floods aimed at them. A volumetric attack fills
the internet line before it reaches either, so it has to be stopped upstream: by the
carrier (blackholing, RTBH, which ADR 0012 leaves as a seam until a carrier offers
the BLACKHOLE community) or by a scrubbing service such as Cloudflare Magic Transit.
Both cost money and are Dudley's call.

## Consequences

- Port names on the image are eth0, eth1… not the predictable names; the console
  shows each port's MAC address and link so the labels on the box can be matched.
- An IPS (Suricata) is not in the image: most branch boxes cannot inspect traffic at
  line rate as well as route it. It can come later as an option for bigger boxes.
- Arm boxes use the package on their own Debian or Ubuntu; there is no Arm image yet.
