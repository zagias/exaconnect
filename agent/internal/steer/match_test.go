package steer

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"sync"
	"testing"
)

// matchMap has classes without defaults, so the classifier chain holds only
// the local return and the match rules.
func matchMap(matches ...Match) *Map {
	return &Map{
		Version:       7,
		Paths:         []Path{{Name: "carrier-a", Tunnel: "wg-a", Table: 101}, {Name: "carrier-b", Tunnel: "wg-b", Table: 102}},
		Classes:       []Class{{Name: "voice", Mark: 0x101}, {Name: "business", Mark: 0x102}},
		Rules:         []Rule{{Class: "voice", Paths: []string{"carrier-b", "carrier-a"}}},
		LocalPrefixes: []string{"192.168.10.0/24"},
		Matches:       matches,
	}
}

// chainRules returns the classify chain's rules after the local return.
func chainRules(nft string) []string {
	var out []string
	in := false
	for _, l := range strings.Split(nft, "\n") {
		l = strings.TrimSpace(l)
		switch {
		case l == "ip daddr @local return":
			in = true
		case l == "}":
			in = false
		case in:
			out = append(out, l)
		}
	}
	return out
}

func addrs(s ...string) []netip.Addr {
	out := make([]netip.Addr, len(s))
	for i, a := range s {
		out[i] = netip.MustParseAddr(a)
	}
	return out
}

const voiceAct = "meta mark set 0x101 ct mark set 0x101 return"
const businessAct = "meta mark set 0x102 ct mark set 0x102 return"

