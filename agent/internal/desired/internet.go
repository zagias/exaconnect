package desired

import (
	"fmt"
	"net/netip"
	"regexp"
	"strconv"
	"strings"
)

// Internet modes (docs/internet-contract.md). A site uses pop, local or off;
// the PoP is always the gateway.
const (
	InternetPoP     = "pop"
	InternetLocal   = "local"
	InternetOff     = "off"
	InternetGateway = "gateway"
)

// Limits on one internet block, so a state cannot make the ruleset unbounded.
const (
	MaxFirewallRules = 500
	MaxPortForwards  = 500
)

// Internet is where a node's LAN traffic to the internet goes, and the NAT
// and firewall applied where it leaves.
type Internet struct {
	Mode        string   `json:"mode"`
	LANPrefixes []string `json:"lan_prefixes"`
	// Tunnels are the tunnels to the PoP in preference order (site, pop mode).
	Tunnels []string `json:"tunnels,omitempty"`
	// Uplinks are the carrier links in preference order (site, local mode),
	// or the PoP's one internet interface.
	Uplinks       []Uplink       `json:"uplinks,omitempty"`
	PublicAddress string         `json:"public_address,omitempty"`
	Firewall      []FirewallRule `json:"firewall,omitempty"`
	PortForwards  []PortForward  `json:"port_forwards,omitempty"`
}

type Uplink struct {
	Interface string `json:"interface"`
	Gateway   string `json:"gateway"`
	Path      string `json:"path"`
	// Tunnel is the tunnel over this carrier; its BFD state says whether the carrier works.
	Tunnel string `json:"tunnel"`
}

// FirewallRule is one of the customer's rules; the first that matches wins.
// Empty Src or Dst means any.
type FirewallRule struct {
	ID       int      `json:"id"`
	Action   string   `json:"action"` // allow | deny
	Src      []string `json:"src"`
	Dst      []string `json:"dst"`
	Protocol string   `json:"protocol"` // any | tcp | udp | icmp
	Ports    string   `json:"ports"`    // "443", "8000-8100,8443"; tcp or udp only
}

// PortForward sends Protocol/Port on the PoP's public address to a host on a
// pop-mode site. Empty AllowFrom means any source.
type PortForward struct {
	ID        int      `json:"id"`
	Protocol  string   `json:"protocol"` // tcp | udp
	Port      int      `json:"port"`
	ToAddress string   `json:"to_address"`
	ToPort    int      `json:"to_port"`
	AllowFrom []string `json:"allow_from"`
}

// Firewalled reports whether the node does NAT and filtering for internet
// traffic itself: the PoP, and a site breaking out locally.
func (s *State) Firewalled() bool {
	return s.Internet != nil && (s.Role == RolePoP || s.Internet.Mode == InternetLocal)
}

// PortRange is one inclusive range of a Ports spec.
type PortRange struct{ From, To int }

// ParsePorts reads "443", "8000-8100,8443" (spaces allowed around commas).
// An empty spec is no ranges.
func ParsePorts(spec string) ([]PortRange, error) {
	var out []PortRange
	if strings.TrimSpace(spec) == "" {
		return nil, nil
	}
	for _, part := range strings.Split(spec, ",") {
		part = strings.TrimSpace(part)
		lo, hi, isRange := strings.Cut(part, "-")
		if !isRange {
			hi = lo
		}
		from, err1 := port(lo)
		to, err2 := port(hi)
		if err1 != nil || err2 != nil || from > to {
			return nil, fmt.Errorf("bad port or range %q", part)
		}
		out = append(out, PortRange{from, to})
	}
	return out, nil
}

func port(s string) (int, error) {
	if s == "" || len(s) > 5 || strings.TrimLeft(s, "0123456789") != "" {
		return 0, fmt.Errorf("not a port")
	}
	n, err := strconv.Atoi(s)
	if err != nil || n < 1 || n > 65535 {
		return 0, fmt.Errorf("not a port")
	}
	return n, nil
}

