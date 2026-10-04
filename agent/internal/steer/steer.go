// Package steer puts each application class on a path. nftables marks packets
// by class (DSCP, then ports, then subnets); one ip rule per class sends the
// mark to the routing table of the chosen path. FRR and BGP still own
// reachability in the main table: a class with no rule simply follows BGP.
//
// The controller sends a steering map with an ordered list of allowed paths
// per class. The agent picks the first path that is up, so a BFD failure moves
// classes at once without waiting for the controller (CLAUDE.md §4.1).
//
// Custom traffic rules (matches) are checked before the class defaults, and
// every marking rule also sets the conntrack mark so flow telemetry can name
// the class. Optional QoS rewrites DSCP per class and puts CAKE on tunnels.
package steer

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/zagias/exaconnect/agent/internal/system"
)

// RulePrefBase is the first ip rule preference the agent owns; it owns
// RulePrefBase up to RulePrefBase+RulePrefSpan-1 and nothing else.
const (
	RulePrefBase = 1000
	RulePrefSpan = 1000
	NFTable      = "exaconnect"
)

type Map struct {
	Version int64   `json:"version"`
	Storm   bool    `json:"storm"`
	Paths   []Path  `json:"paths"`
	Classes []Class `json:"classes"`
	Rules   []Rule  `json:"rules"`
	// LocalPrefixes are never steered (the site's own LAN).
	LocalPrefixes []string `json:"local_prefixes"`
	// CorporatePrefixes, when set, are the only destinations classified: a
	// site breaking out to the internet locally leaves the rest unmarked.
	CorporatePrefixes []string `json:"corporate_prefixes,omitempty"`
	// Matches are custom traffic rules, checked in order before the class
	// defaults; the first that fits decides the class.
	Matches []Match `json:"matches,omitempty"`
	QoS     *QoS    `json:"qos,omitempty"`
}

// Limits on custom traffic rules, so one map cannot make the ruleset or the
// resolver work unbounded.
const (
	MaxMatches = 200
	MaxDomains = 100
)

// Match puts traffic in a class. Every non-empty field must fit (AND); within
// a field any entry may fit (OR). Dst and Domains together are the
// destination: the subnets or the addresses the domains resolve to. A match
// also fits the return direction, except one tied to VLANs (ingress only).
type Match struct {
	Class   string      `json:"class"`
	VLANs   []int       `json:"vlans,omitempty"`
	Src     []string    `json:"src,omitempty"`
	Dst     []string    `json:"dst,omitempty"`
	Domains []string    `json:"domains,omitempty"`
	Ports   []PortRange `json:"ports,omitempty"`
	DSCP    []int       `json:"dscp,omitempty"`
}

// QoS rewrites the DSCP of the listed classes and installs CAKE (diffserv4)
// on the listed tunnels. Without it the agent leaves qdiscs alone.
type QoS struct {
	Classes []QoSClass `json:"classes,omitempty"`
	Paths   []QoSPath  `json:"paths,omitempty"`
}

type QoSClass struct {
	Name string `json:"name"`
	DSCP int    `json:"dscp"`
}

// QoSPath shapes one tunnel's egress; ShapeKbit 0 means unlimited.
type QoSPath struct {
	Tunnel    string `json:"tunnel"`
	ShapeKbit int    `json:"shape_kbit"`
}

// Path ties a path name to its tunnel and the routing table that uses it.
type Path struct {
	Name   string `json:"name"`
	Tunnel string `json:"tunnel"`
	Table  int    `json:"table"`
}

type Class struct {
	Name    string      `json:"name"`
	Mark    int         `json:"mark"`
	DSCP    []int       `json:"dscp"`
	Ports   []PortRange `json:"ports"`
	Subnets []string    `json:"subnets"`
}

type PortRange struct {
	Proto string `json:"proto"` // tcp or udp
	From  int    `json:"from"`
	To    int    `json:"to"`
}

// Rule steers one class, optionally only toward one destination prefix (the
// PoP has one rule per class and site, so return traffic follows the site's
// choice). Paths is the order of preference.
type Rule struct {
	Class       string   `json:"class"`
	Dst         string   `json:"dst,omitempty"`
	Paths       []string `json:"paths"`
	PauseIfNone bool     `json:"pause_if_none,omitempty"`
}

