# 0046 Box console: a local web UI on the ExaCarib box

Date: 2026-10-11
Status: accepted

## Context

Dudley asked for "a whitebox UI for ExaCarib". Branch whiteboxes (uCPE: small x86
or Arm appliances) usually run plain Ubuntu or Debian with FRR, which is what the
Connect agent already runs on. Until now a box joined Connect only through
`install.sh` and a root shell, and nothing on the box showed its state. Commercial
SD-WAN edges all have a local page for the first boot and for the day the link to
the cloud is down: someone at the branch picks the WAN and LAN ports, joins the box
with a code from the cloud portal, and can see links, tunnels and routes and run a
ping without calling the NOC.

## Decision

1. **The agent serves it.** `exa-agent run --console :443` (unit
   `deploy/systemd/exa-box.service`, `install.sh --box`) serves a small web app over
   HTTPS from the agent binary itself (`agent/internal/console`, plain HTML, CSS and
   JavaScript embedded with `go:embed`; no build step, no new dependency). Until the
   box has joined, it serves only the console; once joined it runs the agent loop as
   before, in the same process, so the console reads live state (controller contact,
   applied version, tunnels, BFD, probes, steering) without another API.
2. **One local account, claimed with a setup code.** A fresh box makes a 10-character
   setup code (`exa-agent console-code` prints it; a factory can print it on the label).
   Whoever has it sets the `admin` password (at least 10 characters), PBKDF2-HMAC-SHA256
   with 210,000 iterations and a random salt, stored 0600 in the state directory.
   Sessions are random 256-bit cookies (Secure, HttpOnly, SameSite=Strict), 30 minutes
   idle and 12 hours at most. Five wrong passwords lock that address out for a minute.
   `exa-agent console-reset` (root on the box) forgets the password and makes a new code.
3. **Office side only.** The console refuses any request that arrives on a port set as
   an internet (WAN) port, and any from a public address outside the office network.
   Writes need the console's own `X-Exa-Box` header, so another site's page cannot post
   to it; the content security policy allows only the console's own files. The
   certificate is self-signed (the browser warns once); the console and
   `exa-agent console-code` show its SHA-256 to compare.
4. **Port wizard.** Each port is marked as Internet link 1, Internet link 2, Satellite,
   Office network or not used, with DHCP, a fixed address, PPPoE (pppd) or "leave as it
   is"; the office port can hand out addresses (dnsmasq). It writes netplan
   (`/etc/netplan/90-exacarib.yaml`) where the box has it, and ip commands otherwise.
   Like a router, a change that is not confirmed from the browser within two minutes is
   undone, so a wrong address cannot strand the console.
5. **Join with one paste.** The portal's install panel (Organisation › Locations ›
   Connect this location, and Operations › Sites and links) now leads with a **box setup
   code**: `EXA1.` and base64url JSON of the controller's agent address, the one-time
   token, the CA fingerprint and the site name. The console's join step takes the code
   (or the four values by hand) and enrols exactly as `exa-agent enrol` does. The
   install command stays for boxes installed by hand.
6. **The portal knows the box is managed locally.** With each telemetry flush the agent
   sends `box`: that the console runs, its address, the ports and their roles, and the
   last change made there. The controller keeps it on the node (`nodes.box`); the
   Agents list, the location and the site page show a "Box console" tag and the last
   local change; every change made on the box is also an event ("Changed on the box").
   The controller follows the box's port choice: each link's underlay interface becomes
   the port the box set for that path, so metering counts the right port.
7. **Tested end to end on every lab run** (`lab/ci/checks/box-console.sh` and
   `deploy/uat/box-console.mjs`): a throwaway box from the lab node image with an
   internet port and an office port, the setup code, the wizard (fixed addresses on
   both ports), joining a test organisation through the public agent gateway with the
   code from the portal, the box's status and diagnostics, sign-out and sign-in, a
   refused request on the internet port, then the portal's Agents, Locations and site
   page, and the controller's copy of the box's report and port choice.

## Consequences

- The console makes no change the controller does not hear about, and the controller
  stays the brain: tunnels, routing and steering still come only from desired state.
- Ports set on the box are local state. A box replaced or reinstalled is set up again
  from its console; the portal shows what the old one reported until the new one joins.
- Out of scope for now: Wi-Fi and LTE modem set-up, VLANs on the office port, more
  than one local account, and SSO for the console.
- First boot: the console answers on any private address the box already has (for
  example from the office router's DHCP on the port that will be the office port).
  A factory image can give one port a fixed starting address.