var pathLabel = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,31}$`)

// addrsOrPrefixes accepts IPv4 prefixes and bare IPv4 addresses.
func addrsOrPrefixes(list []string) error {
	for _, v := range list {
		if ipv4(v) {
			continue
		}
		if p, err := netip.ParsePrefix(v); err != nil || !p.Addr().Is4() {
			return fmt.Errorf("bad address or prefix %q", v)
		}
	}
	return nil
}

func (s *State) validateInternet() error {
	in := s.Internet
	if in == nil {
		return nil
	}
	switch {
	case s.Role == RolePoP && in.Mode != InternetGateway:
		return fmt.Errorf("internet: the PoP's mode must be %q, not %q", InternetGateway, in.Mode)
	case s.Role == RoleSite && in.Mode != InternetPoP && in.Mode != InternetLocal && in.Mode != InternetOff:
		return fmt.Errorf("internet: bad mode %q for a site", in.Mode)
	}
	if err := prefixes(in.LANPrefixes); err != nil {
		return fmt.Errorf("internet: lan: %w", err)
	}
	tunnels := map[string]bool{}
	for _, t := range s.Tunnels {
		tunnels[t.Name] = true
	}
	seen := map[string]bool{}
	for _, t := range in.Tunnels {
		if !ifName.MatchString(t) || seen[t] || !tunnels[t] {
			return fmt.Errorf("internet: bad, duplicate or unknown tunnel %q", t)
		}
		seen[t] = true
	}
	seen = map[string]bool{}
	for _, u := range in.Uplinks {
		if !devName.MatchString(u.Interface) || seen[u.Interface] {
			return fmt.Errorf("internet: bad or duplicate uplink interface %q", u.Interface)
		}
		seen[u.Interface] = true
		if !ipv4(u.Gateway) {
			return fmt.Errorf("internet: uplink %s: bad gateway %q", u.Interface, u.Gateway)
		}
		if u.Path != "" && !pathLabel.MatchString(u.Path) {
			return fmt.Errorf("internet: uplink %s: bad path %q", u.Interface, u.Path)
		}
		if u.Tunnel != "" && !ifName.MatchString(u.Tunnel) {
			return fmt.Errorf("internet: uplink %s: bad tunnel %q", u.Interface, u.Tunnel)
		}
	}
	if s.Role == RolePoP && len(in.Uplinks) != 1 {
		return fmt.Errorf("internet: the PoP needs exactly one uplink, not %d", len(in.Uplinks))
	}
	if len(in.Firewall) > 0 && !s.Firewalled() {
		return fmt.Errorf("internet: firewall rules are for the PoP and local-mode sites only")
	}
	if len(in.Firewall) > MaxFirewallRules {
		return fmt.Errorf("internet: too many firewall rules (%d, at most %d)", len(in.Firewall), MaxFirewallRules)
	}
	ids := map[int]bool{}
	for _, r := range in.Firewall {
		if r.ID < 1 || ids[r.ID] {
			return fmt.Errorf("internet: bad or duplicate firewall rule id %d", r.ID)
		}
		ids[r.ID] = true
		if r.Action != "allow" && r.Action != "deny" {
			return fmt.Errorf("internet: rule %d: bad action %q", r.ID, r.Action)
		}
		if err := addrsOrPrefixes(r.Src); err != nil {
			return fmt.Errorf("internet: rule %d: src: %w", r.ID, err)
		}
		if err := addrsOrPrefixes(r.Dst); err != nil {
			return fmt.Errorf("internet: rule %d: dst: %w", r.ID, err)
		}
		switch r.Protocol {
		case "any", "icmp":
			if r.Ports != "" {
				return fmt.Errorf("internet: rule %d: ports need tcp or udp", r.ID)
			}
		case "tcp", "udp":
			if _, err := ParsePorts(r.Ports); err != nil {
				return fmt.Errorf("internet: rule %d: %w", r.ID, err)
			}
		default:
			return fmt.Errorf("internet: rule %d: bad protocol %q", r.ID, r.Protocol)
		}
	}
	if len(in.PortForwards) > 0 && s.Role != RolePoP {
		return fmt.Errorf("internet: port forwards are for the PoP only")
	}
	if len(in.PortForwards) > MaxPortForwards {
		return fmt.Errorf("internet: too many port forwards (%d, at most %d)", len(in.PortForwards), MaxPortForwards)
	}
	if in.PublicAddress != "" && !ipv4(in.PublicAddress) {
		return fmt.Errorf("internet: bad public address %q", in.PublicAddress)
	}
	if len(in.PortForwards) > 0 && in.PublicAddress == "" {
		return fmt.Errorf("internet: port forwards need a public address")
	}
	ids, ports := map[int]bool{}, map[string]bool{}
	for _, f := range in.PortForwards {
		if f.ID < 1 || ids[f.ID] {
			return fmt.Errorf("internet: bad or duplicate port forward id %d", f.ID)
		}
		ids[f.ID] = true
		key := f.Protocol + "/" + strconv.Itoa(f.Port)
		if (f.Protocol != "tcp" && f.Protocol != "udp") || ports[key] {
			return fmt.Errorf("internet: forward %d: bad protocol or duplicate port %s", f.ID, key)
		}
		ports[key] = true
		if f.Port < 1 || f.Port > 65535 || f.ToPort < 1 || f.ToPort > 65535 {
			return fmt.Errorf("internet: forward %d: ports must be 1-65535", f.ID)
		}
		if !ipv4(f.ToAddress) {
			return fmt.Errorf("internet: forward %d: bad to_address %q", f.ID, f.ToAddress)
		}
		if err := addrsOrPrefixes(f.AllowFrom); err != nil {
			return fmt.Errorf("internet: forward %d: allow_from: %w", f.ID, err)
		}
	}
	return nil
}
