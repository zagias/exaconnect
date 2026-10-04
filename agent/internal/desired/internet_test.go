package desired

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func inetSite(mode string) *State {
	return &State{Schema: 1, Version: 1, Role: RoleSite, ASN: 65001, RouterID: "100.64.1.11",
		Tunnels: []Tunnel{{Name: "wg-a", Address: "100.64.1.11/24"}, {Name: "wg-b", Address: "100.64.2.11/24"}},
		Internet: &Internet{Mode: mode, LANPrefixes: []string{"192.168.10.0/24"},
			Tunnels: []string{"wg-a", "wg-b"},
			Uplinks: []Uplink{
				{Interface: "eth1", Gateway: "10.11.1.1", Path: "carrier-a", Tunnel: "wg-a"},
				{Interface: "eth2", Gateway: "10.12.1.1", Path: "carrier-b", Tunnel: "wg-b"},
			}}}
}

func inetPoP() *State {
	return &State{Schema: 1, Version: 1, Role: RolePoP, ASN: 65000, RouterID: "100.64.1.1",
		Internet: &Internet{Mode: "gateway", LANPrefixes: []string{"192.168.10.0/24", "192.168.30.0/24"},
			Uplinks:       []Uplink{{Interface: "eth9", Gateway: "100.64.0.1"}},
			PublicAddress: "100.64.0.2",
			Firewall: []FirewallRule{{ID: 12, Action: "deny", Src: []string{"192.168.10.0/24"},
				Dst: []string{"198.51.100.0/24"}, Protocol: "icmp"}},
			PortForwards: []PortForward{{ID: 3, Protocol: "tcp", Port: 8080, ToAddress: "192.168.10.10", ToPort: 8080,
				AllowFrom: []string{"0.0.0.0/0"}}}}}
}

func TestInternetValid(t *testing.T) {
	for _, mode := range []string{"pop", "local", "off"} {
		if err := inetSite(mode).Validate(); err != nil {
			t.Errorf("%s: %v", mode, err)
		}
	}
	s := inetSite("local")
	s.Internet.Firewall = []FirewallRule{
		{ID: 1, Action: "allow", Dst: []string{"203.0.113.7"}, Protocol: "tcp", Ports: "443, 8000-8100"},
		{ID: 2, Action: "deny", Protocol: "any"},
	}
	if err := s.Validate(); err != nil {
		t.Error(err)
	}
	s.Internet.Uplinks = nil // local with no gateway on any link: nothing to break out over
	if err := s.Validate(); err != nil {
		t.Error(err)
	}
	if err := inetPoP().Validate(); err != nil {
		t.Error(err)
	}
	s = inetPoP()
	s.Internet.PortForwards, s.Internet.PublicAddress = nil, ""
	if err := s.Validate(); err != nil {
		t.Error(err)
	}
}

