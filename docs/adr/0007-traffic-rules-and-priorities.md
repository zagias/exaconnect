# ADR 0007: Traffic rules, priority queues and application detection

Date: 2026-10-01. Status: accepted.

## Context

Dudley asked for customers to choose what traffic is prioritised:
applications, IP addresses, VLANs and sites (branch sites and websites).
He also asked for the network to recognise applications itself and
prioritise them. Until now, classes matched only DSCP, ports and subnets,
and only an admin could change them. Nothing queued traffic by importance
when a link was full; steering chose a path but did not decide what waits.

## Decision

- **Classes say how traffic is treated; rules say which traffic.** A class
  has a queue priority (`realtime`, `interactive`, `normal`, `bulk`), an
  SLA and an optional preferred path. Customers manage their own classes. The
  three seeded classes are built in: they can be edited but not deleted.
- **Traffic rules** (`traffic_rules`, `controller/.../traffic.py`). A rule
  names any of: applications from a catalogue, ports, destination and source
  subnets (IPv4), VLAN IDs, websites (domain names) and DSCP marks. It can be
  limited to some sites. Inside a rule every field given must match; a
  destination subnet or website both count as the destination. A rule naming
  applications becomes one match per application signature. Rules compile
  into the steering map's `matches`, rendered by the agent as nftables rules
  ahead of the classes' own matches. Rules without VLANs are mirrored for
  the return direction. The PoP gets the rules of every site it serves.
- **Websites** are resolved by the agent: on map change and every 5 minutes,
  off the steering path, so a slow DNS server never delays failover. A
  failed lookup keeps the last good addresses. Traffic to a CDN address
  shared with other sites will match too; that is the honest limit of
  matching websites without inspecting traffic.
- **VLANs** match the host's `<parent>.<vid>` subinterfaces by name, since
  nftables only treats `*` as a wildcard at the end of a name.
- **Priority queues.** Each priority maps to a DSCP mark set on the way into
  the tunnel (EF 46, AF31 26, CS1 8; normal is not rewritten). Each tunnel
  gets a CAKE qdisc in `diffserv4` mode, shaped to 95% of the link's speed
  (`links.shape_mbps`), so the queue builds where CAKE can sort it. A tc
  failure never blocks steering; it is reported as a `qos_failed` event.
- **Preferred path.** A class's preferred path comes first among equals in
  its candidates and in the engine's evaluation (terrestrial still before
  satellite). The engine still moves the class off it when the SLA needs to.
- **Application detection** (`ai/apps.py`, `ai/detect.py`). Site agents
  report every minute what leaves the site per protocol, destination and
  port, from connection tracking, with the class each flow was marked as.
  Every 5 minutes the controller labels the last 30 minutes:
  - by the catalogue (ports and published address ranges of Teams, Zoom,
    Webex, Meet, SIP, RDP, Citrix, Horizon, SAP, databases, SSH, DNS, backup
    tools), at 90% confidence;
  - otherwise by behaviour on a fixed server port: a steady stream of small
    UDP packets both ways is real-time; a large one-way transfer of full
    packets is bulk, at 60%.
  It suggests the matching built-in class when the traffic is not already
  in it, with the reason in plain words. The customer applies a suggestion
  (which creates a rule for that site) or dismisses it. With
  auto-prioritise on, catalogue matches are applied without asking;
  behaviour guesses always wait for a person. No packet contents are read.
- Limits match the agent's: 200 matches and 100 websites per site map.

## Consequences

- Customers can express "Teams first, backups last, ERP on Carrier B" in the
  portal without an engineer.
- Flow telemetry adds up to 100 rows per site per minute; it is kept for a
  day.
- The catalogue is deliberately small. A wrong label is worse than
  "unrecognised", which the behaviour profile then handles.
- IPv6 rules, wildcard websites and deep packet inspection are out of scope.
