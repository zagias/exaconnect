// Package steer puts each application class on a path. nftables marks packets
// by class (DSCP, then ports, then subnets); one ip rule per class sends the
// mark to the routing table of the chosen path. FRR and BGP still own
// reachability in the main table: a class with no rule simply follows BGP.
//
// The controller sends a steering map with an ordered list of allowed paths
// per class. The agent picks the first path that is up, so a BFD failure moves
// classes at once without waiting for the controller (CLAUDE.md §4.1).
package steer

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

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
)

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
		for _, d := range c.DSCP {
			if d < 0 || d > 63 {
				return fmt.Errorf("class %s: dscp %d", c.Name, d)
			}
		}
		for _, p := range c.Ports {
			if (p.Proto != "tcp" && p.Proto != "udp") || p.From < 1 || p.To > 65535 || p.From > p.To {
				return fmt.Errorf("class %s: bad port range %+v", c.Name, p)
			}
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

// NFT renders the nftables ruleset that marks packets by class. Replacing the
// table in one `nft -f` run is atomic.
func NFT(m *Map) string {
	var b strings.Builder
	// Create-then-delete makes the delete safe when the table does not exist yet.
	fmt.Fprintf(&b, "table ip %s {}\ndelete table ip %s\n", NFTable, NFTable)
	fmt.Fprintf(&b, "table ip %s {\n", NFTable)
	if len(m.LocalPrefixes) > 0 {
		fmt.Fprintf(&b, "\tset local {\n\t\ttype ipv4_addr\n\t\tflags interval\n\t\telements = { %s }\n\t}\n",
			strings.Join(m.LocalPrefixes, ", "))
	}
	b.WriteString("\tchain classify {\n\t\ttype filter hook prerouting priority mangle; policy accept;\n")
	if len(m.LocalPrefixes) > 0 {
		b.WriteString("\t\tip daddr @local return\n")
	}
	// First match wins, in class order: DSCP, then ports, then subnets.
	for _, c := range m.Classes {
		set := fmt.Sprintf("meta mark set 0x%x return", c.Mark)
		if len(c.DSCP) > 0 {
			fmt.Fprintf(&b, "\t\tip dscp { %s } %s\n", joinInts(c.DSCP), set)
		}
		for _, proto := range []string{"tcp", "udp"} {
			var ports []string
			for _, p := range c.Ports {
				if p.Proto != proto {
					continue
				}
				if p.From == p.To {
					ports = append(ports, fmt.Sprint(p.From))
				} else {
					ports = append(ports, fmt.Sprintf("%d-%d", p.From, p.To))
				}
			}
			if len(ports) > 0 {
				fmt.Fprintf(&b, "\t\t%s dport { %s } %s\n", proto, strings.Join(ports, ", "), set)
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

	nftKey   string
	routeKey string
	rules    []string
	primed   bool
}

// Apply installs the classifier and path tables for m (when they changed)
// and the ip rules for choices.
func (s *Steerer) Apply(ctx context.Context, m *Map, choices []Choice) error {
	if nft := NFT(m); nft != s.nftKey {
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
