// Package desired defines the desired-state document the controller sends to
// each agent (schema 1). The agent applies it idempotently and reports the
// version it applied. See docs/desired-state.md.
package desired

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/netip"
	"os"
	"regexp"
	"strconv"
	"strings"
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
	// Loopback is this node's /32 on lo, advertised in BGP (ADR 0009).
	Loopback string `json:"loopback,omitempty"`
	// Circuits are the PoP's IPsec circuits to cloud gateways.
	Circuits []Circuit `json:"circuits,omitempty"`
	// L2Circuits are a site's VLANs bridged over VXLAN to another site.
	L2Circuits []L2Circuit `json:"l2_circuits,omitempty"`
	// Internet is where the LAN's internet traffic goes (ADR 0010); absent
	// means the agent removes everything it set up for it.
	Internet *Internet `json:"internet,omitempty"`
	// IPFIX, when set, exports the site's flow aggregates to the customer's
	// collector (ADR 0026); absent means no export.
	IPFIX *IPFIX `json:"ipfix,omitempty"`
}

// IPFIX is where a site sends its flow records (RFC 7011 over UDP).
type IPFIX struct {
	Collector         string `json:"collector"` // host:port
	ObservationDomain uint32 `json:"observation_domain"`
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

// Circuit is one route-based IPsec circuit from the PoP to a cloud VPN
// gateway: an XFRM interface, a strongSwan connection and a BGP neighbour.
// PSK is secret: it goes into a 0600 file and nowhere else. String and
// LogValue leave it out, so a circuit printed or logged by mistake is safe.
type Circuit struct {
	ID                int      `json:"id"`
	Name              string   `json:"name"` // xfrm interface, vc<id>
	IfID              uint32   `json:"if_id"`
	UnderlayInterface string   `json:"underlay_interface"`
	LocalAddress      string   `json:"local_address"`
	RemoteAddress     string   `json:"remote_address"`
	PSK               string   `json:"psk"`
	IKEProposals      string   `json:"ike_proposals"`
	ESPProposals      string   `json:"esp_proposals"`
	InsideAddress     string   `json:"inside_address"` // ours, a prefix on the xfrm interface
	PeerInside        string   `json:"peer_inside"`    // the gateway's BGP address
	PeerASN           int      `json:"peer_asn"`
	ImportPrefixes    []string `json:"import_prefixes"` // empty = accept any
	MaxPrefixes       int      `json:"max_prefixes"`
	ExportPrefixes    []string `json:"export_prefixes"`
	ExportCircuits    []int    `json:"export_circuits"`
	ShapeKbit         int      `json:"shape_kbit"`
	// Enabled false means the same as not listed: torn down. Absent means enabled.
	Enabled *bool `json:"enabled,omitempty"`
}

func (c Circuit) String() string { return fmt.Sprintf("circuit %s (id %d)", c.Name, c.ID) }

func (c Circuit) LogValue() slog.Value {
	return slog.GroupValue(slog.Int("id", c.ID), slog.String("name", c.Name), slog.String("remote", c.RemoteAddress))
}

// Conn is the strongSwan connection (and child) name.
func (c Circuit) Conn() string { return "exa-" + c.Name }

// L2Circuit carries one VLAN on the site's LAN interface over VXLAN to the
// other end's loopback, through a bridge.
type L2Circuit struct {
	ID        int    `json:"id"`
	Name      string `json:"name"` // the VXLAN interface, vx<id>
	VNI       int    `json:"vni"`
	VLAN      int    `json:"vlan"`
	Parent    string `json:"parent"`
	Remote    string `json:"remote"` // the other end's loopback address
	ShapeKbit int    `json:"shape_kbit"`
	MTU       int    `json:"mtu"`
	Probe     *Probe `json:"probe,omitempty"`
}

// Bridge is the bridge joining the VXLAN and VLAN interfaces.
func (c L2Circuit) Bridge() string { return "br" + strconv.Itoa(c.ID) }

// VLANDev is the VLAN subinterface on the parent.
func (c L2Circuit) VLANDev() string { return c.Parent + "." + strconv.Itoa(c.VLAN) }

// ActiveCircuits is the cloud circuits to bring up: those not disabled.
func (s *State) ActiveCircuits() []Circuit {
	var out []Circuit
	for _, c := range s.Circuits {
		if c.Enabled == nil || *c.Enabled {
			out = append(out, c)
		}
	}
	return out
}

// LoopbackAddr is the loopback's address without the prefix length, or "".
func (s *State) LoopbackAddr() string {
	if p, err := netip.ParsePrefix(s.Loopback); err == nil {
		return p.Addr().String()
	}
	return ""
}

var ifName = regexp.MustCompile(`^wg-[a-z0-9]{1,10}$`)
var devName = regexp.MustCompile(`^[a-zA-Z0-9._-]{1,15}$`)
var profName = regexp.MustCompile(`^[a-z0-9-]{1,32}$`)
var vcName = regexp.MustCompile(`^vc[0-9]{1,8}$`)
var vxName = regexp.MustCompile(`^vx[0-9]{1,8}$`)
var proposals = regexp.MustCompile(`^[a-z0-9_-]+(,[a-z0-9_-]+)*$`)

// Validate rejects anything the renderers would otherwise write into
// configuration files unchecked.
func (s *State) Validate() error {
	if s.Schema != Schema {
		return fmt.Errorf("unsupported schema %d", s.Schema)
	}
	if s.IPFIX != nil {
		host, port, err := net.SplitHostPort(s.IPFIX.Collector)
		if err != nil || host == "" || port == "0" || strings.ContainsAny(host, " /@") {
			return fmt.Errorf("bad ipfix collector %q", s.IPFIX.Collector)
		}
		if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
			return fmt.Errorf("bad ipfix collector port %q", port)
		}
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
	if s.Reflector != nil {
		if err := validListen(s.Reflector.Listen); err != nil {
			return fmt.Errorf("bad reflector listen %q: %w", s.Reflector.Listen, err)
		}
	}
	if s.Loopback != "" {
		p, err := netip.ParsePrefix(s.Loopback)
		if err != nil || !p.Addr().Is4() || p.Bits() != 32 {
			return fmt.Errorf("bad loopback %q: want an IPv4 /32", s.Loopback)
		}
	}
	if err := s.validateCircuits(); err != nil {
		return err
	}
	if err := s.validateL2(); err != nil {
		return err
	}
	return s.validateInternet()
}

func validListen(l string) error {
	host, port, err := net.SplitHostPort(l)
	if err != nil {
		return err
	}
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
		return fmt.Errorf("bad port")
	}
	if host != "" {
		if _, err := netip.ParseAddr(host); err != nil {
			return fmt.Errorf("bad address")
		}
	}
	return nil
}

