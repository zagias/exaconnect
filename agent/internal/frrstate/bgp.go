package frrstate

import (
	"context"
	"encoding/json"
	"net/netip"
	"sort"

	"github.com/zagias/exaconnect/agent/internal/system"
)

// Neighbor is what circuit telemetry needs from one BGP session.
type Neighbor struct {
	State            string // FRR's state string: Idle, Connect, Active, OpenSent, OpenConfirm, Established
	PrefixesReceived int    // accepted from the neighbour (IPv4 unicast)
}

// BGPNeighbor reads one neighbour from `show bgp neighbors <ip> json`. An
// unknown neighbour gives an empty State.
func BGPNeighbor(ctx context.Context, sys system.Runner, addr string) (Neighbor, error) {
	out, err := sys.Run(ctx, "vtysh", "-c", "show bgp neighbors "+addr+" json")
	if err != nil {
		return Neighbor{}, err
	}
	return ParseBGPNeighbor(out, addr)
}

func ParseBGPNeighbor(b []byte, addr string) (Neighbor, error) {
	var all map[string]json.RawMessage
	if err := json.Unmarshal(b, &all); err != nil {
		return Neighbor{}, err
	}
	raw, ok := all[addr]
	if !ok {
		return Neighbor{}, nil // {"bgpNoSuchNeighbor":true}, or not configured yet
	}
	var n struct {
		State  string `json:"bgpState"`
		PfxRcd *int   `json:"pfxRcd"`
		AFI    map[string]struct {
			Accepted *int `json:"acceptedPrefixCounter"`
		} `json:"addressFamilyInfo"`
	}
	if err := json.Unmarshal(raw, &n); err != nil {
		return Neighbor{}, err
	}
	out := Neighbor{State: n.State}
	if af, ok := n.AFI["ipv4Unicast"]; ok && af.Accepted != nil {
		out.PrefixesReceived = *af.Accepted
	} else if n.PfxRcd != nil {
		out.PrefixesReceived = *n.PfxRcd
	}
	return out, nil
}

// NeighborRoutes lists up to max prefixes accepted from a neighbour, from
// `show bgp ipv4 unicast neighbors <ip> routes json`.
func NeighborRoutes(ctx context.Context, sys system.Runner, addr string, max int) ([]string, error) {
	out, err := sys.Run(ctx, "vtysh", "-c", "show bgp ipv4 unicast neighbors "+addr+" routes json")
	if err != nil {
		return nil, err
	}
	return ParseNeighborRoutes(out, max)
}

func ParseNeighborRoutes(b []byte, max int) ([]string, error) {
	var v struct {
		Routes map[string]json.RawMessage `json:"routes"`
	}
	if err := json.Unmarshal(b, &v); err != nil {
		return nil, err
	}
	ps := make([]netip.Prefix, 0, len(v.Routes))
	for k := range v.Routes {
		if p, err := netip.ParsePrefix(k); err == nil {
			ps = append(ps, p)
		}
	}
	sort.Slice(ps, func(i, j int) bool {
		if c := ps[i].Addr().Compare(ps[j].Addr()); c != 0 {
			return c < 0
		}
		return ps[i].Bits() < ps[j].Bits()
	})
	out := []string{}
	for _, p := range ps {
		if len(out) == max {
			break
		}
		out = append(out, p.String())
	}
	return out, nil
}