var (
	nameRe   = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,19}$`)
	tunnelRe = regexp.MustCompile(`^wg-[a-z0-9]{1,10}$`)
	domainRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$`)
	ifaceRe  = regexp.MustCompile(`^[a-zA-Z0-9._-]{1,15}$`)
)

func validPorts(ports []PortRange) error {
	for _, p := range ports {
		if (p.Proto != "tcp" && p.Proto != "udp") || p.From < 1 || p.To > 65535 || p.From > p.To {
			return fmt.Errorf("bad port range %+v", p)
		}
	}
	return nil
}

func validDSCP(dscp []int) error {
	for _, d := range dscp {
		if d < 0 || d > 63 {
			return fmt.Errorf("dscp %d", d)
		}
	}
	return nil
}

// validV4 parses IPv4 prefixes; the classifier table is IPv4 only.
func validV4(prefixes []string) error {
	for _, s := range prefixes {
		p, err := netip.ParsePrefix(s)
		if err != nil || !p.Addr().Is4() {
			return fmt.Errorf("subnet %q", s)
		}
	}
	return nil
}

func (m *Map) Validate() error {
	paths := map[string]bool{}
	tables := map[int]bool{}
	for _, p := range m.Paths {
		if !nameRe.MatchString(p.Name) || !tunnelRe.MatchString(p.Tunnel) {
			return fmt.Errorf("bad path %q / tunnel %q", p.Name, p.Tunnel)
		}
		if p.Table < 100 || p.Table > 250 || tables[p.Table] {
			return fmt.Errorf("path %s: table %d must be unique and 100-250", p.Name, p.Table)
		}
		paths[p.Name], tables[p.Table] = true, true
	}
	classes := map[string]bool{}
	marks := map[int]bool{}
	for _, c := range m.Classes {
		if !nameRe.MatchString(c.Name) || classes[c.Name] {
			return fmt.Errorf("bad or duplicate class %q", c.Name)
		}
		if c.Mark < 1 || c.Mark > 0xffff || marks[c.Mark] {
			return fmt.Errorf("class %s: mark %d must be unique and 1-65535", c.Name, c.Mark)
		}
		if err := validDSCP(c.DSCP); err != nil {
			return fmt.Errorf("class %s: %w", c.Name, err)
		}
		if err := validPorts(c.Ports); err != nil {
			return fmt.Errorf("class %s: %w", c.Name, err)
		}
		for _, s := range c.Subnets {
			if _, err := netip.ParsePrefix(s); err != nil {
				return fmt.Errorf("class %s: subnet %q", c.Name, s)
			}
		}
		classes[c.Name], marks[c.Mark] = true, true
	}
	if len(m.Rules) > RulePrefSpan/10 {
		return fmt.Errorf("too many rules (%d)", len(m.Rules))
	}
	for _, r := range m.Rules {
		if !classes[r.Class] {
			return fmt.Errorf("rule for unknown class %q", r.Class)
		}
		if r.Dst != "" {
			if _, err := netip.ParsePrefix(r.Dst); err != nil {
				return fmt.Errorf("rule %s: dst %q", r.Class, r.Dst)
			}
		}
		for _, p := range r.Paths {
			if !paths[p] {
				return fmt.Errorf("rule %s: unknown path %q", r.Class, p)
			}
		}
	}
	for _, s := range m.LocalPrefixes {
		if _, err := netip.ParsePrefix(s); err != nil {
			return fmt.Errorf("local prefix %q", s)
		}
	}
	if err := validV4(m.CorporatePrefixes); err != nil {
		return fmt.Errorf("corporate prefix: %w", err)
	}
	if err := m.validateMatches(classes); err != nil {
		return err
	}
	return m.validateQoS(classes)
}

