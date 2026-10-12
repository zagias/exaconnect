# ADR 0052: Box port templates

Date: 2026-10-12. Status: accepted.

## Context

Step 3 of box cloud management (ADR 0049): one set-up for many sites. An
organisation with twenty branches wants every box wired the same way and to
change them all at once.

## Decision

- A template belongs to one organisation (`box_templates`: name and a port set-up
  in the box console's format). A box follows at most one (`nodes.box_template_id`).
- Making a box follow a template, and every later change to the template, puts
  the template's ports on that box through the same path as ports set in the
  portal (ADR 0051): `box.network` in desired state, applied with the box's
  reach-or-undo safety. The result is reported per location, and a box that
  cannot take it (a port it does not have, managed on site) is listed with the
  reason and left as it is.
- Office addresses differ per location, so a template's office port may use the
  address `auto`: the first address of the location's own office network (its
  first LAN prefix). PPPoE sign-ins differ per location too, so a template holds
  none; set them on each box, or use "Leave as it is" in the template.
- Setting a box's ports directly on its site page takes it off its template.
  Removing a template, or a box stopping following it, leaves the box's ports as
  they are.
- A template can be made from a box's current ports ("Save as a template" on the
  site page) or built on the new Box templates page under Connect › Set up.

## Consequences

- Templates cover ports only for now; monitoring settings join them when those
  can be set from the portal.
