package steer

import (
	"context"
	"os"
	"strings"
	"testing"
)

func siteMap() *Map {
	return &Map{
		Version: 3,
		Paths: []Path{
			{Name: "carrier-a", Tunnel: "wg-a", Table: 101},
			{Name: "carrier-b", Tunnel: "wg-b", Table: 102},
			{Name: "sat", Tunnel: "wg-sat", Table: 103},
		},
		Classes: []Class{
			{Name: "voice", Mark: 0x101, DSCP: []int{46, 34}, Ports: []PortRange{{"udp", 5060, 5060}, {"udp", 10000, 20000}}},
			{Name: "business", Mark: 0x102, DSCP: []int{26}, Ports: []PortRange{{"tcp", 443, 443}}, Subnets: []string{"10.50.0.0/16"}},
			{Name: "bulk", Mark: 0x103, DSCP: []int{8, 0}},
		},
		Rules: []Rule{
			{Class: "voice", Paths: []string{"carrier-b", "carrier-a", "sat"}},
			{Class: "business", Paths: []string{"carrier-a", "carrier-b"}},
			{Class: "bulk", Paths: []string{"carrier-a", "carrier-b"}, PauseIfNone: true},
		},
		LocalPrefixes: []string{"192.168.10.0/24"},
	}
}

func TestValidate(t *testing.T) {
	if err := siteMap().Validate(); err != nil {
		t.Fatal(err)
	}
	bad := []func(m *Map){
		func(m *Map) { m.Paths[1].Table = 101 },
		func(m *Map) { m.Classes[1].Mark = 0x101 },
		func(m *Map) { m.Rules[0].Paths = []string{"carrier-z"} },
		func(m *Map) { m.Rules[0].Class = "video" },
		func(m *Map) { m.Classes[0].Ports[0].Proto = "sctp" },
		func(m *Map) { m.Classes[0].DSCP = []int{64} },
		func(m *Map) { m.Paths[0].Tunnel = "eth0; reboot" },
		func(m *Map) { m.Rules[0].Dst = "not-a-prefix" },
	}
	for i, f := range bad {
		m := siteMap()
		f(m)
		if m.Validate() == nil {
			t.Errorf("case %d: invalid map accepted", i)
		}
	}
}

func TestChooseFailsOverInOrder(t *testing.T) {
	m := siteMap()
	up := map[string]bool{"carrier-a": true, "carrier-b": true, "sat": true}
	usable := func(p string) bool { return up[p] }

	got := Choose(m, usable)
	if got[0].Path != "carrier-b" || got[0].Failover {
		t.Fatalf("voice: %+v", got[0])
	}

	up["carrier-b"] = false // BFD down on B: voice goes to the next path in its list
	got = Choose(m, usable)
	if got[0].Path != "carrier-a" || !got[0].Failover {
		t.Fatalf("voice after B down: %+v", got[0])
	}

	up["carrier-a"] = false // both terrestrial down
	got = Choose(m, usable)
	if got[0].Path != "sat" {
		t.Fatalf("voice should reach satellite: %+v", got[0])
	}
	if got[1].Path != "" || got[1].Paused {
		t.Fatalf("business has no allowed path left and follows BGP: %+v", got[1])
	}
	if got[2].Path != "" || !got[2].Paused {
		t.Fatalf("bulk pauses: %+v", got[2])
	}
}

func TestNFT(t *testing.T) {
	nft := NFT(siteMap(), nil)
	for _, want := range []string{
		"table ip exaconnect {}\ndelete table ip exaconnect\n",
		"elements = { 192.168.10.0/24 }",
		"ip daddr @local return",
		"ip dscp { 46, 34 } meta mark set 0x101 ct mark set 0x101 return",
		"udp dport { 5060, 10000-20000 } meta mark set 0x101 ct mark set 0x101 return",
		"tcp dport { 443 } meta mark set 0x102 ct mark set 0x102 return",
		"ip daddr { 10.50.0.0/16 } meta mark set 0x102 ct mark set 0x102 return",
		"ip saddr { 10.50.0.0/16 } meta mark set 0x102 ct mark set 0x102 return",
		"ip dscp { 8, 0 } meta mark set 0x103 ct mark set 0x103 return",
	} {
		if !strings.Contains(nft, want) {
			t.Errorf("missing %q in\n%s", want, nft)
		}
	}
	// Voice rules come before business and bulk: first match wins.
	if strings.Index(nft, "0x101") > strings.Index(nft, "0x102") || strings.Index(nft, "0x102") > strings.Index(nft, "0x103") {
		t.Error("classes out of order")
	}
}

