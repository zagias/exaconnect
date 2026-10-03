package render

import (
	"encoding/base64"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

const psk = "s3cret-Pre-Shared-Key"

func popWithCircuits() *desired.State {
	off := false
	circuit := func(id int, peer string, asn int, imports ...string) desired.Circuit {
		n := "vc" + string(rune('0'+id))
		return desired.Circuit{ID: id, Name: n, IfID: uint32(id), UnderlayInterface: "eth5",
			LocalAddress: "100.64.0.2", RemoteAddress: "100.64.10.2", PSK: psk,
			IKEProposals: "aes256-sha256-modp2048", ESPProposals: "aes256gcm16-modp2048",
			InsideAddress: "169.254.100.2/30", PeerInside: peer, PeerASN: asn,
			ImportPrefixes: imports, MaxPrefixes: 100, ExportPrefixes: []string{"192.168.10.0/24", "192.168.20.0/24"}, ShapeKbit: 50000}
	}
	c7 := circuit(7, "169.254.100.1", 64512, "10.100.0.0/16", "10.100.9.9/32")
	c7.ExportCircuits = []int{8, 9}
	c8 := circuit(8, "169.254.100.5", 12076)
	c8.InsideAddress, c8.ExportPrefixes = "169.254.100.6/30", nil
	c9 := circuit(9, "169.254.100.9", 16550, "10.200.0.0/16")
	c9.InsideAddress, c9.Enabled = "169.254.100.10/30", &off
	s := &desired.State{Schema: 1, Version: 4, NodeName: "pop-miami", Role: desired.RolePoP, ASN: 65000, RouterID: "100.64.1.1",
		Loopback:    "10.254.0.1/32",
		BFDProfiles: []desired.BFDProfile{{Name: "terrestrial", TxMS: 200, RxMS: 200, Multiplier: 3}},
		Tunnels: []desired.Tunnel{{Name: "wg-a", Path: "carrier-a", Address: "100.64.1.1/24", ListenPort: 51820,
			Peers:     []desired.Peer{{Name: "site-a", PublicKey: key, AllowedIPs: []string{"100.64.1.11/32"}}},
			Neighbors: []desired.Neighbor{{Address: "100.64.1.11", ASN: 65001, BFDProfile: "terrestrial"}}}},
		Circuits: []desired.Circuit{c7, c8, c9},
	}
	return s
}

func TestFRRCircuits(t *testing.T) {
	s := popWithCircuits()
	if err := s.Validate(); err != nil {
		t.Fatal(err)
	}
	got := FRR(s)
	for _, want := range []string{
		"  network 10.254.0.1/32\n",
		" neighbor 169.254.100.1 remote-as 64512\n",
		" neighbor 169.254.100.1 description vc7\n",
		"  neighbor 169.254.100.1 prefix-list VC7-IN in\n",
		"  neighbor 169.254.100.1 prefix-list VC7-OUT out\n",
		"  neighbor 169.254.100.1 maximum-prefix 100\n",
		"ip prefix-list VC7-IN seq 5 permit 10.100.0.0/16 le 32\n",
		"ip prefix-list VC7-IN seq 10 permit 10.100.9.9/32\n", // a /32 takes no "le 32"
		"ip prefix-list VC7-OUT seq 5 permit 192.168.10.0/24\n",
		"ip prefix-list VC7-OUT seq 10 permit 192.168.20.0/24\n",
		// vc8 accepts any prefix, so there is nothing named to pass on from it.
		"ip prefix-list VC8-IN seq 5 permit any\n",
		"ip prefix-list VC8-OUT seq 5 deny any\n",
		" neighbor 100.64.1.11 bfd profile terrestrial\n", // the site neighbour is untouched
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in:\n%s", want, got)
		}
	}
	for _, bad := range []string{
		"169.254.100.1 bfd", "169.254.100.5 bfd", // no BFD to clouds
		"169.254.100.9", "VC9-", // vc9 is disabled
		"10.200.0.0", // so vc7 exports nothing of it
		psk,
	} {
		if strings.Contains(got, bad) {
			t.Errorf("unexpected %q in:\n%s", bad, got)
		}
	}
	// Cloud-to-cloud: vc8 exports what vc7 accepts.
	s.Circuits[1].ExportCircuits = []int{7}
	got = FRR(s)
	if !strings.Contains(got, "ip prefix-list VC8-OUT seq 5 permit 10.100.0.0/16 le 32\n") || strings.Contains(got, "VC8-OUT seq 5 deny") {
		t.Errorf("vc8 should export vc7's prefixes:\n%s", got)
	}
}

func TestFRRSiteLoopback(t *testing.T) {
	s := site()
	s.Loopback = "10.254.0.11/32"
	got := FRR(s)
	if !strings.Contains(got, "  network 10.254.0.11/32\n  network 192.168.10.0/24\n") {
		t.Fatalf("loopback not advertised:\n%s", got)
	}
	if strings.Contains(got, "prefix-list") {
		t.Fatalf("a site has no circuit filters:\n%s", got)
	}
}

func TestSwanctl(t *testing.T) {
	c := popWithCircuits().Circuits[0]
	c.PSK = `we"ird}\key` // quoting cannot break out of the file
	got := Swanctl(c)
	for _, want := range []string{
		"connections {\n  exa-vc7 {\n",
		"    version = 2\n",
		"    local_addrs = 100.64.0.2\n",
		"    remote_addrs = 100.64.10.2\n",
		"    proposals = aes256-sha256-modp2048\n",
		"    local {\n      auth = psk\n      id = 100.64.0.2\n    }\n",
		"    remote {\n      auth = psk\n      id = 100.64.10.2\n    }\n",
		"        local_ts = 0.0.0.0/0\n        remote_ts = 0.0.0.0/0\n",
		"        esp_proposals = aes256gcm16-modp2048\n",
		"        if_id_in = 7\n        if_id_out = 7\n",
		"        start_action = start\n",
		"        dpd_action = restart\n",
		"secrets {\n  ike-exa-vc7 {\n    id-local = 100.64.0.2\n    id-remote = 100.64.10.2\n",
		"    secret = 0s" + base64.StdEncoding.EncodeToString([]byte(c.PSK)) + "\n",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in:\n%s", want, got)
		}
	}
	if strings.Contains(got, c.PSK) {
		t.Fatal("the key must only appear encoded")
	}
	if strings.Count(got, "{") != strings.Count(got, "}") {
		t.Fatalf("unbalanced braces:\n%s", got)
	}
}
