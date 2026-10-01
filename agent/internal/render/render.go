// Package render turns a desired state into WireGuard and FRR configuration text.
package render

import (
	"fmt"
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
	b.WriteString(" !\n address-family ipv4 unicast\n")
	for _, p := range s.LANPrefixes {
		fmt.Fprintf(&b, "  network %s\n", p)
	}
	for _, n := range nbs {
		if n.LocalPref > 0 {
			fmt.Fprintf(&b, "  neighbor %s route-map %s in\n", n.Address, rmName(n.tunnel))
		}
	}
	b.WriteString(" exit-address-family\nexit\n!\n")
	for _, n := range nbs {
		if n.LocalPref > 0 {
			fmt.Fprintf(&b, "route-map %s permit 10\n set local-preference %d\nexit\n!\n", rmName(n.tunnel), n.LocalPref)
		}
	}
	return b.String()
}

func rmName(tunnel string) string { return "LP-" + strings.ToUpper(tunnel) }