func (m *Map) validateMatches(classes map[string]bool) error {
	if len(m.Matches) > MaxMatches {
		return fmt.Errorf("too many matches (%d, at most %d)", len(m.Matches), MaxMatches)
	}
	domains := 0
	for i, mt := range m.Matches {
		if !classes[mt.Class] {
			return fmt.Errorf("match %d: unknown class %q", i, mt.Class)
		}
		for _, v := range mt.VLANs {
			if v < 1 || v > 4094 {
				return fmt.Errorf("match %d: vlan %d", i, v)
			}
		}
		if err := validV4(mt.Src); err != nil {
			return fmt.Errorf("match %d: src %w", i, err)
		}
		if err := validV4(mt.Dst); err != nil {
			return fmt.Errorf("match %d: dst %w", i, err)
		}
		for _, d := range mt.Domains {
			if len(d) > 253 || !domainRe.MatchString(d) {
				return fmt.Errorf("match %d: domain %q", i, d)
			}
		}
		domains += len(mt.Domains)
		if err := validPorts(mt.Ports); err != nil {
			return fmt.Errorf("match %d: %w", i, err)
		}
		if err := validDSCP(mt.DSCP); err != nil {
			return fmt.Errorf("match %d: %w", i, err)
		}
	}
	if domains > MaxDomains {
		return fmt.Errorf("too many domains (%d, at most %d)", domains, MaxDomains)
	}
	return nil
}

func (m *Map) validateQoS(classes map[string]bool) error {
	if m.QoS == nil {
		return nil
	}
	seen := map[string]bool{}
	for _, c := range m.QoS.Classes {
		if !classes[c.Name] || seen[c.Name] {
			return fmt.Errorf("qos: unknown or duplicate class %q", c.Name)
		}
		if c.DSCP < 0 || c.DSCP > 63 {
			return fmt.Errorf("qos: class %s: dscp %d", c.Name, c.DSCP)
		}
		seen[c.Name] = true
	}
	seen = map[string]bool{}
	for _, p := range m.QoS.Paths {
		if !tunnelRe.MatchString(p.Tunnel) || seen[p.Tunnel] {
			return fmt.Errorf("qos: bad or duplicate tunnel %q", p.Tunnel)
		}
		if p.ShapeKbit < 0 {
			return fmt.Errorf("qos: %s: shape %d kbit", p.Tunnel, p.ShapeKbit)
		}
		seen[p.Tunnel] = true
	}
	return nil
}

func Load(path string) (*Map, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m Map
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return &m, m.Validate()
}

// Choice is where one rule's class goes right now.
type Choice struct {
	Class  string `json:"class"`
	Dst    string `json:"dst,omitempty"`
	Path   string `json:"path,omitempty"` // empty: follows BGP (or paused)
	Paused bool   `json:"paused,omitempty"`
	// Failover is true when the controller's first choice is down and the
	// agent picked another path on its own.
	Failover bool `json:"failover,omitempty"`
}

// Choose picks the first usable path of each rule.
func Choose(m *Map, usable func(path string) bool) []Choice {
	out := make([]Choice, 0, len(m.Rules))
	for _, r := range m.Rules {
		c := Choice{Class: r.Class, Dst: r.Dst}
		for i, p := range r.Paths {
			if usable(p) {
				c.Path, c.Failover = p, i > 0
				break
			}
		}
		if c.Path == "" {
			c.Paused = r.PauseIfNone
			c.Failover = len(r.Paths) > 0
		}
		out = append(out, c)
	}
	return out
}

// Lookups is what the classifier needs from outside the map: the IPv4
// addresses each domain resolves to, and the VLAN subinterfaces (eth0.20) on
// this host by VLAN id.
type Lookups struct {
	Domains map[string][]netip.Addr
	VLANs   map[int][]string
}