func TestInternetInvalid(t *testing.T) {
	site := []func(in *Internet){
		func(in *Internet) { in.Mode = "gateway" },
		func(in *Internet) { in.Mode = "" },
		func(in *Internet) { in.LANPrefixes = []string{"192.168.10.0/33"} },
		func(in *Internet) { in.LANPrefixes = []string{"fd00::/64"} },
		func(in *Internet) { in.Tunnels = []string{"wg-a", "wg-a"} },
		func(in *Internet) { in.Tunnels = []string{"wg-sat"} }, // not one of the node's tunnels
		func(in *Internet) { in.Tunnels = []string{"eth0"} },
		func(in *Internet) { in.Uplinks[0].Interface = "eth1; reboot" },
		func(in *Internet) { in.Uplinks[1].Interface = "eth1" },
		func(in *Internet) { in.Uplinks[0].Gateway = "" },
		func(in *Internet) { in.Uplinks[0].Gateway = "fe80::1" },
		func(in *Internet) { in.Uplinks[0].Tunnel = "eth0" },
		func(in *Internet) { in.Uplinks[0].Path = "Carrier A" },
		func(in *Internet) {
			in.PortForwards = []PortForward{{ID: 1, Protocol: "tcp", Port: 80, ToAddress: "192.168.10.1", ToPort: 80}}
		},
	}
	for i, f := range site {
		s := inetSite("local")
		f(s.Internet)
		if s.Validate() == nil {
			t.Errorf("site case %d accepted", i)
		}
	}
	rule := func(r FirewallRule) func(in *Internet) {
		return func(in *Internet) { in.Firewall = []FirewallRule{r} }
	}
	ok := FirewallRule{ID: 1, Action: "deny", Protocol: "tcp", Ports: "443"}
	with := func(f func(r *FirewallRule)) func(in *Internet) { r := ok; f(&r); return rule(r) }
	pop := []func(in *Internet){
		func(in *Internet) { in.Mode = "pop" },
		func(in *Internet) { in.Uplinks = nil },
		func(in *Internet) { in.Uplinks = append(in.Uplinks, Uplink{Interface: "eth8", Gateway: "100.64.9.1"}) },
		func(in *Internet) { in.PublicAddress = "" },
		func(in *Internet) { in.PublicAddress = "100.64.0.2/32" },
		with(func(r *FirewallRule) { r.ID = 0 }),
		with(func(r *FirewallRule) { r.Action = "reject" }),
		with(func(r *FirewallRule) { r.Protocol = "sctp" }),
		with(func(r *FirewallRule) { r.Protocol = "" }),
		with(func(r *FirewallRule) { r.Protocol = "icmp" }), // ports need tcp or udp
		with(func(r *FirewallRule) { r.Ports = "0" }),
		with(func(r *FirewallRule) { r.Ports = "65536" }),
		with(func(r *FirewallRule) { r.Ports = "443-80" }),
		with(func(r *FirewallRule) { r.Ports = "tcp:443" }),
		with(func(r *FirewallRule) { r.Ports = "443,,80" }),
		with(func(r *FirewallRule) { r.Src = []string{"192.168.10.0/24 ; flush ruleset"} }),
		with(func(r *FirewallRule) { r.Dst = []string{"2001:db8::/32"} }),
		func(in *Internet) { in.Firewall = append(in.Firewall, in.Firewall[0]) },
		func(in *Internet) { in.PortForwards[0].ID = -1 },
		func(in *Internet) { in.PortForwards[0].Protocol = "icmp" },
		func(in *Internet) { in.PortForwards[0].Port = 0 },
		func(in *Internet) { in.PortForwards[0].ToPort = 70000 },
		func(in *Internet) { in.PortForwards[0].ToAddress = "host.example" },
		func(in *Internet) { in.PortForwards[0].AllowFrom = []string{"any"} },
		func(in *Internet) {
			f := in.PortForwards[0]
			f.ID = 4
			in.PortForwards = append(in.PortForwards, f) // the same public port twice
		},
	}
	for i, f := range pop {
		s := inetPoP()
		f(s.Internet)
		if s.Validate() == nil {
			t.Errorf("pop case %d accepted", i)
		}
	}
	for _, mode := range []string{"pop", "off"} {
		s := inetSite(mode)
		s.Internet.Firewall = []FirewallRule{ok}
		if s.Validate() == nil {
			t.Errorf("%s-mode site accepted firewall rules", mode)
		}
	}
}

func TestInternetFromJSON(t *testing.T) {
	doc := `{"schema":1,"version":4,"role":"site","asn":65001,"router_id":"100.64.1.11",
	  "tunnels":[{"name":"wg-a","address":"100.64.1.11/24","peers":[],"bgp_neighbors":[]}],
	  "internet":{"mode":"local","lan_prefixes":["192.168.10.0/24"],
	    "uplinks":[{"interface":"eth1","gateway":"10.11.1.1","path":"carrier-a","tunnel":"wg-a"}],
	    "firewall":[{"id":12,"action":"deny","src":["192.168.10.0/24"],"dst":["198.51.100.0/24"],"protocol":"icmp","ports":""}]}}`
	var s State
	if err := json.Unmarshal([]byte(doc), &s); err != nil {
		t.Fatal(err)
	}
	if err := s.Validate(); err != nil {
		t.Fatal(err)
	}
	if !s.Firewalled() || s.Internet.Firewall[0].ID != 12 || s.Internet.Uplinks[0].Tunnel != "wg-a" {
		t.Fatalf("%+v", s.Internet)
	}
	s.Internet = nil
	if s.Firewalled() {
		t.Fatal("no block, no firewall")
	}
}

func TestParsePorts(t *testing.T) {
	got, err := ParsePorts("443, 8000-8100,8443")
	if err != nil {
		t.Fatal(err)
	}
	want := []PortRange{{443, 443}, {8000, 8100}, {8443, 8443}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v", got)
	}
	if r, err := ParsePorts(" "); err != nil || r != nil {
		t.Fatalf("empty: %v %v", r, err)
	}
	for _, bad := range []string{"-1", "1-", "a", "1-2-3", "443 80", strings.Repeat("9", 6), "+80"} {
		if _, err := ParsePorts(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}
