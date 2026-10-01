package render

import (
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

const key = "aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8gd28="

func site() *desired.State {
	return &desired.State{
		Schema: 1, Version: 3, NodeName: "site-a", Role: desired.RoleSite, ASN: 65001, RouterID: "100.64.1.11",
		LANPrefixes: []string{"192.168.10.0/24"},
		BFDProfiles: []desired.BFDProfile{{Name: "terrestrial", TxMS: 200, RxMS: 200, Multiplier: 3}},
		Tunnels: []desired.Tunnel{
			{Name: "wg-a", Path: "carrier-a", Address: "100.64.1.11/24",
				Peers:     []desired.Peer{{Name: "pop-miami", PublicKey: key, Endpoint: "10.11.0.2:51820", AllowedIPs: []string{"0.0.0.0/0"}, Keepalive: 10}},
				Neighbors: []desired.Neighbor{{Address: "100.64.1.1", ASN: 65000, BFDProfile: "terrestrial", LocalPref: 200}}},
			{Name: "wg-b", Path: "carrier-b", Address: "100.64.2.11/24",
				Peers:     []desired.Peer{{Name: "pop-miami", PublicKey: key, Endpoint: "10.12.0.2:51821", AllowedIPs: []string{"0.0.0.0/0"}}},
				Neighbors: []desired.Neighbor{{Address: "100.64.2.1", ASN: 65000, BFDProfile: "terrestrial"}}},
		},
	}
}

func TestValidate(t *testing.T) {
	s := site()
	if err := s.Validate(); err != nil {
		t.Fatal(err)
	}
	s.Tunnels[0].Name = "eth0; rm -rf /"
	if s.Validate() == nil {
		t.Fatal("expected bad tunnel name to be rejected")
	}
	s = site()
	s.Tunnels[1].Neighbors[0].BFDProfile = "nope"
	if s.Validate() == nil {
		t.Fatal("expected unknown bfd profile to be rejected")
	}
}

func TestWireGuard(t *testing.T) {
	got := WireGuard(site().Tunnels[0], "PRIV")
	for _, want := range []string{"PrivateKey = PRIV", "Endpoint = 10.11.0.2:51820", "AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 10"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in:\n%s", want, got)
		}
	}
	if strings.Contains(got, "ListenPort") {
		t.Error("site tunnels should not set a listen port")
	}
}

func TestFRR(t *testing.T) {
	got := FRR(site())
	for _, want := range []string{
		"router bgp 65001",
		"bgp router-id 100.64.1.11",
		"neighbor 100.64.1.1 remote-as 65000",
		"neighbor 100.64.1.1 bfd profile terrestrial",
		"neighbor 100.64.2.1 update-source wg-b",
		"network 192.168.10.0/24",
		"profile terrestrial\n  detect-multiplier 3",
		"neighbor 100.64.1.1 route-map LP-WG-A in",
		"route-map LP-WG-A permit 10\n set local-preference 200",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in:\n%s", want, got)
		}
	}
	if strings.Contains(got, "LP-WG-B") {
		t.Error("no route-map expected for a neighbour without local_pref")
	}
}