// NFT renders the nftables ruleset that marks packets by class. Replacing the
// table in one `nft -f` run is atomic. l may be nil.
func NFT(m *Map, l *Lookups) string {
	if l == nil {
		l = &Lookups{}
	}
	var b strings.Builder
	// Create-then-delete makes the delete safe when the table does not exist yet.
	fmt.Fprintf(&b, "table ip %s {}\ndelete table ip %s\n", NFTable, NFTable)
	fmt.Fprintf(&b, "table ip %s {\n", NFTable)
	if len(m.LocalPrefixes) > 0 {
		fmt.Fprintf(&b, "\tset local {\n\t\ttype ipv4_addr\n\t\tflags interval\n\t\telements = { %s }\n\t}\n",
			strings.Join(m.LocalPrefixes, ", "))
	}
	if len(m.CorporatePrefixes) > 0 {
		fmt.Fprintf(&b, "\tset corporate {\n\t\ttype ipv4_addr\n\t\tflags interval\n\t\telements = { %s }\n\t}\n",
			strings.Join(prefixList(parseAll(m.CorporatePrefixes)), ", "))
	}
	// One set per match with domains: its dst subnets plus what the domains
	// resolve to. A set with nothing in it matches nothing.
	for i, mt := range m.Matches {
		if len(mt.Domains) == 0 {
			continue
		}
		fmt.Fprintf(&b, "\tset %s {\n\t\ttype ipv4_addr\n\t\tflags interval\n", matchSet(i))
		if el := matchDst(mt, l); len(el) > 0 {
			fmt.Fprintf(&b, "\t\telements = { %s }\n", strings.Join(el, ", "))
		}
		b.WriteString("\t}\n")
	}
	b.WriteString("\tchain classify {\n\t\ttype filter hook prerouting priority mangle; policy accept;\n")
	if len(m.LocalPrefixes) > 0 {
		b.WriteString("\t\tip daddr @local return\n")
	}
	if len(m.CorporatePrefixes) > 0 {
		b.WriteString("\t\tip daddr != @corporate return\n")
	}
	acts := actions(m)
	// Custom traffic rules first, in order.
	for i, mt := range m.Matches {
		for _, r := range matchRules(i, mt, l, acts[mt.Class]) {
			fmt.Fprintf(&b, "\t\t%s\n", r)
		}
	}
	// Then the class defaults, in class order: DSCP, then ports, then subnets.
	for _, c := range m.Classes {
		set := acts[c.Name]
		if len(c.DSCP) > 0 {
			fmt.Fprintf(&b, "\t\tip dscp { %s } %s\n", joinInts(c.DSCP), set)
		}
		for _, proto := range []string{"tcp", "udp"} {
			if ports := portList(c.Ports, proto); ports != "" {
				fmt.Fprintf(&b, "\t\t%s dport { %s } %s\n", proto, ports, set)
			}
		}
		if len(c.Subnets) > 0 {
			s := strings.Join(c.Subnets, ", ")
			fmt.Fprintf(&b, "\t\tip daddr { %s } %s\n\t\tip saddr { %s } %s\n", s, set, s, set)
		}
	}
	b.WriteString("\t}\n}\n")
	return b.String()
}

// actions is what every rule marking a class does: set the packet mark, set
// the conntrack mark (so flows can be attributed to the class), rewrite DSCP
// when QoS lists the class, and stop.
func actions(m *Map) map[string]string {
	dscp := map[string]int{}
	if m.QoS != nil {
		for _, c := range m.QoS.Classes {
			dscp[c.Name] = c.DSCP
		}
	}
	out := map[string]string{}
	for _, c := range m.Classes {
		a := fmt.Sprintf("meta mark set 0x%x ct mark set 0x%x", c.Mark, c.Mark)
		if d, ok := dscp[c.Name]; ok {
			a += fmt.Sprintf(" ip dscp set %d", d)
		}
		out[c.Name] = a + " return"
	}
	return out
}

func matchSet(i int) string { return fmt.Sprintf("match_%d", i) }

// matchDst is the destination set of a match with domains: its subnets and
// the addresses its domains resolve to, without overlaps (an interval set
// rejects them).
func matchDst(mt Match, l *Lookups) []string {
	var ps []netip.Prefix
	for _, s := range mt.Dst {
		if p, err := netip.ParsePrefix(s); err == nil {
			ps = append(ps, p)
		}
	}
	for _, d := range mt.Domains {
		for _, a := range l.Domains[d] {
			ps = append(ps, netip.PrefixFrom(a, 32))
		}
	}
	return prefixList(ps)
}

// prefixList masks, sorts and de-overlaps prefixes for an nft set, keeping
// the widest of any that overlap.
func prefixList(ps []netip.Prefix) []string {
	for i := range ps {
		ps[i] = ps[i].Masked()
	}
	sort.Slice(ps, func(i, j int) bool {
		if ps[i].Bits() != ps[j].Bits() {
			return ps[i].Bits() < ps[j].Bits()
		}
		return ps[i].Addr().Less(ps[j].Addr())
	})
	var keep []netip.Prefix
next:
	for _, p := range ps {
		for _, k := range keep {
			if k.Contains(p.Addr()) {
				continue next
			}
		}
		keep = append(keep, p)
	}
	sort.Slice(keep, func(i, j int) bool { return keep[i].Addr().Less(keep[j].Addr()) })
	out := make([]string, len(keep))
	for i, p := range keep {
		if p.Bits() == 32 {
			out[i] = p.Addr().String()
		} else {
			out[i] = p.String()
		}
	}
	return out
}

