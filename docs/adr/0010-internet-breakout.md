# ADR 0010: Internet breakout, NAT gateway and firewall

Date: 2026-10-04. Status: accepted.

## Context

Step 3 of ExaConnect Fabric. Until now a site's internet traffic was not
ExaConnect's business: whatever default route the host had, it used. Customers
want to choose where it leaves, have a NAT gateway and a basic firewall they
control, and publish a server without opening their sites to the internet.

## Decision

**Breakout per site**, three choices:

- **Through the PoP** (the default): internet traffic rides the overlay to
  the PoP, which is the customer's NAT gateway and firewall. One place to
  set policy, and port forwards work. The site picks the first tunnel with
  BFD up, so it fails over with the tunnels.
- **Straight out at the site**: internet traffic leaves by the site's own
  carrier links, NAT and firewall on the site's agent. Less latency and no
  PoP bandwidth; it fails over between links on BFD, since a tunnel's BFD
  going down is the best sign that carrier is failing.
- **Off**: no internet from the site's LAN.

**Only LAN-sourced traffic, and only what would use a default route.** Two
`ip rule`s per LAN prefix, after the steering rules: first `lookup main
suppress_prefixlength 0` (so the customer's own networks still follow BGP
and steering), then a table holding only the internet default. The host's
own traffic (agent to controller, WireGuard, IKE) is never touched, so a
mistake here cannot cut a site off from its controller.

**A site breaking out locally stops classifying internet traffic.** The
steering map carries the customer's own prefixes, and the classifier
returns early for anything else; otherwise a class's path table would
pull, for example, voice to a public service into a tunnel.

**Firewall rules** are the customer's, ordered, first match wins, then
allow. Each has an optional site, source, destination, protocol and ports.
They are applied where traffic leaves for the internet: at the PoP for
sites going through it, at the site for sites going straight out. Unsolicited
inbound connections are dropped at both. Rules carry nftables counters,
reported with telemetry, so the portal shows hits per rule.

**Port forwards** listen on the PoP's public address and reach an address
at a site. The public address is shared by every customer on the PoP, so a
public port is unique across customers. A forward only works while its site
goes through the PoP (the reply must come back through the PoP to be
translated), so it is left out otherwise and the portal says why.

## Consequences

- The PoP needs an internet interface and next hop (`internet_interface`,
  `internet_gateway`), and each link a carrier next hop
  (`underlay_gateway`) for local breakout; all are in the admin forms.
- Changing every existing site to "through the PoP" changes where their
  internet traffic goes. In the lab nothing used it; on a real deployment
  this is a planned change.
- IPv4 only, like the rest of the overlay. No application-aware rules,
  URL filtering or IDS: that is beyond a basic firewall.
- The lab has a stand-in internet address on every carrier router and
  behind the PoP; lab check `m9-internet.sh` covers both breakouts, a rule,
  a forward and failover. The contract is `docs/internet-contract.md`.
