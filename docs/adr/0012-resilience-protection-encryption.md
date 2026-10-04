# ADR 0012: Resilient circuits, DDoS protection and encryption reporting

Date: 2026-10-04. Status: accepted.

## Context

Step 5 of ExaConnect Fabric. Megaport and Equinix sell diverse circuit
pairs, peering at their exchanges and DDoS protection. Large Caribbean
organisations also ask, often in audits, whether their traffic is encrypted
end to end.

## Decision

**Resilient pairs on one PoP, now.** AWS and Azure give every VPN connection
two gateway addresses. A cloud circuit may name the second one; the PoP then
runs a second IPsec tunnel with its own BGP session, and BGP moves traffic
when a tunnel fails (within the 30-second BGP hold time that AWS and Azure use, or sooner on dead-peer detection). The agent
needed no change: the second tunnel is one more circuit in the PoP's desired
state, numbered 1,000,000 + the circuit's id. Billing is unchanged: the
pair is one circuit at one bandwidth.

**Pairs through two PoPs, later.** Diversity across PoPs protects against
losing the PoP itself, but it means a second PoP host (a cost) and sites
with tunnels to both, which changes the hub-and-spoke design of the MVP.
It waits for Dudley's decision on a second PoP.

**DDoS protection on the PoP's public address.** Before NAT, for new
inbound traffic only: a per-source limit on new connections (default 50 a
second) that blocks the source for a while (default 10 minutes), a limit on
new TCP connections from everyone together (default 2,000 a second), and a
block list kept by ExaCarib's admins, with optional expiry. The limits and
block list are ExaCarib's, not each customer's, because every customer on
a PoP shares its public address. Customers see what was dropped, not the
addresses. Replacing the nftables table (on any firewall or block list
change) clears the automatic blocks; that is acceptable for a basic
protection and keeps the table atomic.

**Remotely triggered blackholing (RTBH) is a seam, not built.** Asking an
upstream carrier to drop traffic to an attacked address needs BGP with a
carrier that honours the BLACKHOLE community (RFC 7999). No carrier is
connected that way yet; volumetric attacks bigger than the PoP's link need
the carrier's help in any case.

**PoP route-server peering is deferred.** It is only useful with partners
at the PoP who want to peer, and there are none yet. The partner
directory (ADR 0011) is where they will appear.

**An encryption report** per customer: every site path (WireGuard,
ChaCha20-Poly1305), every circuit tunnel with the algorithms actually
negotiated (read from strongSwan), layer 2 circuits (VXLAN inside
WireGuard), the control channel (mutual TLS), and a plain statement that
internet traffic is only encrypted as far as the PoP. Weak algorithms
(SHA-1, MD5, DES, small Diffie-Hellman groups) are flagged; ExaConnect's
own proposals use none of them, but a cloud console can.

## Consequences

- A PoP's own failure still takes its circuits down until there is a
  second PoP.
- The protection limits are global for now (every PoP gets the same).
- Lab check `m9-protection.sh` covers a pair failing over, the encryption
  report, an automatic block and the block list. The contract is
  `docs/protection-contract.md`.