func parseAll(subnets []string) []netip.Prefix {
	var ps []netip.Prefix
	for _, s := range subnets {
		if p, err := netip.ParsePrefix(s); err == nil {
			ps = append(ps, p)
		}
	}
	return ps
}

func subnetSet(subnets []string) string {
	return "{ " + strings.Join(prefixList(parseAll(subnets)), ", ") + " }"
}

// matchRules renders one match: one rule per VLAN and per protocol with
// ports, then (without VLANs) the same for the return direction.
func matchRules(i int, mt Match, l *Lookups, act string) []string {
	var src, dst string
	if len(mt.Src) > 0 {
		src = subnetSet(mt.Src)
	}
	switch {
	case len(mt.Domains) > 0:
		dst = "@" + matchSet(i)
	case len(mt.Dst) > 0:
		dst = subnetSet(mt.Dst)
	}
	var dscp string
	if len(mt.DSCP) > 0 {
		dscp = fmt.Sprintf("ip dscp { %s }", joinInts(mt.DSCP))
	}
	// One rule per protocol with ports: "tcp dport { .. }" or "udp dport { .. }".
	type l4 struct{ proto, ports string }
	var protos []l4
	for _, proto := range []string{"tcp", "udp"} {
		if ports := portList(mt.Ports, proto); ports != "" {
			protos = append(protos, l4{proto, "{ " + ports + " }"})
		}
	}
	if len(protos) == 0 {
		protos = []l4{{}}
	}
	// nft only matches interface names by prefix ("eth*"), so "*.20" is
	// expanded to the VLAN 20 subinterfaces this host has. A VLAN with none
	// gets no rule: nothing can arrive on it.
	var iifs []string
	for _, v := range mt.VLANs {
		names := l.VLANs[v]
		if len(names) == 0 {
			continue
		}
		q := make([]string, len(names))
		for j, n := range names {
			q[j] = strconv.Quote(n)
		}
		iifs = append(iifs, fmt.Sprintf("iifname { %s }", strings.Join(q, ", ")))
	}
	if len(mt.VLANs) == 0 {
		iifs = []string{""}
	}
	rule := func(parts ...string) string {
		var keep []string
		for _, p := range parts {
			if p != "" {
				keep = append(keep, p)
			}
		}
		return strings.Join(append(keep, act), " ")
	}
	with := func(prefix, v string) string {
		if v == "" {
			return ""
		}
		return prefix + v
	}
	var out []string
	for _, iif := range iifs {
		for _, p := range protos {
			out = append(out, rule(iif, with("ip saddr ", src), with("ip daddr ", dst), with(p.proto+" dport ", p.ports), dscp))
		}
	}
	if len(mt.VLANs) > 0 {
		return out
	}
	for j, p := range protos {
		r := rule(with("ip saddr ", dst), with("ip daddr ", src), with(p.proto+" sport ", p.ports), dscp)
		if r != out[j] { // a match with nothing to swap fits both ways already
			out = append(out, r)
		}
	}
	return out
}

// portList renders the port ranges of one protocol for an nft set, or "".
func portList(ports []PortRange, proto string) string {
	var out []string
	for _, p := range ports {
		if p.Proto != proto {
			continue
		}
		if p.From == p.To {
			out = append(out, fmt.Sprint(p.From))
		} else {
			out = append(out, fmt.Sprintf("%d-%d", p.From, p.To))
		}
	}
	return strings.Join(out, ", ")
}

func joinInts(v []int) string {
	s := make([]string, len(v))
	for i, x := range v {
		s[i] = fmt.Sprint(x)
	}
	return strings.Join(s, ", ")
}

// IPRules renders one `ip -batch` line per rule (without the leading "rule
// add"); the index in the slice decides the preference.
func IPRules(m *Map, choices []Choice) []string {
	marks := map[string]int{}
	for _, c := range m.Classes {
		marks[c.Name] = c.Mark
	}
	tables := map[string]int{}
	for _, p := range m.Paths {
		tables[p.Name] = p.Table
	}
	out := make([]string, 0, len(choices))
	for i, c := range choices {
		line := fmt.Sprintf("pref %d fwmark 0x%x", RulePrefBase+10*i, marks[c.Class])
		if c.Dst != "" {
			line += " to " + c.Dst
		}
		switch {
		case c.Path != "":
			line += fmt.Sprintf(" lookup %d", tables[c.Path])
		case c.Paused:
			line += " blackhole"
		default:
			continue // no rule: the class follows BGP in the main table
		}
		out = append(out, line)
	}
	return out
}