func ipv4(a string) bool {
	ip, err := netip.ParseAddr(a)
	return err == nil && ip.Is4()
}

func prefixes(list []string) error {
	for _, p := range list {
		if pp, err := netip.ParsePrefix(p); err != nil || !pp.Addr().Is4() {
			return fmt.Errorf("bad prefix %q", p)
		}
	}
	return nil
}

// validPSK accepts printable ASCII only. The key itself is never put in an error.
func validPSK(k string) bool {
	if len(k) < 8 || len(k) > 256 {
		return false
	}
	for i := 0; i < len(k); i++ {
		if k[i] < 0x20 || k[i] > 0x7e {
			return false
		}
	}
	return true
}

// validateCircuits checks the circuits that will be brought up; disabled
// ones are ignored, like circuits not listed.
func (s *State) validateCircuits() error {
	active := s.ActiveCircuits()
	if len(active) > 0 && s.Role != RolePoP {
		return fmt.Errorf("cloud circuits are for the PoP only")
	}
	ids, names, ifids, peers := map[int]bool{}, map[string]bool{}, map[uint32]bool{}, map[string]bool{}
	for _, c := range active {
		if c.ID < 1 || ids[c.ID] {
			return fmt.Errorf("bad or duplicate circuit id %d", c.ID)
		}
		ids[c.ID] = true
		if !vcName.MatchString(c.Name) || names[c.Name] {
			return fmt.Errorf("bad or duplicate circuit name %q", c.Name)
		}
		names[c.Name] = true
		if c.IfID == 0 || ifids[c.IfID] {
			return fmt.Errorf("%s: bad or duplicate if_id %d", c.Name, c.IfID)
		}
		ifids[c.IfID] = true
		if !devName.MatchString(c.UnderlayInterface) {
			return fmt.Errorf("%s: bad underlay interface %q", c.Name, c.UnderlayInterface)
		}
		if !ipv4(c.LocalAddress) || !ipv4(c.RemoteAddress) {
			return fmt.Errorf("%s: bad local or remote address", c.Name)
		}
		if !validPSK(c.PSK) {
			return fmt.Errorf("%s: missing or unusable pre-shared key (8 to 256 printable characters)", c.Name)
		}
		if len(c.IKEProposals) > 256 || !proposals.MatchString(c.IKEProposals) ||
			len(c.ESPProposals) > 256 || !proposals.MatchString(c.ESPProposals) {
			return fmt.Errorf("%s: bad proposals", c.Name)
		}
		inside, err := netip.ParsePrefix(c.InsideAddress)
		if err != nil || !inside.Addr().Is4() || inside.Bits() > 30 {
			return fmt.Errorf("%s: bad inside address %q", c.Name, c.InsideAddress)
		}
		peer, err := netip.ParseAddr(c.PeerInside)
		if err != nil || !inside.Contains(peer) || peer == inside.Addr() || peers[c.PeerInside] {
			return fmt.Errorf("%s: bad or duplicate peer inside address %q", c.Name, c.PeerInside)
		}
		peers[c.PeerInside] = true
		if c.PeerASN < 1 || c.PeerASN > 4294967294 {
			return fmt.Errorf("%s: bad peer asn %d", c.Name, c.PeerASN)
		}
		if err := prefixes(c.ImportPrefixes); err != nil {
			return fmt.Errorf("%s: import: %w", c.Name, err)
		}
		if err := prefixes(c.ExportPrefixes); err != nil {
			return fmt.Errorf("%s: export: %w", c.Name, err)
		}
		if c.MaxPrefixes < 0 || c.ShapeKbit < 0 {
			return fmt.Errorf("%s: bad max_prefixes or shape_kbit", c.Name)
		}
	}
	return nil
}

