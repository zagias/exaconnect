package desired

import (
	"bytes"
	"fmt"
	"log/slog"
	"strings"
	"testing"
)

const psk = "s3cret-Pre-Shared-Key"

func pop() *State {
	return &State{Schema: 1, Version: 1, NodeName: "pop-miami", Role: RolePoP, ASN: 65000, RouterID: "100.64.1.1",
		Loopback:  "10.254.0.1/32",
		Reflector: &Reflector{Listen: ":7000"},
		Circuits: []Circuit{{
			ID: 7, Name: "vc7", IfID: 7, UnderlayInterface: "eth5", LocalAddress: "100.64.0.2", RemoteAddress: "100.64.10.2",
			PSK: psk, IKEProposals: "aes256-sha256-modp2048", ESPProposals: "aes256-sha256-modp2048",
			InsideAddress: "169.254.100.2/30", PeerInside: "169.254.100.1", PeerASN: 64512,
			ImportPrefixes: []string{"10.100.0.0/16"}, MaxPrefixes: 100, ExportPrefixes: []string{"192.168.10.0/24"},
			ShapeKbit: 50000,
		}},
	}
}

func site() *State {
	return &State{Schema: 1, Version: 1, NodeName: "site-a", Role: RoleSite, ASN: 65001, RouterID: "100.64.1.11",
		Loopback:  "10.254.0.11/32",
		Reflector: &Reflector{Listen: "10.254.0.11:7000"},
		L2Circuits: []L2Circuit{{ID: 9, Name: "vx9", VNI: 10009, VLAN: 100, Parent: "eth4", Remote: "10.254.0.12",
			ShapeKbit: 20000, MTU: 1370, Probe: &Probe{Target: "10.254.0.12:7000", IntervalMS: 1000}}},
	}
}

func TestValidateCircuits(t *testing.T) {
	if err := pop().Validate(); err != nil {
		t.Fatal(err)
	}
	if err := site().Validate(); err != nil {
		t.Fatal(err)
	}
	bad := map[string]func(s *State){
		"name":          func(s *State) { s.Circuits[0].Name = "vc7; reboot" },
		"if_id":         func(s *State) { s.Circuits[0].IfID = 0 },
		"underlay":      func(s *State) { s.Circuits[0].UnderlayInterface = "eth5 up" },
		"remote":        func(s *State) { s.Circuits[0].RemoteAddress = "gateway.example" },
		"psk empty":     func(s *State) { s.Circuits[0].PSK = "" },
		"psk newline":   func(s *State) { s.Circuits[0].PSK = psk + "\n}" },
		"proposals":     func(s *State) { s.Circuits[0].IKEProposals = "aes256\n}" },
		"inside":        func(s *State) { s.Circuits[0].InsideAddress = "169.254.100.2" },
		"peer outside":  func(s *State) { s.Circuits[0].PeerInside = "169.254.101.1" },
		"peer is us":    func(s *State) { s.Circuits[0].PeerInside = "169.254.100.2" },
		"import":        func(s *State) { s.Circuits[0].ImportPrefixes = []string{"10.100.0.0/33"} },
		"export":        func(s *State) { s.Circuits[0].ExportPrefixes = []string{"any"} },
		"duplicate":     func(s *State) { s.Circuits = append(s.Circuits, s.Circuits[0]) },
		"on a site":     func(s *State) { s.Role = RoleSite },
		"loopback /24":  func(s *State) { s.Loopback = "10.254.0.0/24" },
		"reflector":     func(s *State) { s.Reflector.Listen = "7000" },
		"reflector bad": func(s *State) { s.Reflector.Listen = "nowhere:7000" },
	}
	for name, mutate := range bad {
		s := pop()
		mutate(s)
		err := s.Validate()
		if err == nil {
			t.Errorf("%s: expected rejection", name)
			continue
		}
		if strings.Contains(err.Error(), psk) {
			t.Errorf("%s: error leaks the psk: %v", name, err)
		}
	}
	// A disabled circuit is ignored, even one that would not validate.
	s := pop()
	off := false
	s.Circuits[0].Enabled, s.Circuits[0].PSK = &off, ""
	if err := s.Validate(); err != nil || len(s.ActiveCircuits()) != 0 {
		t.Fatalf("disabled circuit: %v, active %d", err, len(s.ActiveCircuits()))
	}
}

// A resilient circuit's second tunnel is a second entry, numbered 1,000,000
// above the circuit (ADR 0012).
func TestValidateResilientCircuit(t *testing.T) {
	s := pop()
	c := s.Circuits[0]
	c.ID, c.Name, c.IfID = 1000007, "vc1000007", 1000007
	c.RemoteAddress, c.InsideAddress, c.PeerInside = "100.64.10.3", "169.254.100.6/30", "169.254.100.5"
	s.Circuits = append(s.Circuits, c)
	if err := s.Validate(); err != nil {
		t.Fatal(err)
	}
	if got := s.Circuits[1].Conn(); got != "exa-vc1000007" {
		t.Fatalf("conn %q", got)
	}
}

func TestValidateL2(t *testing.T) {
	bad := map[string]func(s *State){
		"name":      func(s *State) { s.L2Circuits[0].Name = "eth0" },
		"vni":       func(s *State) { s.L2Circuits[0].VNI = 1 << 24 },
		"vlan":      func(s *State) { s.L2Circuits[0].VLAN = 4095 },
		"parent":    func(s *State) { s.L2Circuits[0].Parent = "eth4; ls" },
		"long name": func(s *State) { s.L2Circuits[0].Parent = "enp0s31f6abcd" },
		"remote":    func(s *State) { s.L2Circuits[0].Remote = "site-b" },
		"mtu":       func(s *State) { s.L2Circuits[0].MTU = 100 },
		"probe":     func(s *State) { s.L2Circuits[0].Probe.Target = "10.254.0.12" },
		"no lo":     func(s *State) { s.Loopback = "" },
		"on pop":    func(s *State) { s.Role = RolePoP },
		"dup vlan": func(s *State) {
			c := s.L2Circuits[0]
			c.ID, c.Name, c.VNI = 10, "vx10", 10010
			s.L2Circuits = append(s.L2Circuits, c)
		},
	}
	for name, mutate := range bad {
		s := site()
		mutate(s)
		if s.Validate() == nil {
			t.Errorf("%s: expected rejection", name)
		}
	}
	c := site().L2Circuits[0]
	if c.Bridge() != "br9" || c.VLANDev() != "eth4.100" {
		t.Fatalf("names %s %s", c.Bridge(), c.VLANDev())
	}
}

func TestCircuitNeverPrintsPSK(t *testing.T) {
	c := pop().Circuits[0]
	for _, s := range []string{fmt.Sprint(c), fmt.Sprintf("%v %+v %s", c, c, c), fmt.Sprintf("%v", []Circuit{c})} {
		if strings.Contains(s, psk) {
			t.Fatalf("psk printed: %s", s)
		}
	}
	var buf bytes.Buffer
	for _, h := range []slog.Handler{slog.NewTextHandler(&buf, nil), slog.NewJSONHandler(&buf, nil)} {
		slog.New(h).Info("circuit", "c", c)
	}
	if strings.Contains(buf.String(), psk) || !strings.Contains(buf.String(), "vc7") {
		t.Fatalf("psk logged or circuit missing: %s", buf.String())
	}
}