// Routes renders each path table: a default route on a site, or one route per
// destination prefix on the PoP.
func Routes(m *Map) map[int][]string {
	dsts := map[string]bool{}
	for _, r := range m.Rules {
		if r.Dst != "" {
			dsts[r.Dst] = true
		}
	}
	var list []string
	for d := range dsts {
		list = append(list, d)
	}
	sort.Strings(list)
	if len(list) == 0 {
		list = []string{"default"}
	}
	out := map[int][]string{}
	for _, p := range m.Paths {
		for _, d := range list {
			out[p.Table] = append(out[p.Table], fmt.Sprintf("%s dev %s", d, p.Tunnel))
		}
	}
	return out
}

// Steerer applies maps and choices to the host, skipping work that is
// already in place so a BFD-driven swap is a single `ip -batch` call.
type Steerer struct {
	Sys      system.Runner
	StateDir string
	// Resolve looks up the IPv4 addresses of a domain; nil uses the Go
	// resolver. Interfaces lists the host's interface names; nil uses net.
	Resolve    func(ctx context.Context, host string) ([]netip.Addr, error)
	Interfaces func() ([]string, error)

	nftKey   string
	routeKey string
	rules    []string
	primed   bool

	qosKeys   map[string]string // tunnel -> tc arguments last run
	qosFailed map[string]bool   // tunnels whose last tc run failed
	qosErr    string

	mu      sync.Mutex // guards lookups: Refresh runs off the steering goroutine
	lookups Lookups
}

// ResolveTimeout bounds one domain lookup.
const ResolveTimeout = 5 * time.Second

// Refresh looks up the domains in m (only those never resolved, unless all)
// and re-reads the host's VLAN subinterfaces. It reports whether the result
// changed, so the classifier needs re-applying. A failed lookup keeps the
// last good addresses; one that never succeeded leaves an empty set. It may
// run alongside Apply.
func (s *Steerer) Refresh(ctx context.Context, m *Map, all bool) (changed bool, err error) {
	want := map[string]bool{}
	for _, mt := range m.Matches {
		for _, d := range mt.Domains {
			want[d] = true
		}
	}
	s.mu.Lock()
	var todo []string
	for d := range want {
		if _, ok := s.lookups.Domains[d]; all || !ok {
			todo = append(todo, d)
		}
	}
	s.mu.Unlock()
	sort.Strings(todo)

	got, errs := s.resolveAll(ctx, todo)
	vlans, ierr := s.vlanIfaces()
	if ierr != nil {
		errs = append(errs, ierr)
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	domains := map[string][]netip.Addr{}
	for d := range want {
		if a, ok := got[d]; ok {
			domains[d] = a
		} else if a, ok := s.lookups.Domains[d]; ok {
			domains[d] = a
		}
	}
	if ierr != nil {
		vlans = s.lookups.VLANs
	}
	changed = fmt.Sprint(domains) != fmt.Sprint(s.lookups.Domains) || fmt.Sprint(vlans) != fmt.Sprint(s.lookups.VLANs)
	s.lookups = Lookups{Domains: domains, VLANs: vlans}
	return changed, errors.Join(errs...)
}

// resolveAll looks up domains a few at a time; failed ones are left out.
func (s *Steerer) resolveAll(ctx context.Context, domains []string) (map[string][]netip.Addr, []error) {
	resolve := s.Resolve
	if resolve == nil {
		resolve = func(ctx context.Context, host string) ([]netip.Addr, error) {
			return net.DefaultResolver.LookupNetIP(ctx, "ip4", host)
		}
	}
	type result struct {
		domain string
		addrs  []netip.Addr
		err    error
	}
	jobs := make(chan string)
	results := make(chan result)
	var wg sync.WaitGroup
	for w := 0; w < min(8, len(domains)); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for d := range jobs {
				lctx, cancel := context.WithTimeout(ctx, ResolveTimeout)
				a, err := resolve(lctx, d)
				cancel()
				results <- result{d, a, err}
			}
		}()
	}
	go func() {
		for _, d := range domains {
			jobs <- d
		}
		close(jobs)
		wg.Wait()
		close(results)
	}()
	got := map[string][]netip.Addr{}
	var errs []error
	for r := range results {
		if r.err != nil {
			errs = append(errs, fmt.Errorf("resolve %s: %w", r.domain, r.err))
			continue
		}
		got[r.domain] = ipv4Only(r.addrs)
	}
	sort.Slice(errs, func(i, j int) bool { return errs[i].Error() < errs[j].Error() })
	return got, errs
}

