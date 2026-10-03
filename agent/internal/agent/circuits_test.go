package agent

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/counters"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/probe"
)

// circuitSys answers swanctl and vtysh like a PoP with one circuit up.
type circuitSys struct{ fakeSys }

func (c *circuitSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	c.fakeSys.Run(ctx, name, args...)
	cmd := name + " " + strings.Join(args, " ")
	switch {
	case cmd == "swanctl --list-sas":
		return []byte("exa-vc7: #1, ESTABLISHED, IKEv2, 01_i* 02_r\n  exa-vc7: #2, reqid 1, INSTALLED, TUNNEL, ESP:AES_GCM_16-256\nexa-vc8: #3, CONNECTING, IKEv2, 03_i* 00_r\n"), nil
	case cmd == "vtysh -c show bgp neighbors 169.254.100.1 json":
		return []byte(`{"169.254.100.1":{"bgpState":"Established","addressFamilyInfo":{"ipv4Unicast":{"acceptedPrefixCounter":1}}}}`), nil
	case cmd == "vtysh -c show bgp ipv4 unicast neighbors 169.254.100.1 routes json":
		return []byte(`{"routes":{"10.100.0.0/16":[{"valid":true}]}}`), nil
	case cmd == "vtysh -c show bgp neighbors 169.254.100.5 json":
		return []byte(`{"169.254.100.5":{"bgpState":"Active","addressFamilyInfo":{"ipv4Unicast":{}}}}`), nil
	}
	return nil, nil
}

func TestCircuitTelemetry(t *testing.T) {
	dir := t.TempDir()
	old := counters.Root
	counters.Root = dir
	defer func() { counters.Root = old }()
	for dev, v := range map[string][2]string{"vc7": {"123", "456"}, "vx9": {"7", "8"}} {
		os.MkdirAll(filepath.Join(dir, dev, "statistics"), 0o755)
		os.WriteFile(filepath.Join(dir, dev, "statistics", "rx_bytes"), []byte(v[0]+"\n"), 0o644)
		os.WriteFile(filepath.Join(dir, dev, "statistics", "tx_bytes"), []byte(v[1]+"\n"), 0o644)
	}

	sys := &circuitSys{fakeSys{files: map[string]string{}}}
	a := &Agent{Sys: sys, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Cfg: Config{ProbeTimeout: 200 * time.Millisecond}}
	pop := &desired.State{Role: desired.RolePoP, Circuits: []desired.Circuit{
		{ID: 7, Name: "vc7", PeerInside: "169.254.100.1"},
		{ID: 8, Name: "vc8", PeerInside: "169.254.100.5"},
	}}
	got := a.circuitStates(context.Background(), pop, time.Now())
	if len(got) != 2 {
		t.Fatalf("%+v", got)
	}
	c := got[0]
	if c.ID != 7 || c.IKE != "up" || c.BGP != "Established" || c.PrefixesReceived != 1 ||
		len(c.Routes) != 1 || c.Routes[0] != "10.100.0.0/16" || c.BytesIn != 123 || c.BytesOut != 456 || c.RTTMS != nil {
		t.Fatalf("vc7: %+v", c)
	}
	if got[1].IKE != "connecting" || got[1].BGP != "Active" || got[1].Routes == nil {
		t.Fatalf("vc8: %+v", got[1])
	}
	b, _ := json.Marshal(got[1])
	for _, want := range []string{`"ike":"connecting"`, `"routes":[]`, `"rtt_ms":null`, `"sent":0`} {
		if !strings.Contains(string(b), want) {
			t.Errorf("missing %s in %s", want, b)
		}
	}
	if sys.count("routes json") != 1 {
		t.Fatal("routes should only be read for a circuit with prefixes")
	}

	// Layer 2: probes to the far end's reflector, from our loopback.
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go probe.Reflect(ctx, pc)
	site := &desired.State{Role: desired.RoleSite, Loopback: "127.0.0.1/32", L2Circuits: []desired.L2Circuit{
		{ID: 9, Name: "vx9", Probe: &desired.Probe{Target: pc.LocalAddr().String(), IntervalMS: 20}},
	}}
	a.startCircuitProbes(ctx, site)
	defer a.stopCircuitProbes()
	time.Sleep(300 * time.Millisecond)
	got = a.circuitStates(ctx, site, time.Now().Add(time.Second))
	if len(got) != 1 {
		t.Fatalf("%+v", got)
	}
	l2 := got[0]
	if l2.IKE != "" || l2.BGP != "" || l2.Sent == 0 || l2.Received == 0 || l2.RTTMS == nil || l2.BytesIn != 7 || l2.BytesOut != 8 {
		t.Fatalf("vx9: %+v", l2)
	}
	if sys.count("swanctl") != 1 {
		t.Fatal("a site must not ask strongSwan")
	}
	// Nothing sent before the timeout window: no probes, and a null RTT.
	again := a.circuitStates(ctx, site, time.Now().Add(-time.Hour))
	if again[0].Sent != 0 || again[0].RTTMS != nil {
		t.Fatalf("second flush: %+v", again[0])
	}
}

func TestTelemetryCarriesCircuits(t *testing.T) {
	var tel Telemetry
	tel.Circuits = []CircuitState{{ID: 9, Name: "vx9", Routes: []string{}}}
	b, _ := json.Marshal(tel)
	if !strings.Contains(string(b), `"circuits":[{"id":9,"name":"vx9","ike":"","bgp":"","prefixes_received":0,"routes":[],"sent":0,"received":0,"rtt_ms":null,"bytes_in":0,"bytes_out":0}]`) {
		t.Fatalf("%s", b)
	}
}
