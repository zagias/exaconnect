// Package render turns a desired state into WireGuard and FRR configuration text.
package render

import (
	"fmt"
	"net/netip"
	"sort"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// WireGuard renders a `wg setconf`/`wg syncconf` file (not wg-quick: addresses
// and routes are applied separately; FRR owns routes).
func WireGuard(t desired.Tunnel, privateKey string) string {
	var b strings.Builder
	b.WriteString("[Interface]\n")
	fmt.Fprintf(&b, "PrivateKey = %s\n", privateKey)
	if t.ListenPort > 0 {
		fmt.Fprintf(&b, "ListenPort = %d\n", t.ListenPort)
	}
	for _, p := range t.Peers {
		fmt.Fprintf(&b, "\n# %s\n[Peer]\n", p.Name)
		fmt.Fprintf(&b, "PublicKey = %s\n", p.PublicKey)
		if p.Endpoint != "" {
			fmt.Fprintf(&b, "Endpoint = %s\n", p.Endpoint)
		}
		fmt.Fprintf(&b, "AllowedIPs = %s\n", strings.Join(p.AllowedIPs, ", "))
		if p.Keepalive > 0 {
			fmt.Fprintf(&b, "PersistentKeepalive = %d\n", p.Keepalive)
		}
	}
	return b.String()
}

// FRR renders /etc/frr/frr.conf: eBGP over every tunnel with BFD, announcing
// the node's LAN prefixes. Local preference per neighbour lets the controller
// rank paths for reachability; per-class steering is layered on top (M4).
func FRR(s *desired.State) string {
	var b strings.Builder
	fmt.Fprintf(&b, "! Rendered by exa-agent from desired state version %d. Do not edit.\n", s.Version)
	b.WriteString("frr defaults traditional\n")
	fmt.Fprintf(&b, "hostname %s\n", s.NodeName)
	b.WriteString("log syslog informational\nservice integrated-vtysh-config\n!\n")

	if len(s.BFDProfiles) > 0 {
		b.WriteString("bfd\n")
		for _, p := range s.BFDProfiles {
			fmt.Fprintf(&b, " profile %s\n  detect-multiplier %d\n  receive-interval %d\n  transmit-interval %d\n exit\n !\n",
				p.Name, p.Multiplier, p.RxMS, p.TxMS)
		}
		b.WriteString("exit\n!\n")
	}

	type nb struct {
		desired.Neighbor
		tunnel string
	}
	var nbs []nb
	for _, t := range s.Tunnels {
		for _, n := range t.Neighbors {
			nbs = append(nbs, nb{n, t.Name})
		}
	}
	sort.Slice(nbs, func(i, j int) bool { return nbs[i].Address < nbs[j].Address })

	fmt.Fprintf(&b, "router bgp %d\n", s.ASN)
	fmt.Fprintf(&b, " bgp router-id %s\n", s.RouterID)
	b.WriteString(" no bgp ebgp-requires-policy\n no bgp network import-check\n bgp bestpath as-path multipath-relax\n")
	for _, n := range nbs {
		fmt.Fprintf(&b, " neighbor %s remote-as %d\n", n.Address, n.ASN)
		fmt.Fprintf(&b, " neighbor %s description %s\n", n.Address, n.tunnel)
		fmt.Fprintf(&b, " neighbor %s update-source %s\n", n.Address, n.tunnel)
		fmt.Fprintf(&b, " neighbor %s timers 3 9\n", n.Address)
		if n.BFDProfile != "" {
			fmt.Fprintf(&b, " neighbor %s bfd profile %s\n", n.Address, n.BFDProfile)
		}
	}
	// Cloud gateways: plain eBGP over the circuit's XFRM interface, no BFD.
	circuits := s.ActiveCircuits()
	for _, c := range circuits {
		fmt.Fprintf(&b, " neighbor %s remote-as %d\n", c.PeerInside, c.PeerASN)
		fmt.Fprintf(&b, " neighbor %s description %s\n", c.PeerInside, c.Name)
	}
	b.WriteString(" !\n address-family ipv4 unicast\n")
	if s.Loopback != "" {
		fmt.Fprintf(&b, "  network %s\n", s.Loopback)
	}
	for _, p := range s.LANPrefixes {
		fmt.Fprintf(&b, "  network %s\n", p)
	}
	for _, n := range nbs {
		if n.LocalPref > 0 {
			fmt.Fprintf(&b, "  neighbor %s route-map %s in\n", n.Address, rmName(n.tunnel))
		}
	}
	for _, c := range circuits {
		fmt.Fprintf(&b, "  neighbor %s prefix-list %s in\n", c.PeerInside, plName(c.Name, "IN"))
		fmt.Fprintf(&b, "  neighbor %s prefix-list %s out\n", c.PeerInside, plName(c.Name, "OUT"))
		if c.MaxPrefixes > 0 {
			fmt.Fprintf(&b, "  neighbor %s maximum-prefix %d\n", c.PeerInside, c.MaxPrefixes)
		}
	}
	b.WriteString(" exit-address-family\nexit\n!\n")
	for _, n := range nbs {
		if n.LocalPref > 0 {
			fmt.Fprintf(&b, "route-map %s permit 10\n set local-preference %d\nexit\n!\n", rmName(n.tunnel), n.LocalPref)
		}
	}
	circuitPrefixLists(&b, circuits)
	return b.String()
}

// circuitPrefixLists writes each cloud circuit's filters. In: the cloud's
// prefixes and anything longer inside them, or anything when none are set.
// Out: our exported prefixes exactly, plus what we accept from the circuits
// named in export_circuits (the cloud router), or nothing at all.
func circuitPrefixLists(b *strings.Builder, circuits []desired.Circuit) {
	byID := map[int]desired.Circuit{}
	for _, c := range circuits {
		byID[c.ID] = c
	}
	for _, c := range circuits {
		in := plName(c.Name, "IN")
		if len(c.ImportPrefixes) == 0 {
			fmt.Fprintf(b, "ip prefix-list %s seq 5 permit any\n", in)
		}
		for i, p := range dedupe(nil, c.ImportPrefixes, true) {
			fmt.Fprintf(b, "ip prefix-list %s seq %d permit %s\n", in, 5*(i+1), p)
		}
		lines := dedupe(nil, c.ExportPrefixes, false)
		for _, id := range c.ExportCircuits {
			// Only circuits that are up here; one that is off has nothing to give.
			if o, ok := byID[id]; ok && id != c.ID {
				lines = dedupe(lines, o.ImportPrefixes, true)
			}
		}
		out := plName(c.Name, "OUT")
		if len(lines) == 0 {
			fmt.Fprintf(b, "ip prefix-list %s seq 5 deny any\n", out)
		}
		for i, p := range lines {
			fmt.Fprintf(b, "ip prefix-list %s seq %d permit %s\n", out, 5*(i+1), p)
		}
		b.WriteString("!\n")
	}
}

// dedupe appends prefix-list entries for list to have, skipping repeats.
// Prefixes are masked to their network; orLonger adds "le 32" where it means anything.
func dedupe(have, list []string, orLonger bool) []string {
	seen := map[string]bool{}
	for _, h := range have {
		seen[h] = true
	}
	for _, s := range list {
		p, err := netip.ParsePrefix(s)
		if err != nil {
			continue // Validate has rejected these already
		}
		e := p.Masked().String()
		if orLonger && p.Bits() < 32 {
			e += " le 32"
		}
		if !seen[e] {
			seen[e] = true
			have = append(have, e)
		}
	}
	return have
}

func plName(circuit, dir string) string { return strings.ToUpper(circuit) + "-" + dir }

func rmName(tunnel string) string { return "LP-" + strings.ToUpper(tunnel) }
