# ADR 0051: Box ports set from the Connect portal

Date: 2026-10-12. Status: accepted.

## Context

Step 2 of box cloud management (ADR 0049): every box setting changed from the
portal, starting with the ports (which port is Internet link 1, 2, Satellite or
the office network; DHCP, fixed address or PPPoE; handing out addresses on the
office network). A port change can cut a box off. The box console already
protects against that by undoing an unconfirmed change after 2 minutes.

## Decision

- The portal's copy of a box's ports lives on the node (`nodes.box_network`,
  `{network, at, by}`), in the box console's own format, and goes down in desired
  state as `box.network`. The controller checks it with the same rules as the box
  (`box.check_network`), so mistakes show at once in the portal.
- The box takes it only while managed from the cloud or both, and only when it
  is newer than its own ports, than the last portal set-up it handled, and than
  the last "Managed from" switch (switching keeps the box's set-up as it is).
- Safety: the box applies it, then checks every 3 seconds that it can still
  connect to the controller address it joined with. Once it can, it keeps the
  change. If it never can, the usual 2-minute undo puts the earlier ports back.
  The outcome (`trying`, `kept`, `undone`, `refused`, `replaced`) is saved on the
  box, so an undone change is never retried, and goes up with telemetry
  (`box.portal`), with events `box_portal_applied`, `box_portal_undone` and
  `box_portal_refused`. The portal shows it on the site page.
- In "both" mode a later change on the box wins, as with every other setting:
  the box's ports carry their own time (`network_at`).
- The box now reports all its ports and each set-up port's address, gateway, DNS
  and PPPoE user name, so the portal's form starts from what the box has.
- PPPoE passwords: shown as "set" everywhere. A password typed in the portal goes
  down to the box in desired state (over mutual TLS) and is removed from the node
  and from every stored desired-state version as soon as the box reports it has
  handled that set-up. After that "set" means "keep the one the box has".

## Consequences

- Desired state can hold a PPPoE password for a short time; it is the one
  exception to "no secrets in desired state", and it is scrubbed afterwards.
- Monitoring settings, the box's admin account and firmware follow as later steps.