func TestIPRulesAndRoutes(t *testing.T) {
	m := siteMap()
	choices := []Choice{
		{Class: "voice", Path: "carrier-b"},
		{Class: "business"},           // follows BGP: no rule
		{Class: "bulk", Paused: true}, // blackholed
	}
	got := IPRules(m, choices)
	want := []string{"pref 1000 fwmark 0x101 lookup 102", "pref 1020 fwmark 0x103 blackhole"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("rules = %q, want %q", got, want)
	}
	r := Routes(m)
	if r[101][0] != "default dev wg-a" || r[103][0] != "default dev wg-sat" {
		t.Fatalf("site routes %v", r)
	}

	// PoP: per-destination rules and routes.
	m.Rules = []Rule{{Class: "voice", Dst: "192.168.20.0/24", Paths: []string{"carrier-b"}}, {Class: "voice", Dst: "192.168.10.0/24", Paths: []string{"carrier-a"}}}
	got = IPRules(m, Choose(m, func(string) bool { return true }))
	if got[0] != "pref 1000 fwmark 0x101 to 192.168.20.0/24 lookup 102" {
		t.Fatalf("pop rule %q", got[0])
	}
	r = Routes(m)
	if strings.Join(r[102], "|") != "192.168.10.0/24 dev wg-b|192.168.20.0/24 dev wg-b" {
		t.Fatalf("pop routes %v", r[102])
	}
}

type fakeSys struct {
	cmds  []string
	files map[string]string
	rules string                 // output of ip -j rule show
	fail  func(cmd string) error // optional: make a command fail
}

func (f *fakeSys) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	cmd := name + " " + strings.Join(args, " ")
	if strings.HasPrefix(cmd, "ip -batch ") {
		cmd += "\n" + f.files[args[1]]
	}
	f.cmds = append(f.cmds, cmd)
	if f.fail != nil {
		if err := f.fail(cmd); err != nil {
			return nil, err
		}
	}
	if cmd == "ip -j rule show" {
		return []byte(f.rules), nil
	}
	return nil, nil
}
func (f *fakeSys) WriteFile(p string, d []byte, _ os.FileMode) error {
	f.files[p] = string(d)
	return nil
}
func (f *fakeSys) Exists(p string) bool              { _, ok := f.files[p]; return ok }
func (f *fakeSys) ReadFile(p string) ([]byte, error) { return []byte(f.files[p]), nil }

func TestSteererSwapsOnlyRules(t *testing.T) {
	sys := &fakeSys{files: map[string]string{}, rules: `[{"priority":0},{"priority":1000},{"priority":1010},{"priority":32766}]`}
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	up := map[string]bool{"carrier-a": true, "carrier-b": true, "sat": true}
	ctx := context.Background()

	if err := s.Apply(ctx, m, Choose(m, func(p string) bool { return up[p] })); err != nil {
		t.Fatal(err)
	}
	all := strings.Join(sys.cmds, "\n")
	for _, want := range []string{"nft -f /st/steer.nft", "route flush table 101", "rule del pref 1000\nrule del pref 1010\nrule add pref 1000 fwmark 0x101 lookup 102"} {
		if !strings.Contains(all, want) {
			t.Fatalf("missing %q in\n%s", want, all)
		}
	}
	if strings.Contains(all, "pref 32766") {
		t.Fatal("touched a rule outside its range")
	}

	// Same state again: nothing to do.
	n := len(sys.cmds)
	if err := s.Apply(ctx, m, Choose(m, func(p string) bool { return up[p] })); err != nil {
		t.Fatal(err)
	}
	if len(sys.cmds) != n {
		t.Fatalf("re-applied unchanged state: %v", sys.cmds[n:])
	}

	// BFD down on B: one ip -batch call, no nft or route work.
	up["carrier-b"] = false
	if err := s.Apply(ctx, m, Choose(m, func(p string) bool { return up[p] })); err != nil {
		t.Fatal(err)
	}
	if len(sys.cmds) != n+1 || !strings.HasPrefix(sys.cmds[n], "ip -batch") {
		t.Fatalf("swap should be one ip -batch call, got %v", sys.cmds[n:])
	}
	if !strings.Contains(sys.cmds[n], "rule add pref 1000 fwmark 0x101 lookup 101") {
		t.Fatalf("voice should move to carrier-a: %s", sys.cmds[n])
	}
}

func TestNFTCorporatePrefixes(t *testing.T) {
	m := siteMap()
	plain := NFT(m, nil)
	m.CorporatePrefixes = []string{"192.168.10.0/24", "10.200.0.0/24", "10.254.0.0/24", "100.64.0.0/16", "100.64.1.0/24"}
	if err := m.Validate(); err != nil {
		t.Fatal(err)
	}
	nft := NFT(m, nil)
	set := "\tset corporate {\n\t\ttype ipv4_addr\n\t\tflags interval\n" +
		"\t\telements = { 10.200.0.0/24, 10.254.0.0/24, 100.64.0.0/16, 192.168.10.0/24 }\n\t}\n"
	top := "\t\ttype filter hook prerouting priority mangle; policy accept;\n" +
		"\t\tip daddr @local return\n\t\tip daddr != @corporate return\n\t\tip dscp { 46, 34 }"
	for _, want := range []string{set, top} {
		if !strings.Contains(nft, want) {
			t.Errorf("missing %q in\n%s", want, nft)
		}
	}
	if strings.Replace(strings.Replace(nft, set, "", 1), "\t\tip daddr != @corporate return\n", "", 1) != plain {
		t.Error("corporate prefixes changed more than the set and the early return")
	}
	m.CorporatePrefixes = []string{"10.0.0.0/8", "fd00::/8"}
	if m.Validate() == nil {
		t.Error("IPv6 corporate prefix accepted")
	}
	m.CorporatePrefixes = []string{"10.0.0.0/33"}
	if m.Validate() == nil {
		t.Error("bad corporate prefix accepted")
	}
}
