package render

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// The privileged packet harness consumes rules from the production renderer,
// rather than maintaining a second implementation of the firewall in Python.
func TestExportFirewallPacketFixtures(t *testing.T) {
	dir := os.Getenv("EXA_FIREWALL_FIXTURE_DIR")
	if dir == "" {
		t.Skip("set EXA_FIREWALL_FIXTURE_DIR for the isolated Linux packet job")
	}
	if !filepath.IsAbs(dir) {
		t.Fatal("fixture directory must be absolute")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	s := &desired.State{Schema: 1, Version: 1, Role: desired.RolePoP, ASN: 65000, RouterID: "192.0.2.1",
		Internet: &desired.Internet{Mode: desired.InternetGateway,
			LANPrefixes: []string{"10.70.1.0/24", "10.70.2.0/24"}, PublicAddress: "192.0.2.1",
			Uplinks: []desired.Uplink{{Interface: "eth9", Gateway: "192.0.2.2"}},
			Firewall: []desired.FirewallRule{
				{ID: 11, Action: "deny", Src: []string{"10.70.1.0/24"}, Dst: []string{"198.51.100.10"}, Protocol: "tcp", Ports: "8001"},
				{ID: 12, Action: "allow", Src: []string{"10.70.1.0/24"}, Dst: []string{"198.51.100.10"}, Protocol: "tcp", Ports: "8002"},
				{ID: 13, Action: "deny", Src: []string{"10.70.1.0/24"}, Dst: []string{"198.51.100.10"}, Protocol: "tcp", Ports: "8002"},
			},
			PortForwards: []desired.PortForward{
				{ID: 1, Protocol: "tcp", Port: 8080, ToAddress: "10.70.1.2", ToPort: 9000, AllowFrom: []string{"192.0.2.2"}},
				{ID: 2, Protocol: "tcp", Port: 8081, ToAddress: "10.70.1.2", ToPort: 9000, AllowFrom: []string{"192.0.2.3"}},
			}}}
	if err := s.Validate(); err != nil {
		t.Fatal(err)
	}
	scripts := map[string]string{"gateway.nft": InternetNFT(s), "guard.nft": InternetForwardGuard(s)}
	s.Internet.Firewall[1], s.Internet.Firewall[2] = s.Internet.Firewall[2], s.Internet.Firewall[1]
	scripts["first-deny.nft"] = InternetNFT(s)
	off := &desired.State{Schema: 1, Version: 2, Role: desired.RoleSite, ASN: 65001, RouterID: "10.70.1.1",
		Internet: &desired.Internet{Mode: desired.InternetOff,
			LANPrefixes: []string{"10.70.1.0/24"}, CorporatePrefixes: []string{"10.70.0.0/16"}}}
	if err := off.Validate(); err != nil {
		t.Fatal(err)
	}
	scripts["off.nft"] = InternetForwardGuard(off)
	for name, script := range scripts {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(script), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}