func TestNFTMatches(t *testing.T) {
	cases := []struct {
		name    string
		matches []Match
		qos     *QoS
		lookups *Lookups
		rules   []string
		has     []string // more text the ruleset must contain
		hasNot  []string
	}{
		{
			name: "all fields, mirrored for the return direction",
			matches: []Match{{Class: "voice", Src: []string{"10.0.0.0/24"}, Dst: []string{"52.112.0.0/14"},
				Ports: []PortRange{{"udp", 3478, 3481}}, DSCP: []int{46}}},
			rules: []string{
				"ip saddr { 10.0.0.0/24 } ip daddr { 52.112.0.0/14 } udp dport { 3478-3481 } ip dscp { 46 } " + voiceAct,
				"ip saddr { 52.112.0.0/14 } ip daddr { 10.0.0.0/24 } udp sport { 3478-3481 } ip dscp { 46 } " + voiceAct,
			},
		},
		{
			name:    "tcp and udp ports give one rule per protocol",
			matches: []Match{{Class: "business", Dst: []string{"10.9.0.0/16"}, Ports: []PortRange{{"udp", 443, 443}, {"tcp", 443, 443}, {"udp", 3478, 3478}}}},
			rules: []string{
				"ip daddr { 10.9.0.0/16 } tcp dport { 443 } " + businessAct,
				"ip daddr { 10.9.0.0/16 } udp dport { 443, 3478 } " + businessAct,
				"ip saddr { 10.9.0.0/16 } tcp sport { 443 } " + businessAct,
				"ip saddr { 10.9.0.0/16 } udp sport { 443, 3478 } " + businessAct,
			},
		},
		{
			name:    "one rule per VLAN with subinterfaces, ingress only",
			matches: []Match{{Class: "voice", VLANs: []int{20, 30, 40}, Ports: []PortRange{{"tcp", 443, 443}, {"udp", 5060, 5061}}}},
			lookups: &Lookups{VLANs: map[int][]string{20: {"eth0.20", "eth1.20"}, 40: {"lan.40"}}},
			rules: []string{
				`iifname { "eth0.20", "eth1.20" } tcp dport { 443 } ` + voiceAct,
				`iifname { "eth0.20", "eth1.20" } udp dport { 5060-5061 } ` + voiceAct,
				`iifname { "lan.40" } tcp dport { 443 } ` + voiceAct,
				`iifname { "lan.40" } udp dport { 5060-5061 } ` + voiceAct,
			},
		},
		{
			name:    "VLAN with no subinterface on this host gives no rule",
			matches: []Match{{Class: "voice", VLANs: []int{30}}},
			rules:   nil,
		},
		{
			name:    "domains and dst share one named set",
			matches: []Match{{Class: "voice", Dst: []string{"52.112.0.0/14"}, Domains: []string{"teams.microsoft.com", "zoom.us"}}},
			lookups: &Lookups{Domains: map[string][]netip.Addr{
				"teams.microsoft.com": addrs("52.113.1.1", "13.107.64.1"), // the first is inside 52.112.0.0/14
				"zoom.us":             addrs("170.114.52.2"),
			}},
			rules: []string{"ip daddr @match_0 " + voiceAct, "ip saddr @match_0 " + voiceAct},
			has:   []string{"\tset match_0 {\n\t\ttype ipv4_addr\n\t\tflags interval\n\t\telements = { 13.107.64.1, 52.112.0.0/14, 170.114.52.2 }\n\t}\n"},
		},
		{
			name:    "a domain that does not resolve gives an empty set",
			matches: []Match{{Class: "business"}, {Class: "voice", Domains: []string{"nowhere.example"}, Ports: []PortRange{{"udp", 8801, 8810}}}},
			rules: []string{
				businessAct, // a match with no criteria fits everything
				"ip daddr @match_1 udp dport { 8801-8810 } " + voiceAct,
				"ip saddr @match_1 udp sport { 8801-8810 } " + voiceAct,
			},
			has:    []string{"\tset match_1 {\n\t\ttype ipv4_addr\n\t\tflags interval\n\t}\n"},
			hasNot: []string{"set match_0"},
		},
		{
			name:    "src only is mirrored to daddr; overlapping subnets merge",
			matches: []Match{{Class: "voice", Src: []string{"10.0.0.0/8", "10.1.2.0/24", "172.16.0.5/16"}}},
			rules: []string{
				"ip saddr { 10.0.0.0/8, 172.16.0.0/16 } " + voiceAct,
				"ip daddr { 10.0.0.0/8, 172.16.0.0/16 } " + voiceAct,
			},
		},
		{
			name:    "dscp only needs no mirror",
			matches: []Match{{Class: "voice", DSCP: []int{46, 34}}},
			rules:   []string{"ip dscp { 46, 34 } " + voiceAct},
		},
		{
			name:    "matches keep their order",
			matches: []Match{{Class: "business", DSCP: []int{26}}, {Class: "voice", DSCP: []int{46}}},
			rules:   []string{"ip dscp { 26 } " + businessAct, "ip dscp { 46 } " + voiceAct},
		},
		{
			name:    "qos rewrites dscp for the listed class in every rule",
			matches: []Match{{Class: "voice", Dst: []string{"52.112.0.0/14"}}, {Class: "business", DSCP: []int{26}}},
			qos:     &QoS{Classes: []QoSClass{{Name: "voice", DSCP: 46}}},
			rules: []string{
				"ip daddr { 52.112.0.0/14 } meta mark set 0x101 ct mark set 0x101 ip dscp set 46 return",
				"ip saddr { 52.112.0.0/14 } meta mark set 0x101 ct mark set 0x101 ip dscp set 46 return",
				"ip dscp { 26 } " + businessAct,
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := matchMap(tc.matches...)
			m.QoS = tc.qos
			if err := m.Validate(); err != nil {
				t.Fatal(err)
			}
			nft := NFT(m, tc.lookups)
			if got := chainRules(nft); strings.Join(got, "\n") != strings.Join(tc.rules, "\n") {
				t.Fatalf("rules:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(tc.rules, "\n"))
			}
			for _, h := range tc.has {
				if !strings.Contains(nft, h) {
					t.Errorf("missing %q in\n%s", h, nft)
				}
			}
			for _, h := range tc.hasNot {
				if strings.Contains(nft, h) {
					t.Errorf("unexpected %q in\n%s", h, nft)
				}
			}
		})
	}
}

func TestNFTMatchesBeforeClassDefaults(t *testing.T) {
	m := siteMap()
	m.Matches = []Match{{Class: "bulk", Dst: []string{"203.0.113.0/24"}}}
	m.QoS = &QoS{Classes: []QoSClass{{Name: "voice", DSCP: 46}}}
	rules := chainRules(NFT(m, nil))
	if rules[0] != "ip daddr { 203.0.113.0/24 } meta mark set 0x103 ct mark set 0x103 return" {
		t.Fatalf("first rule %q", rules[0])
	}
	want := []string{
		"ip dscp { 46, 34 } meta mark set 0x101 ct mark set 0x101 ip dscp set 46 return",
		"udp dport { 5060, 10000-20000 } meta mark set 0x101 ct mark set 0x101 ip dscp set 46 return",
	}
	if rules[2] != want[0] || rules[3] != want[1] {
		t.Fatalf("voice defaults should rewrite dscp: %q", rules[2:4])
	}
	for _, r := range rules {
		if !strings.Contains(r, "ct mark set") {
			t.Errorf("rule without ct mark: %q", r)
		}
	}
}

// contractJSON is the steering map as the controller sends it.
const contractJSON = `{
  "version": 9,
  "paths": [{"name": "carrier-a", "tunnel": "wg-a", "table": 101}],
  "classes": [{"name": "voice", "mark": 257, "dscp": [46]}],
  "rules": [{"class": "voice", "paths": ["carrier-a"]}],
  "local_prefixes": ["192.168.10.0/24"],
  "matches": [{"class": "voice", "vlans": [20], "src": ["10.0.0.0/24"], "dst": ["52.112.0.0/14"],
    "domains": ["teams.microsoft.com"], "ports": [{"proto": "udp", "from": 3478, "to": 3481}], "dscp": [46]}],
  "qos": {"classes": [{"name": "voice", "dscp": 46}], "paths": [{"tunnel": "wg-a", "shape_kbit": 200000}]}
}`

func TestContractJSON(t *testing.T) {
	var m Map
	if err := json.Unmarshal([]byte(contractJSON), &m); err != nil {
		t.Fatal(err)
	}
	if err := m.Validate(); err != nil {
		t.Fatal(err)
	}
	mt := m.Matches[0]
	if mt.VLANs[0] != 20 || mt.Domains[0] != "teams.microsoft.com" || mt.Ports[0].To != 3481 || m.QoS.Paths[0].ShapeKbit != 200000 || m.QoS.Classes[0].DSCP != 46 {
		t.Fatalf("decoded %+v %+v", mt, m.QoS)
	}
	// Maps without the new fields still load and render as before.
	var old Map
	if err := json.Unmarshal([]byte(`{"version":1,"classes":[{"name":"voice","mark":257}]}`), &old); err != nil || old.Validate() != nil {
		t.Fatal("old map rejected")
	}
	if old.Matches != nil || old.QoS != nil || strings.Contains(NFT(&old, nil), "set match_") {
		t.Fatal("old map grew matches or qos")
	}
}

func TestValidateMatchesAndQoS(t *testing.T) {
	many := func(n int) []Match {
		out := make([]Match, n)
		for i := range out {
			out[i] = Match{Class: "voice", DSCP: []int{46}}
		}
		return out
	}
	domains := func(n int) []string {
		out := make([]string, n)
		for i := range out {
			out[i] = fmt.Sprintf("d%d.example.com", i)
		}
		return out
	}
	ok := []func(m *Map){
		func(m *Map) { m.Matches = many(MaxMatches) },
		func(m *Map) {
			m.Matches = []Match{{Class: "voice", Domains: domains(60)}, {Class: "business", Domains: domains(40)}}
		},
		func(m *Map) {
			m.Matches = []Match{{Class: "voice", VLANs: []int{1, 4094}, Domains: []string{"a-b.c0.example"}}}
		},
		func(m *Map) {
			m.Matches = []Match{{Class: "voice", Domains: []string{strings.Repeat("abcdefghi.", 25) + "com"}}} // 253 characters
		},
		func(m *Map) {
			m.QoS = &QoS{Classes: []QoSClass{{"voice", 46}, {"business", 0}}, Paths: []QoSPath{{"wg-a", 0}, {"wg-b", 50000}}}
		},
	}
	for i, f := range ok {
		m := matchMap()
		f(m)
		if err := m.Validate(); err != nil {
			t.Errorf("ok case %d rejected: %v", i, err)
		}
	}
	bad := map[string]func(m *Map){
		"unknown class":   func(m *Map) { m.Matches = []Match{{Class: "video"}} },
		"vlan 0":          func(m *Map) { m.Matches = []Match{{Class: "voice", VLANs: []int{0}}} },
		"vlan 4095":       func(m *Map) { m.Matches = []Match{{Class: "voice", VLANs: []int{4095}}} },
		"bad src":         func(m *Map) { m.Matches = []Match{{Class: "voice", Src: []string{"10.0.0.0/33"}}} },
		"bare address":    func(m *Map) { m.Matches = []Match{{Class: "voice", Dst: []string{"10.0.0.1"}}} },
		"ipv6 dst":        func(m *Map) { m.Matches = []Match{{Class: "voice", Dst: []string{"2001:db8::/32"}}} },
		"upper-case name": func(m *Map) { m.Matches = []Match{{Class: "voice", Domains: []string{"Teams.microsoft.com"}}} },
		"wildcard":        func(m *Map) { m.Matches = []Match{{Class: "voice", Domains: []string{"*.microsoft.com"}}} },
		"injection":       func(m *Map) { m.Matches = []Match{{Class: "voice", Domains: []string{"a.com }; flush ruleset"}}} },
		"empty label":     func(m *Map) { m.Matches = []Match{{Class: "voice", Domains: []string{"a..com"}}} },
		"long domain": func(m *Map) {
			m.Matches = []Match{{Class: "voice", Domains: []string{strings.Repeat("abcdefghi.", 25) + "comx"}}}
		},
		"port proto": func(m *Map) { m.Matches = []Match{{Class: "voice", Ports: []PortRange{{"sctp", 1, 2}}}} },
		"port range": func(m *Map) { m.Matches = []Match{{Class: "voice", Ports: []PortRange{{"udp", 9, 8}}}} },
		"port zero":  func(m *Map) { m.Matches = []Match{{Class: "voice", Ports: []PortRange{{"tcp", 0, 80}}}} },
		"dscp 64":    func(m *Map) { m.Matches = []Match{{Class: "voice", DSCP: []int{64}}} },
		"too many":   func(m *Map) { m.Matches = many(MaxMatches + 1) },
		"too many names": func(m *Map) {
			m.Matches = []Match{{Class: "voice", Domains: domains(60)}, {Class: "voice", Domains: domains(41)}}
		},
		"qos class":       func(m *Map) { m.QoS = &QoS{Classes: []QoSClass{{"video", 46}}} },
		"qos dup class":   func(m *Map) { m.QoS = &QoS{Classes: []QoSClass{{"voice", 46}, {"voice", 34}}} },
		"qos dscp":        func(m *Map) { m.QoS = &QoS{Classes: []QoSClass{{"voice", 64}}} },
		"qos tunnel":      func(m *Map) { m.QoS = &QoS{Paths: []QoSPath{{"eth0; reboot", 0}}} },
		"qos dup tunnel":  func(m *Map) { m.QoS = &QoS{Paths: []QoSPath{{"wg-a", 0}, {"wg-a", 10}}} },
		"qos negative bw": func(m *Map) { m.QoS = &QoS{Paths: []QoSPath{{"wg-a", -1}}} },
	}
	for name, f := range bad {
		m := matchMap()
		f(m)
		if m.Validate() == nil {
			t.Errorf("%s: invalid map accepted", name)
		}
	}
}

func count(cmds []string, prefix string) int {
	n := 0
	for _, c := range cmds {
		if strings.HasPrefix(c, prefix) {
			n++
		}
	}
	return n
}

func TestQoSArgs(t *testing.T) {
	for _, tc := range []struct {
		p    QoSPath
		want string
	}{
		{QoSPath{"wg-a", 200000}, "qdisc replace dev wg-a root cake bandwidth 200000kbit diffserv4"},
		{QoSPath{"wg-sat", 0}, "qdisc replace dev wg-sat root cake unlimited diffserv4"},
	} {
		if got := strings.Join(QoSArgs(tc.p), " "); got != tc.want {
			t.Errorf("%+v: %q, want %q", tc.p, got, tc.want)
		}
	}
}

func TestSteererQoS(t *testing.T) {
	sys := &fakeSys{files: map[string]string{}, rules: "[]"}
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	m.QoS = &QoS{Paths: []QoSPath{{"wg-a", 200000}, {"wg-b", 0}}}
	up := map[string]bool{"carrier-a": true, "carrier-b": true, "sat": true}
	apply := func() {
		t.Helper()
		if err := s.Apply(context.Background(), m, Choose(m, func(p string) bool { return up[p] })); err != nil {
			t.Fatal(err)
		}
	}
	tcCmds := func(from int) []string {
		var out []string
		for _, c := range sys.cmds[from:] {
			if strings.HasPrefix(c, "tc ") {
				out = append(out, c)
			}
		}
		return out
	}

	apply()
	got := tcCmds(0)
	want := []string{"tc qdisc replace dev wg-a root cake bandwidth 200000kbit diffserv4", "tc qdisc replace dev wg-b root cake unlimited diffserv4"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("tc = %q, want %q", got, want)
	}

	n := len(sys.cmds)
	apply() // unchanged: no tc
	if len(tcCmds(n)) != 0 {
		t.Fatal("tc re-run without a change")
	}

	m.QoS.Paths[0].ShapeKbit = 100000 // one tunnel changes: only it is re-run
	n = len(sys.cmds)
	apply()
	if got := tcCmds(n); len(got) != 1 || got[0] != "tc qdisc replace dev wg-a root cake bandwidth 100000kbit diffserv4" {
		t.Fatalf("tc after change = %q", got)
	}

	m.QoS = nil // absent: qdiscs left alone
	n = len(sys.cmds)
	apply()
	if len(tcCmds(n)) != 0 || s.QoSError() != "" {
		t.Fatal("qos absent should not run tc")
	}
}

func TestSteererQoSFailureDoesNotStopSteering(t *testing.T) {
	sys := &fakeSys{files: map[string]string{}, rules: "[]", fail: func(cmd string) error {
		if strings.HasPrefix(cmd, "tc ") {
			return errors.New("Error: Specified qdisc kind is unknown.")
		}
		return nil
	}}
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	m.QoS = &QoS{Paths: []QoSPath{{"wg-a", 0}}}
	up := map[string]bool{"carrier-a": true, "carrier-b": true, "sat": true}
	ctx := context.Background()
	if err := s.Apply(ctx, m, Choose(m, func(p string) bool { return up[p] })); err != nil {
		t.Fatalf("qos failure failed steering: %v", err)
	}
	if !strings.Contains(strings.Join(sys.cmds, "\n"), "rule add pref 1000 fwmark 0x101 lookup 102") {
		t.Fatal("rules not applied")
	}
	if !strings.Contains(s.QoSError(), "qdisc kind is unknown") {
		t.Fatalf("QoSError = %q", s.QoSError())
	}

	// A BFD swap does not pay for tc again.
	up["carrier-b"] = false
	n := len(sys.cmds)
	if err := s.Apply(ctx, m, Choose(m, func(p string) bool { return up[p] })); err != nil {
		t.Fatal(err)
	}
	if len(sys.cmds) != n+1 || count(sys.cmds[n:], "tc ") != 0 {
		t.Fatalf("swap should be one ip -batch call, got %q", sys.cmds[n:])
	}

	// RetryQoS makes the next apply try again.
	if !s.RetryQoS() {
		t.Fatal("RetryQoS should report the failed tunnel")
	}
	n = len(sys.cmds)
	s.Apply(ctx, m, Choose(m, func(p string) bool { return up[p] }))
	if count(sys.cmds[n:], "tc qdisc replace dev wg-a") != 1 {
		t.Fatalf("tc not retried: %q", sys.cmds[n:])
	}
	if s.RetryQoS(); s.RetryQoS() {
		t.Fatal("nothing left to retry")
	}
}

// fakeDNS answers lookups from a map the test changes, and counts them.
type fakeDNS struct {
	mu      sync.Mutex
	answers map[string][]netip.Addr
	fail    map[string]bool
	calls   int
}

func (f *fakeDNS) resolve(_ context.Context, host string) ([]netip.Addr, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.fail[host] {
		return nil, errors.New("server misbehaving")
	}
	return f.answers[host], nil
}

func TestRefreshReResolvesAndChangesRender(t *testing.T) {
	dns := &fakeDNS{answers: map[string][]netip.Addr{
		"teams.microsoft.com": addrs("52.113.1.1", "2603:1063::1", "52.113.1.1"),
	}, fail: map[string]bool{}}
	ifaces := []string{"lo", "eth0"}
	sys := &fakeSys{files: map[string]string{}, rules: "[]"}
	s := &Steerer{Sys: sys, StateDir: "/st", Resolve: dns.resolve, Interfaces: func() ([]string, error) { return ifaces, nil }}
	m := matchMap(
		Match{Class: "voice", Domains: []string{"teams.microsoft.com"}},
		Match{Class: "business", VLANs: []int{20}},
		Match{Class: "business", Domains: []string{"down.example"}},
	)
	ctx := context.Background()
	nftRuns := func() int { return count(sys.cmds, "nft -f") }
	apply := func() {
		t.Helper()
		if err := s.Apply(ctx, m, Choose(m, func(string) bool { return true })); err != nil {
			t.Fatal(err)
		}
	}

	apply() // before any lookup: empty sets
	if strings.Contains(sys.files["/st/steer.nft"], "elements = { 52.") {
		t.Fatal("addresses before any lookup")
	}

	dns.fail["down.example"] = true
	changed, err := s.Refresh(ctx, m, false)
	if !changed || err == nil || !strings.Contains(err.Error(), "down.example") {
		t.Fatalf("first refresh: changed=%v err=%v", changed, err)
	}
	apply()
	nft := sys.files["/st/steer.nft"]
	if nftRuns() != 2 || !strings.Contains(nft, "set match_0 {\n\t\ttype ipv4_addr\n\t\tflags interval\n\t\telements = { 52.113.1.1 }\n") {
		t.Fatalf("resolved set not applied (%d runs):\n%s", nftRuns(), nft)
	}
	if !strings.Contains(nft, "set match_2 {\n\t\ttype ipv4_addr\n\t\tflags interval\n\t}") {
		t.Fatalf("unresolved domain should give an empty set:\n%s", nft)
	}

	// Only new domains are looked up when a map arrives; the failed one is retried.
	calls := dns.calls
	if changed, _ := s.Refresh(ctx, m, false); changed || dns.calls != calls+1 {
		t.Fatalf("refresh of known domains: changed=%v lookups=%d", changed, dns.calls-calls)
	}

	// The periodic refresh sees a new address: the ruleset changes and is re-applied.
	dns.mu.Lock()
	dns.answers["teams.microsoft.com"] = addrs("52.113.1.1", "52.114.7.7")
	dns.mu.Unlock()
	if changed, _ := s.Refresh(ctx, m, true); !changed {
		t.Fatal("new address not noticed")
	}
	apply()
	if nftRuns() != 3 || !strings.Contains(sys.files["/st/steer.nft"], "elements = { 52.113.1.1, 52.114.7.7 }") {
		t.Fatalf("new address not applied:\n%s", sys.files["/st/steer.nft"])
	}

	// A failed lookup keeps the last good addresses.
	dns.fail["teams.microsoft.com"] = true
	if changed, err := s.Refresh(ctx, m, true); changed || err == nil {
		t.Fatalf("failed lookup: changed=%v err=%v", changed, err)
	}
	apply()
	if nftRuns() != 3 {
		t.Fatal("ruleset changed after a failed lookup")
	}

	// A VLAN subinterface appears: its rule is added.
	ifaces = append(ifaces, "eth0.20", "eth0.4095", "eth0.x")
	if changed, _ := s.Refresh(ctx, m, false); !changed {
		t.Fatal("new VLAN subinterface not noticed")
	}
	apply()
	if nftRuns() != 4 || !strings.Contains(sys.files["/st/steer.nft"], `iifname { "eth0.20" } meta mark set 0x102 ct mark set 0x102 return`) {
		t.Fatalf("VLAN rule missing:\n%s", sys.files["/st/steer.nft"])
	}
	if strings.Contains(sys.files["/st/steer.nft"], "4095") {
		t.Fatal("out-of-range VLAN interface used")
	}
}
