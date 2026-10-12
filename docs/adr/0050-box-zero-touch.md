# 0050 Zero-touch installation of ExaCarib boxes

Date: 2026-10-12
Status: accepted

## Context

Dudley asked for a zero-touch installation. Until now a box joined Connect
through its console: someone at the branch plugged in a laptop, claimed the box
with the setup code on its label and pasted the box setup code from the portal
(ADR 0046). Meraki, Fortinet and Cisco ship boxes that need nothing on site: the
box is plugged into any internet line, calls the vendor's cloud, is recognised by
its serial number and pulls its configuration.

## Decision

1. **The box calls home.** While it has not joined, the agent with the box console
   (`exa-agent run --console`) calls `POST /api/v1/ztp/hello` every 30 seconds (every
   2 minutes after the first 10) on `https://connect.exacarib.com`, the portal's own
   address, which the box can check against the public certificate authorities it
   already trusts. `EXA_ZTP_URL` or `/etc/exacarib/ztp-url` points it elsewhere;
   `off` turns zero-touch off. The console keeps working alongside.
2. **Who it is.** The serial number from the firmware (DMI product, board or chassis
   serial, ignoring placeholders such as "To Be Filled By O.E.M."), else `MAC-` and
   the first port's MAC address; `EXA_BOX_SERIAL` overrides it. The box makes an
   Ed25519 key on first boot and signs each call (`exacarib-ztp-v1`, serial, time;
   10 minutes of clock skew allowed). The first key seen for a serial is kept.
   A call with another key is refused and shown to staff as a conflict until they
   release the serial (a copied serial, or a replaced box).
3. **Only staff decide where it goes.** Operations › Boxes lists every box that has
   called home or joined, across organisations, with its key (the console shows the
   same short key), MAC, model and address. Staff add boxes by serial before they
   arrive and assign each to a customer's Connect site. A serial and key prove which
   box is calling, not that it may join.
4. **Joining.** Once assigned, the next call's answer carries a one-time enrolment
   token (valid an hour, at most one a minute), the agent gateway's address and the
   CA fingerprint: what the console's join step takes. The box enrols as any agent
   does, then pulls its desired state. Its telemetry reports its serial, which links
   the row to its node, so the list shows it Online or Offline. Boxes that joined
   through their console appear the same way.
5. **Tested end to end on every lab run** (`lab/ci/checks/box-ztp.sh`,
   `deploy/uat/box-ztp.mjs`): a fresh box with only an internet port calls the live
   portal; staff assign it in Operations › Boxes in a browser; it must join, apply its
   desired state and show Online, with no admin password ever set on the box.

## Consequences

- A box that nobody assigns keeps calling and stays listed as waiting; staff can
  remove it. Calls are limited to 60 a minute per address.
- Ports stay as the image set them (first port internet by DHCP, last port the office
  network); changing them from the portal is the next step of ADR 0049.
- Someone who learns a serial and calls before the real box would be kept as that
  serial's key; the real box is then refused and staff see the conflict. Comparing
  the key on the box's screen or console with the list closes that gap; a factory key
  on the label would remove it, and is a seam for when boxes are built in volume.
- Customers cannot claim boxes by serial themselves yet; staff do it, so a mistyped
  or guessed serial cannot pull a box into another organisation.