func ipv4Only(addrs []netip.Addr) []netip.Addr {
	seen := map[netip.Addr]bool{}
	out := []netip.Addr{}
	for _, a := range addrs {
		a = a.Unmap()
		if a.Is4() && !seen[a] {
			seen[a] = true
			out = append(out, a)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Less(out[j]) })
	return out
}

// vlanIfaces groups the host's routed VLAN subinterfaces (name.<vid>) by VLAN id.
func (s *Steerer) vlanIfaces() (map[int][]string, error) {
	list := s.Interfaces
	if list == nil {
		list = func() ([]string, error) {
			ifs, err := net.Interfaces()
			names := make([]string, len(ifs))
			for i, f := range ifs {
				names[i] = f.Name
			}
			return names, err
		}
	}
	names, err := list()
	if err != nil {
		return nil, err
	}
	sort.Strings(names)
	out := map[int][]string{}
	for _, n := range names {
		dot := strings.LastIndexByte(n, '.')
		if dot < 1 || !ifaceRe.MatchString(n) {
			continue
		}
		if vid, err := strconv.Atoi(n[dot+1:]); err == nil && vid >= 1 && vid <= 4094 {
			out[vid] = append(out[vid], n)
		}
	}
	return out, nil
}

func (s *Steerer) currentLookups() *Lookups {
	s.mu.Lock()
	defer s.mu.Unlock()
	l := s.lookups
	return &l
}

// Apply installs the classifier and path tables for m (when they changed),
// the QoS qdiscs, and the ip rules for choices. A QoS failure does not fail
// steering; QoSError reports it.
func (s *Steerer) Apply(ctx context.Context, m *Map, choices []Choice) error {
	if nft := NFT(m, s.currentLookups()); nft != s.nftKey {
		f := filepath.Join(s.StateDir, "steer.nft")
		if err := s.Sys.WriteFile(f, []byte(nft), 0o600); err != nil {
			return err
		}
		if _, err := s.Sys.Run(ctx, "nft", "-f", f); err != nil {
			return err
		}
		s.nftKey = nft
	}
	routes := Routes(m)
	if key := fmt.Sprint(routes); key != s.routeKey {
		if err := s.applyRoutes(ctx, routes); err != nil {
			return err
		}
		s.routeKey = key
	}
	s.applyQoS(ctx, m)
	want := IPRules(m, choices)
	if s.primed && equal(want, s.rules) {
		return nil
	}
	var batch strings.Builder
	if s.primed {
		for _, r := range s.rules {
			fmt.Fprintf(&batch, "rule del %s\n", prefOf(r))
		}
	} else {
		// First run: clear whatever an earlier agent left in our range.
		prefs, err := s.ownedPrefs(ctx)
		if err != nil {
			return err
		}
		for _, p := range prefs {
			fmt.Fprintf(&batch, "rule del pref %d\n", p)
		}
	}
	for _, r := range want {
		fmt.Fprintf(&batch, "rule add %s\n", r)
	}
	if batch.Len() > 0 {
		f := filepath.Join(s.StateDir, "steer.rules")
		if err := s.Sys.WriteFile(f, []byte(batch.String()), 0o600); err != nil {
			return err
		}
		if _, err := s.Sys.Run(ctx, "ip", "-batch", f); err != nil {
			s.primed = false // re-read the kernel next time
			return err
		}
	}
	s.rules, s.primed = want, true
	return nil
}

// QoSArgs is the tc command that puts CAKE (diffserv4) on a tunnel's egress.
func QoSArgs(p QoSPath) []string {
	args := []string{"qdisc", "replace", "dev", p.Tunnel, "root", "cake"}
	if p.ShapeKbit > 0 {
		args = append(args, "bandwidth", fmt.Sprintf("%dkbit", p.ShapeKbit))
	} else {
		args = append(args, "unlimited")
	}
	return append(args, "diffserv4")
}

