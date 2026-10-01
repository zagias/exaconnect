// Package desired defines the desired-state document the controller sends to
// each agent (schema 1). The agent applies it idempotently and reports the
// version it applied. See docs/desired-state.md.
package desired

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"regexp"
)

const Schema = 1

type Role string

const (
	RoleSite Role = "site"
	RolePoP  Role = "pop"
)

type State struct {
	Schema      int          `json:"schema"`
	Version     int64        `json:"version"`
	NodeID      string       `json:"node_id"`
	NodeName    string       `json:"node_name"`
	CustomerID  string       `json:"customer_id"`
	Role        Role         `json:"role"`
	ASN         int          `json:"asn"`
	RouterID    string       `json:"router_id"`
	LANPrefixes []string     `json:"lan_prefixes"`
	Tunnels     []Tunnel     `json:"tunnels"`
	BFDProfiles []BFDProfile `json:"bfd_profiles"`
	Reflector   *Reflector   `json:"reflector,omitempty"`
}

// Tunnel is one WireGuard interface over one underlay (carrier) link.
type Tunnel struct {
	Name string `json:"name"` // wg-a, wg-b, wg-sat
	Path string `json:"path"` // carrier-a, carrier-b, sat
	// Underlay is the kernel interface facing this path's carrier, metered for settlement.
	Underlay   string     `json:"underlay_interface,omitempty"`
	Address    string     `json:"address"`
	ListenPort int        `json:"listen_port,omitempty"`
	MTU        int        `json:"mtu,omitempty"`
	Peers      []Peer     `json:"peers"`
	Neighbors  []Neighbor `json:"bgp_neighbors"`
	Probe      *Probe     `json:"probe,omitempty"`
}

type Peer struct {
	Name       string   `json:"name"`
	PublicKey  string   `json:"public_key"`
	Endpoint   string   `json:"endpoint,omitempty"`
	AllowedIPs []string `json:"allowed_ips"`
	Keepalive  int      `json:"keepalive,omitempty"`
}

type Neighbor struct {
	Address    string `json:"address"`
	ASN        int    `json:"asn"`
	BFDProfile string `json:"bfd_profile,omitempty"`
	// LocalPref is applied to routes learned from this neighbour (higher wins).
	LocalPref int `json:"local_pref,omitempty"`
}

type BFDProfile struct {
	Name       string `json:"name"`
	TxMS       int    `json:"tx_ms"`
	RxMS       int    `json:"rx_ms"`
	Multiplier int    `json:"multiplier"`
}

type Probe struct {
	Target     string `json:"target"` // host:port of the PoP reflector, reached through this tunnel
	IntervalMS int    `json:"interval_ms"`
}

type Reflector struct {
	Listen string `json:"listen"`
}

var ifName = regexp.MustCompile(`^wg-[a-z0-9]{1,10}$`)
var devName = regexp.MustCompile(`^[a-zA-Z0-9._-]{1,15}$`)
var profName = regexp.MustCompile(`^[a-z0-9-]{1,32}$`)

// Validate rejects anything the renderers would otherwise write into
// configuration files unchecked.
func (s *State) Validate() error {
	if s.Schema != Schema {
		return fmt.Errorf("unsupported schema %d", s.Schema)
	}
	if s.Role != RoleSite && s.Role != RolePoP {
		return fmt.Errorf("bad role %q", s.Role)
	}
	if s.ASN < 1 || s.ASN > 4294967294 {
		return fmt.Errorf("bad asn %d", s.ASN)
	}
	if _, err := netip.ParseAddr(s.RouterID); err != nil {
		return fmt.Errorf("bad router_id: %w", err)
	}
	for _, p := range s.LANPrefixes {
		if _, err := netip.ParsePrefix(p); err != nil {
			return fmt.Errorf("bad lan prefix %q", p)
		}
	}
	profiles := map[string]bool{}
	for _, p := range s.BFDProfiles {
		if !profName.MatchString(p.Name) || p.TxMS < 10 || p.RxMS < 10 || p.Multiplier < 1 {
			return fmt.Errorf("bad bfd profile %+v", p)
		}
		profiles[p.Name] = true
	}
	seen := map[string]bool{}
	for _, t := range s.Tunnels {
		if !ifName.MatchString(t.Name) || seen[t.Name] {
			return fmt.Errorf("bad or duplicate tunnel name %q", t.Name)
		}
		seen[t.Name] = true
		if t.Underlay != "" && !devName.MatchString(t.Underlay) {
			return fmt.Errorf("%s: bad underlay interface %q", t.Name, t.Underlay)
		}
		if _, err := netip.ParsePrefix(t.Address); err != nil {
			return fmt.Errorf("%s: bad address %q", t.Name, t.Address)
		}
		if t.ListenPort < 0 || t.ListenPort > 65535 {
			return fmt.Errorf("%s: bad listen port", t.Name)
		}
		for _, p := range t.Peers {
			if len(p.PublicKey) != 44 {
				return fmt.Errorf("%s: peer %s: bad public key", t.Name, p.Name)
			}
			if p.Endpoint != "" {
				if _, err := netip.ParseAddrPort(p.Endpoint); err != nil {
					return fmt.Errorf("%s: peer %s: bad endpoint", t.Name, p.Name)
				}
			}
			for _, a := range p.AllowedIPs {
				if _, err := netip.ParsePrefix(a); err != nil {
					return fmt.Errorf("%s: peer %s: bad allowed ip %q", t.Name, p.Name, a)
				}
			}
		}
		for _, n := range t.Neighbors {
			if _, err := netip.ParseAddr(n.Address); err != nil {
				return fmt.Errorf("%s: bad neighbour %q", t.Name, n.Address)
			}
			if n.BFDProfile != "" && !profiles[n.BFDProfile] {
				return fmt.Errorf("%s: unknown bfd profile %q", t.Name, n.BFDProfile)
			}
		}
		if t.Probe != nil {
			if _, err := netip.ParseAddrPort(t.Probe.Target); err != nil {
				return fmt.Errorf("%s: bad probe target", t.Name)
			}
		}
	}
	return nil
}

func Load(path string) (*State, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var s State
	if err := json.Unmarshal(b, &s); err != nil {
		return nil, err
	}
	return &s, s.Validate()
}
