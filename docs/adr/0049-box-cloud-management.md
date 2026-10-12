# 0049 Cloud management of ExaCarib boxes: "Managed from"

Date: 2026-10-12
Status: accepted

## Context

Dudley chose Meraki-style cloud management for ExaCarib boxes: every box setting
changed from the Connect portal, templates across sites, fleet firmware with
rollback, and one device list for ExaCarib staff with zero-touch claim. Until now a
box's own settings (its ports) could only be changed in its box console (ADR 0046);
the portal showed them. Some customers want the branch to stay in charge, some want
head office or ExaCarib to run every box centrally, and some want both. Whoever is
in charge must be able to change their mind either way at any time, without the box
losing its set-up. This ADR is the first step: who may change a box's settings.

## Decision

1. **One setting per box, "Managed from"**, with three choices:
   - **On site** (`site`): only the box console changes the box's settings.
   - **Cloud** (`cloud`): only the portal does. The console shows everything but
     refuses changes (409), apart from an emergency **Take back control**, which
     needs the admin password again and switches the box to On site.
   - **On site and cloud** (`both`, the default): either may change them; the
     latest change wins.
   Tunnels, routing and steering always come from the controller, whatever the
   setting. Switching never changes the box's settings themselves.
2. **Newest wins, both ways.** The portal's copy is on the node (`nodes.box_managed`:
   mode, time, who) and goes to the box in desired state (`box.managed_from`). The
   box's copy is in `console-managed.json` and comes up with telemetry
   (`box.managed`). Each side keeps whichever is newer, so a switch made on either
   side reaches the other however long they were cut off. Times are kept to the
   microsecond on both sides so the copies settle and stop. A switch made on a side
   whose clock is behind the other's is still made newest (the last time plus 1 ms).
3. **Who.** The organisation's owners and admins and ExaCarib staff switch it in the
   portal (`POST /api/v1/sites/{id}/box/managed`, on the site page), members read it.
   On the box, the console's admin. Every switch is audited (`box.managed`); a portal
   switch is also an event, and a box switch is a "Changed on the box" event.
4. **The agent hands the box block to the console** with every new desired state,
   whether or not the rest of it applied (`Agent.BoxDesired`), so a broken tunnel
   config never stops a box being handed back.
5. **Tested end to end on every lab run**: the box-console walk-through switches the
   new box to Cloud in the portal, checks the console turns read-only and refuses a
   port change, takes control back on the box with a wrong and then the right
   password, checks the portal follows, then sets both from the portal and checks the
   box follows.

## Next steps (each its own change)

- Every box setting from the portal: ports first, then the box's monitoring
  (ADR 0048), carried in the same `box` block, each part with its own time, applied
  only when the box is managed from the cloud or both. A port change from the portal
  keeps the console's two-minute undo: the box keeps it only once it reaches the
  controller again.
- Templates that set the same box settings on many sites.
- Fleet firmware, on the box's own safe A/B update mechanism once it lands.
- A staff device list across organisations, with zero-touch claim by serial number.

## Consequences

- A box that never had the setting is managed from both, which is how boxes worked
  before: settings on the box, routing from the cloud.
- Cloud mode does not stop someone with root on the box; it is about who is in
  charge, not a lock. The emergency take-back is always there, and the portal sees it.