// applyQoS runs tc only for tunnels whose wanted qdisc changed. A failed run
// is not retried until the setting changes or RetryQoS is called, so a host
// without sch_cake does not pay for tc on every BFD swap.
func (s *Steerer) applyQoS(ctx context.Context, m *Map) {
	if m.QoS == nil {
		return // leave qdiscs alone
	}
	if s.qosKeys == nil {
		s.qosKeys, s.qosFailed = map[string]string{}, map[string]bool{}
	}
	listed := map[string]bool{}
	var errs []string
	ran := false
	for _, p := range m.QoS.Paths {
		listed[p.Tunnel] = true
		args := QoSArgs(p)
		key := strings.Join(args, " ")
		if s.qosKeys[p.Tunnel] == key {
			continue
		}
		ran = true
		s.qosKeys[p.Tunnel] = key
		if _, err := s.Sys.Run(ctx, "tc", args...); err != nil {
			s.qosFailed[p.Tunnel] = true
			errs = append(errs, err.Error())
		} else {
			delete(s.qosFailed, p.Tunnel)
		}
	}
	for t := range s.qosKeys {
		if !listed[t] { // no longer managed: forget it, so listing it again re-applies
			delete(s.qosKeys, t)
			delete(s.qosFailed, t)
		}
	}
	if ran {
		s.qosErr = strings.Join(errs, "; ")
	}
}

// QoSError is the error of the last tc run, or "" when it succeeded.
func (s *Steerer) QoSError() string { return s.qosErr }

// RetryQoS forgets failed tc runs so the next Apply tries them again, and
// reports whether there were any.
func (s *Steerer) RetryQoS() bool {
	for t := range s.qosFailed {
		delete(s.qosKeys, t)
	}
	n := len(s.qosFailed)
	s.qosFailed = map[string]bool{}
	return n > 0
}

func (s *Steerer) applyRoutes(ctx context.Context, routes map[int][]string) error {
	tables := make([]int, 0, len(routes))
	for t := range routes {
		tables = append(tables, t)
	}
	sort.Ints(tables)
	var batch strings.Builder
	for _, t := range tables {
		// Flush then add in one batch: these tables only carry marked traffic.
		fmt.Fprintf(&batch, "route flush table %d\n", t)
		for _, r := range routes[t] {
			fmt.Fprintf(&batch, "route replace %s table %d\n", r, t)
		}
	}
	f := filepath.Join(s.StateDir, "steer.routes")
	if err := s.Sys.WriteFile(f, []byte(batch.String()), 0o600); err != nil {
		return err
	}
	_, err := s.Sys.Run(ctx, "ip", "-batch", f)
	return err
}

func (s *Steerer) ownedPrefs(ctx context.Context) ([]int, error) {
	out, err := s.Sys.Run(ctx, "ip", "-j", "rule", "show")
	if err != nil {
		return nil, err
	}
	var rules []struct {
		Priority int `json:"priority"`
	}
	if len(strings.TrimSpace(string(out))) > 0 {
		if err := json.Unmarshal(out, &rules); err != nil {
			return nil, fmt.Errorf("parse ip rules: %w", err)
		}
	}
	seen := map[int]bool{}
	var prefs []int
	for _, r := range rules {
		if r.Priority >= RulePrefBase && r.Priority < RulePrefBase+RulePrefSpan && !seen[r.Priority] {
			seen[r.Priority] = true
			prefs = append(prefs, r.Priority)
		}
	}
	sort.Ints(prefs)
	return prefs, nil
}

// Clear removes everything the steerer installed, so all traffic follows BGP.
func (s *Steerer) Clear(ctx context.Context) error {
	s.primed, s.nftKey, s.routeKey = false, "", ""
	s.qosKeys, s.qosFailed = nil, nil
	if _, err := s.Sys.Run(ctx, "nft", "delete", "table", "ip", NFTable); err != nil && !strings.Contains(err.Error(), "No such file") {
		return err
	}
	prefs, err := s.ownedPrefs(ctx)
	if err != nil {
		return err
	}
	for _, p := range prefs {
		if _, err := s.Sys.Run(ctx, "ip", "rule", "del", "pref", fmt.Sprint(p)); err != nil {
			return err
		}
	}
	return nil
}

func prefOf(rule string) string {
	f := strings.Fields(rule)
	return f[0] + " " + f[1]
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
