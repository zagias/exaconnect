package agent

import (
	"net/netip"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

// chooseSteering keeps a PoP's health decision in the same destination scope
// as its rule. A shared WireGuard interface can have one failed site peer and
// another healthy peer; the failed site must not pause the healthy site's LAN.
// The caller holds a.mu, as for usable and the local SLA callbacks.
func (a *Agent) chooseSteering(m *steer.Map) []steer.Choice {
	var healthy func(string, string) bool
	var severity func(string, string) float64
	if a.silent {
		healthy, severity = a.slaHealthy, a.slaSeverity(m)
	}
	choices := make([]steer.Choice, 0, len(m.Rules))
	for i, rule := range m.Rules {
		scoped := *m
		scoped.Rules = m.Rules[i : i+1]
		choices = append(choices, steer.ChooseHealthy(&scoped, a.usableForRule(m, rule), healthy, severity)...)
	}
	return choices
}

func (a *Agent) usableForRule(m *steer.Map, rule steer.Rule) func(string) bool {
	fallback := a.usable(m)
	if a.current == nil || a.current.Role != desired.RolePoP || rule.Dst == "" {
		return fallback
	}
	dst, err := netip.ParsePrefix(rule.Dst)
	if err != nil {
		return func(string) bool { return false }
	}
	tunnels := make(map[string]desired.Tunnel, len(a.current.Tunnels))
	for _, tunnel := range a.current.Tunnels {
		tunnels[tunnel.Name] = tunnel
	}
	return func(path string) bool {
		for _, p := range m.Paths {
			if p.Name != path {
				continue
			}
			if status, known := destinationBFD(tunnels[p.Tunnel], dst, a.bfd); known {
				return status == "up" || status == "init"
			}
			break
		}
		// A scoped PoP route needs evidence from its own peer. An unrelated
		// peer's up state cannot fill an ownership or session gap, including
		// while desired state and a new steering map are being reconciled.
		return false
	}
}

// destinationBFD identifies the WireGuard peer owning the whole rule prefix,
// then checks only that peer's configured BGP/BFD neighbours. Longest-prefix
// ownership follows WireGuard's allowed-IP routing. A split destination or
// missing/ambiguous neighbour ownership is not evidence of a healthy route.
func destinationBFD(tunnel desired.Tunnel, dst netip.Prefix, peers map[string]bfdPeer) (string, bool) {
	owner, known := prefixOwner(tunnel, dst)
	if !known {
		return "", false
	}
	scoped := map[string]bfdPeer{}
	for _, neighbour := range tunnel.Neighbors {
		addr, err := netip.ParseAddr(neighbour.Address)
		if err != nil {
			continue
		}
		neighbourOwner, known := prefixOwner(tunnel, netip.PrefixFrom(addr, addr.BitLen()))
		if !known && peerContains(tunnel.Peers[owner], addr) {
			return "", false
		}
		if !known || neighbourOwner != owner {
			continue
		}
		state, ok := peers[neighbour.Address]
		if !ok || state.Tunnel != tunnel.Name || state.Status == "" {
			return "", false
		}
		scoped[neighbour.Address] = state
	}
	if len(scoped) == 0 {
		return "", false
	}
	return tunnelBFD(scoped, tunnel.Name), true
}

func prefixOwner(tunnel desired.Tunnel, dst netip.Prefix) (int, bool) {
	owner, best, ambiguous := -1, -1, false
	for i, peer := range tunnel.Peers {
		bits := -1
		for _, allowed := range peer.AllowedIPs {
			prefix, err := netip.ParsePrefix(allowed)
			if err == nil && prefix.Bits() <= dst.Bits() && prefix.Contains(dst.Masked().Addr()) && prefix.Bits() > bits {
				bits = prefix.Bits()
			}
		}
		if bits > best {
			owner, best, ambiguous = i, bits, false
		} else if bits >= 0 && bits == best {
			ambiguous = true
		}
	}
	if owner < 0 || ambiguous {
		return -1, false
	}
	// A peer covering the whole rule is not its sole owner if another peer
	// owns a more-specific range inside it. Do not treat only the base address
	// as proof of health for every destination selected by that rule.
	for i, peer := range tunnel.Peers {
		if i == owner {
			continue
		}
		for _, allowed := range peer.AllowedIPs {
			prefix, err := netip.ParsePrefix(allowed)
			if err == nil && prefix.Bits() >= best && prefix.Overlaps(dst) {
				return -1, false
			}
		}
	}
	return owner, true
}

func peerContains(peer desired.Peer, addr netip.Addr) bool {
	for _, allowed := range peer.AllowedIPs {
		prefix, err := netip.ParsePrefix(allowed)
		if err == nil && prefix.Contains(addr) {
			return true
		}
	}
	return false
}
