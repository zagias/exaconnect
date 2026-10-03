# ADR 0009: Virtual circuits, the cloud router and elastic bandwidth

Date: 2026-10-03. Status: accepted.

## Context

Step 2 of ExaConnect Fabric (the Equinix and Megaport research Dudley
shared) asks for virtual circuits from sites to clouds and between sites, a
per-customer cloud router, VLAN mapping, bandwidth the customer can change
at any time, and monitoring per circuit. Megaport and Equinix do this with
physical ports and private interconnects (Direct Connect, ExpressRoute).
We have no physical presence yet, so this step has to work over what every
cloud already offers: its site-to-site IPsec VPN gateway.

## Decision

**Cloud circuits** end at the PoP. For each one the PoP agent makes a
route-based IPsec tunnel (an XFRM interface `vc<id>` with its own `if_id`,
IKEv2 with a pre-shared key through strongSwan's `swanctl`) and runs eBGP
with the cloud's gateway over the tunnel's inside /30, as AWS, Azure,
Google and Oracle all expect. The cloud takes the first host of the /30
and we take the second; the controller hands out a free /30 from
169.254.100.0/22 when the console does not dictate one. We accept only the
cloud prefixes the customer entered (and longer ones), at most 100, and
advertise the customer's site networks.

**The cloud router** is that same BGP speaker. Sites learn every cloud's
routes from the PoP. With `cloud_to_cloud` on (the default, a customer
setting), each circuit also exports what the others accept, so traffic
between two clouds turns at the PoP instead of going back to an office.

**Site circuits** are layer 2: a VLAN at each end (they may differ, which
is the VLAN mapping) bridged over VXLAN (VNI 10000 + id) between the two
sites' loopbacks. Every node now has a loopback, 10.254.0.<host>/32, put on
`lo` and announced in BGP, so the VXLAN runs over the existing WireGuard
tunnels through the PoP and follows normal failover. MTU 1370 inside the
circuit (WireGuard's 1420 less 50 for VXLAN). Each end probes the other's
loopback every second for round trip and loss.

**Steering.** A circuit may name a class; traffic to its cloud prefixes is
then put in that class at every site, so it gets that class's queue, SLA
routing and Storm Mode behaviour.

**Elastic bandwidth.** Each circuit has a speed, shaped on its interface.
Changing it takes effect on the agents' next poll (within 10 seconds).
Every speed is kept with its start and end, and charged by the hour at the
circuit's monthly price per Mbps (default 2.00, over 730 hours a month).
A circuit that is switched off is torn down but still billed, as with a
port that is reserved; deleting it ends billing.

**Monitoring.** The PoP reports IKE state, BGP state and accepted routes
for each cloud circuit; sites report probe results for each layer 2
circuit; both report interface byte counters. The portal shows status
(provisioning, up, down, off), round trip, loss, Mbps and month-to-date
charges.

**The pre-shared key** is write-only. It is stored for the agent, which
writes it to a 0600 file. It is never returned by the API (only `has_psk`),
never logged and never audited.

## Not done in this step

- Physical ports, cross-connects, MACsec and private cloud interconnects
  (Direct Connect, ExpressRoute, Partner Interconnect): these need presence
  in a data centre.
- IPv6 inside circuits; two tunnels per cloud connection for redundancy
  (step 5, resilient circuits); certificates instead of pre-shared keys.

## Consequences

- The PoP needs a public-facing interface and address for IKE
  (`cloud_interface`, `cloud_address` in the site form) and the
  `xfrm_interface` and `vxlan` kernel modules on the host.
- Throughput to a cloud is limited by that cloud's VPN gateway (about
  1.25 Gbps per AWS tunnel), which is fine for this stage.
- Lab check `m9-fabric.sh` runs two simulated cloud gateways (strongSwan
  and FRR in containers) behind an exchange router.
- The contract between controller, agent and portal is in
  `docs/fabric-contract.md`.
