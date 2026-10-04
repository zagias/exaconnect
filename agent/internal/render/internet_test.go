package render

import (
	"reflect"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

func inetPoP() *desired.State {
	return &desired.State{Role: desired.RolePoP, Internet: &desired.Internet{Mode: "gateway",
		LANPrefixes:   []string{"192.168.30.0/24", "192.168.10.0/24", "192.168.10.0/25"},
		Uplinks:       []desired.Uplink{{Interface: "eth9", Gateway: "100.64.0.1"}},
		PublicAddress: "100.64.0.2",
		Firewall: []desired.FirewallRule{
			{ID: 12, Action: "deny", Src: []string{"192.168.10.0/24"}, Dst: []string{"198.51.100.0/24"}, Protocol: "icmp"},
			{ID: 13, Action: "allow", Dst: []string{"203.0.113.7", "203.0.113.0/24"}, Protocol: "tcp", Ports: "443,8000-8100"},
			{ID: 14, Action: "deny", Protocol: "udp"},
			{ID: 15, Action: "deny", Protocol: "any"},
		},
		PortForwards: []desired.PortForward{
			{ID: 3, Protocol: "tcp", Port: 8080, ToAddress: "192.168.10.10", ToPort: 80, AllowFrom: []string{"0.0.0.0/0"}},
			{ID: 4, Protocol: "udp", Port: 5060, ToAddress: "192.168.30.5", ToPort: 5060},
		}}}
}

const popNFT = `table ip exa_inet {}
delete table ip exa_inet
table ip exa_inet {
	set lan {
		type ipv4_addr
		flags interval
		elements = { 192.168.10.0/24, 192.168.30.0/24 }
	}
	chain pre {
		type nat hook prerouting priority dstnat;
		iifname "eth9" ip daddr 100.64.0.2 tcp dport 8080 dnat to 192.168.10.10:80
		iifname "eth9" ip daddr 100.64.0.2 udp dport 5060 dnat to 192.168.30.5:5060
	}
	chain post {
		type nat hook postrouting priority srcnat;
		oifname { "eth9" } ip saddr @lan masquerade
	}
	chain filter_fwd {
		type filter hook forward priority filter; policy accept;
		ct state established,related accept
		iifname { "eth9" } ct status dnat ip daddr 192.168.10.10 tcp dport 80 ip saddr { 0.0.0.0/0 } counter accept comment "pf3"
		iifname { "eth9" } ct status dnat ip daddr 192.168.30.5 udp dport 5060 counter accept comment "pf4"
		iifname { "eth9" } counter drop comment "inbound"
		oifname { "eth9" } ip saddr @lan ip saddr { 192.168.10.0/24 } ip daddr { 198.51.100.0/24 } meta l4proto icmp counter drop comment "fw12"
		oifname { "eth9" } ip saddr @lan ip daddr { 203.0.113.0/24 } tcp dport { 443, 8000-8100 } counter accept comment "fw13"
		oifname { "eth9" } ip saddr @lan meta l4proto udp counter drop comment "fw14"
		oifname { "eth9" } ip saddr @lan counter drop comment "fw15"
	}
}
`

const localNFT = `table ip exa_inet {}
delete table ip exa_inet
table ip exa_inet {
	set lan {
		type ipv4_addr
		flags interval
		elements = { 192.168.10.0/24 }
	}
	chain post {
		type nat hook postrouting priority srcnat;
		oifname { "eth1", "eth2" } ip saddr @lan masquerade
	}
	chain filter_fwd {
		type filter hook forward priority filter; policy accept;
		ct state established,related accept
		iifname { "eth1", "eth2" } counter drop comment "inbound"
		oifname { "eth1", "eth2" } ip saddr @lan ip daddr { 198.51.100.0/24 } counter drop comment "fw7"
	}
}
`

func inetLocal() *desired.State {
	return &desired.State{Role: desired.RoleSite, Internet: &desired.Internet{Mode: "local",
		LANPrefixes: []string{"192.168.10.0/24"},
		Uplinks: []desired.Uplink{
			{Interface: "eth1", Gateway: "10.11.1.1", Path: "carrier-a", Tunnel: "wg-a"},
			{Interface: "eth2", Gateway: "10.12.1.1", Path: "carrier-b", Tunnel: "wg-b"},
		},
		Firewall: []desired.FirewallRule{{ID: 7, Action: "deny", Dst: []string{"198.51.100.0/24"}, Protocol: "any"}}}}
}

func TestInternetNFT(t *testing.T) {
	if got := InternetNFT(inetPoP()); got != popNFT {
		t.Errorf("pop:\n%s\nwant:\n%s", got, popNFT)
	}
	if got := InternetNFT(inetLocal()); got != localNFT {
		t.Errorf("local:\n%s\nwant:\n%s", got, localNFT)
	}
}

func TestInternetNFTOnlyWhereFiltered(t *testing.T) {
	for _, mode := range []string{"pop", "off"} {
		s := inetLocal()
		s.Internet.Mode = mode
		if got := InternetNFT(s); got != "" {
			t.Errorf("%s-mode site should have no table:\n%s", mode, got)
		}
	}
	if got := InternetNFT(&desired.State{Role: desired.RolePoP}); got != "" {
		t.Errorf("no block, no table:\n%s", got)
	}
	// A local site whose links have no gateway yet: the table, with nothing going out.
	s := inetLocal()
	s.Internet.Uplinks = nil
	want := `table ip exa_inet {}
delete table ip exa_inet
table ip exa_inet {
	set lan {
		type ipv4_addr
		flags interval
		elements = { 192.168.10.0/24 }
	}
	chain post {
		type nat hook postrouting priority srcnat;
	}
	chain filter_fwd {
		type filter hook forward priority filter; policy accept;
		ct state established,related accept
	}
}
`
	if got := InternetNFT(s); got != want {
		t.Errorf("no uplinks:\n%s", got)
	}
}

func TestInternetRules(t *testing.T) {
	in := &desired.Internet{LANPrefixes: []string{"192.168.10.0/24", "192.168.20.7/24", "192.168.10.0/24", "10.9.9.9/32"}}
	got := InternetRules(in, 31000, 31001, 251)
	want := []string{
		"pref 31000 from 192.168.10.0/24 lookup main suppress_prefixlength 0",
		"pref 31001 from 192.168.10.0/24 lookup 251",
		"pref 31000 from 192.168.20.0/24 lookup main suppress_prefixlength 0",
		"pref 31001 from 192.168.20.0/24 lookup 251",
		"pref 31000 from 10.9.9.9/32 lookup main suppress_prefixlength 0",
		"pref 31001 from 10.9.9.9/32 lookup 251",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q", got)
	}
	if InternetRules(nil, 31000, 31001, 251) != nil {
		t.Fatal("no block, no rules")
	}
}