func (s *State) validateL2() error {
	if len(s.L2Circuits) == 0 {
		return nil
	}
	if s.Role != RoleSite {
		return fmt.Errorf("layer 2 circuits are for sites only")
	}
	if s.Loopback == "" {
		return fmt.Errorf("layer 2 circuits need a loopback")
	}
	ids, names, vnis, vlans := map[int]bool{}, map[string]bool{}, map[int]bool{}, map[string]bool{}
	for _, c := range s.L2Circuits {
		if c.ID < 1 || ids[c.ID] {
			return fmt.Errorf("bad or duplicate l2 circuit id %d", c.ID)
		}
		ids[c.ID] = true
		if !vxName.MatchString(c.Name) || names[c.Name] {
			return fmt.Errorf("bad or duplicate l2 circuit name %q", c.Name)
		}
		names[c.Name] = true
		if c.VNI < 1 || c.VNI > 1<<24-1 || vnis[c.VNI] {
			return fmt.Errorf("%s: bad or duplicate vni %d", c.Name, c.VNI)
		}
		vnis[c.VNI] = true
		if c.VLAN < 1 || c.VLAN > 4094 {
			return fmt.Errorf("%s: bad vlan %d", c.Name, c.VLAN)
		}
		if !devName.MatchString(c.Parent) || strings.Contains(c.Parent, ".") || !devName.MatchString(c.VLANDev()) || vlans[c.VLANDev()] {
			return fmt.Errorf("%s: bad parent %q or duplicate vlan", c.Name, c.Parent)
		}
		vlans[c.VLANDev()] = true
		if len(c.Bridge()) > 15 {
			return fmt.Errorf("%s: id too long for a bridge name", c.Name)
		}
		if !ipv4(c.Remote) {
			return fmt.Errorf("%s: bad remote %q", c.Name, c.Remote)
		}
		if c.ShapeKbit < 0 || (c.MTU != 0 && (c.MTU < 576 || c.MTU > 9000)) {
			return fmt.Errorf("%s: bad shape_kbit or mtu", c.Name)
		}
		if c.Probe != nil {
			if _, err := netip.ParseAddrPort(c.Probe.Target); err != nil || c.Probe.IntervalMS < 0 {
				return fmt.Errorf("%s: bad probe", c.Name)
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
