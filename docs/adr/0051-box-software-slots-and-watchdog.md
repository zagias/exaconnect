# 0051 Box software slots (A/B updates that undo themselves) and watchdogs

Date: 2026-10-12
Status: accepted

## Context

A branch box must never be bricked by an update, and must restart itself if it
hangs: nobody is on site, and the box is the site's only way to reach us.
FortiGate and Meraki keep two firmware images and boot the old one when the new
one fails. The ExaCarib box image (ADR 0047) had one root file system and no
watchdog.

## Decision

- **Disk layout** (deploy/box/files/ab-disk.sh): BIOS boot, `EXABOOT` (the EFI
  system partition: GRUB for BIOS and UEFI, `grub.cfg`, `grubenv`), `exaroot-A`
  and `exaroot-B` (3 GB each). The image ships with A; B is empty.
- **Boot** (files/grub-ab.cfg): GRUB boots `slot` from grubenv. If `try` is set
  it boots that slot instead, once: GRUB clears `try` before starting it. The
  kernel gets `exacarib.slot=` and `panic=10`.
- **Install** (`/usr/lib/exacarib/exacarib-update install <url> <sha256>`):
  formats the other slot, streams the release's `root.tar.zst` into it while
  hashing it, refuses it if the SHA-256 differs, carries this box's settings
  over (state directory with identity and keys, netplan, PPPoE, office DHCP,
  SNMP, syslog, FRR, machine ID, host name, zero-touch address), and sets `try`.
- **Keep or undo** (`exacarib-update-confirm.service` at every boot): on a
  tried slot the box must answer `GET /api/health` (loopback; once joined,
  Connect must have answered too) for 2 minutes without a break, within 7
  minutes. Then the slot becomes `slot`. Otherwise the box restarts into the
  old slot. A tried slot that panics or hangs never gets this far: the panic
  restart or the watchdog reset boots the old slot, which records that the
  update did not start.
- **Watchdogs**: systemd drives the hardware watchdog (`RuntimeWatchdogSec=30s`,
  `RebootWatchdogSec=10min`): a box frozen for 30 s is reset. `exa-box.service`
  has `WatchdogSec=90`; the agent pings systemd every 20 s while its console
  answers, so a hung agent is restarted. Kernel panics and oopses restart the
  box after 10 s.
- **Where updates come from**: GitHub releases `box-v<version>` carry
  `exacarib-box-<version>-amd64.root.tar.zst` and its SHA-256 in the `.sha256`
  file. The box console's Settings > Software checks for the newest release,
  installs it, and can go back to the other slot's software.
- **API** (box console, signed in): `GET /api/update` (slots, versions, trial,
  last result, watchdog), `GET /api/update/latest`, `POST /api/update`
  (`{"version"}` from GitHub releases, or `{"url", "sha256", "version"}`),
  `POST /api/update/rollback`. In Go, `console.Server.StartUpdate(Release, by)`
  is the entry point for Connect's fleet management; the box reports its
  slots in telemetry (`box.software`) and sends `box_update_restarting` and
  `box_update_failed` events.
- While the box is managed from the Connect portal (ADR 0049), installing from
  the console is refused like any other local change; going back to the previous
  software stays allowed, as an emergency undo.
- Boxes installed from the .deb (not the image) get the watchdogs, not slots:
  they update with apt.

## Consequences

- The image test (`deploy/box/test-image.sh update`) installs a good update
  (kept, settings carried over), a broken one whose agent will not start (goes
  back after the health window) and one that hangs the kernel with panics
  turned off (the emulated i6300esb watchdog resets it and it goes back), on
  every image build.
- Secure Boot needs signed shim and GRUB: a seam. Boxes need Secure Boot off.
- Releases are trusted through HTTPS from GitHub plus the SHA-256 from the same
  release; signing releases (minisign or Sigstore) is the next step.
- Debian's own security updates still install in the running slot through
  unattended-upgrades; a slot update replaces the whole system.
