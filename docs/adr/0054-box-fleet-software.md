# ADR 0054: box software updates from the Connect portal

Status: accepted, 2026-10-12

## Context

ExaCarib boxes installed from the box image can update themselves safely: the
new software goes into the spare root slot, the box restarts into it and keeps
it only if it comes up healthy (ADR 0052). Dudley asked for Meraki-style cloud
management, which includes updating many boxes from the portal and rolling back.

## Decision

- The portal asks boxes for a version: Connect › Set up › Box software, or
  `POST /api/v1/box-software {site_ids, version}`. The request is kept on the
  node (`nodes.box_software` = `{version, at, by}`) and goes down in desired
  state as `box.software`. Going back fleet-wide is asking for an earlier version.
- Versions come from the GitHub releases `box-v<version>` that carry an update
  file (`*-amd64.root.tar.zst`), cached for ten minutes on the controller.
- The box takes a request only while managed from the cloud or both (ADR 0049),
  once (newer than the last one it handled and than the last "Managed from"
  switch), and installs it with the console's own `StartUpdate`. The box
  resolves the version and checks the SHA-256 itself; the controller never
  sends a download address.
- The box saves the request (`console-software.json`) and reports what became
  of it as `box.software_portal`: installing, done, rolled_back (restarted but
  still on the old version after the health check), failed (did not install),
  refused (not the box image, or a version that does not exist) or current.
- Boxes managed on site are refused by the controller; they update from their
  own console.

## Consequences

- A box installed with the `.deb` on its own Debian or Ubuntu says it updates
  with apt; the portal shows that.
- The lab box runs from a container, so the live test checks the request, the
  box's answer and the portal's display. A real install and automatic rollback
  are proven by the box image test (ADR 0052).
- There is no staged rollout yet (for example ten per cent first); boxes ticked
  together update together.
