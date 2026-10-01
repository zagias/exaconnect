package agent

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

// ctSys serves conntrack output from the conntrack tool and can fail tc.
type ctSys struct {
	*fakeSys
	conntrack string
	tcErr     error
}

func (c *ctSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	switch name {
	case "conntrack":
		c.fakeSys.Run(ctx, name, args...)
		return []byte(c.conntrack), nil
	case "tc":
		c.fakeSys.Run(ctx, name, args...)
		return nil, c.tcErr
	case "ip":
		if len(args) > 1 && args[0] == "-j" {
			return []byte("[]"), nil
		}
		if len(args) > 1 && args[0] == "-batch" { // record the batch body
			body, _ := c.fakeSys.ReadFile(args[1])
			return c.fakeSys.Run(ctx, name, "-batch", string(body))
		}
	}
	return c.fakeSys.Run(ctx, name, args...)
}

func flowMap() *steer.Map {
	return &steer.Map{
		Version:       2,
		Paths:         []steer.Path{{Name: "carrier-a", Tunnel: "wg-a", Table: 101}},
		Classes:       []steer.Class{{Name: "voice", Mark: 0x101}, {Name: "bulk", Mark: 0x103}},
		Rules:         []steer.Rule{{Class: "voice", Paths: []string{"carrier-a"}}},
		LocalPrefixes: []string{"192.168.10.0/24"},
	}
}

const ctLine = "udp      17 29 src=192.168.10.10 dst=192.168.20.10 sport=40000 dport=8801 packets=%d bytes=%d src=192.168.20.10 dst=192.168.10.10 sport=8801 dport=40000 packets=9 bytes=1400 [ASSURED] mark=257 use=1\n"

func quietAgent(sys *ctSys) *Agent {
	a := &Agent{Sys: sys, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	a.Cfg.defaults()
	return a
}

func TestCollectFlowsOnSites(t *testing.T) {
	for _, tc := range []struct {
		name     string
		proc     bool // /proc/net/nf_conntrack exists
		wantTool bool // the conntrack tool is run
	}{
		{name: "proc file", proc: true},
		{name: "conntrack tool", wantTool: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sys := &ctSys{fakeSys: &fakeSys{files: map[string]string{}}}
			read := func(pkts, bytes int) {
				line := strings.Replace(strings.Replace(ctLine, "packets=%d", "packets="+itoa(int64(pkts)), 1), "bytes=%d", "bytes="+itoa(int64(bytes)), 1)
				if tc.proc {
					sys.files[conntrackProc] = "ipv4     2 " + line
				} else {
					sys.conntrack = line
				}
			}
			a := quietAgent(sys)
			ds := state(1)
			a.current, a.steerMap = &ds, flowMap()

			read(10, 1000)
			a.collectFlows(context.Background()) // baseline
			read(25, 2600)
			a.collectFlows(context.Background())
			if got := sys.count("conntrack -L -o extended"); (got > 0) != tc.wantTool {
				t.Fatalf("conntrack tool runs = %d", got)
			}
			if len(a.buf.Flows) != 1 {
				t.Fatalf("flows = %+v", a.buf.Flows)
			}
			f := a.buf.Flows[0]
			if f.Proto != "udp" || f.Dst != "192.168.20.10" || f.DPort != 8801 || f.Class != "voice" || f.Flows != 1 ||
				f.BytesOut != 1600 || f.PktsOut != 15 || f.BytesIn != 0 {
				t.Fatalf("flow %+v", f)
			}
			b, _ := json.Marshal(Telemetry{Flows: a.buf.Flows})
			for _, k := range []string{`"flows":[{"at":`, `"proto":"udp"`, `"dst":"192.168.20.10"`, `"dport":8801`, `"class":"voice"`, `"flows":1`, `"bytes_out":1600`, `"bytes_in":0`, `"pkts_out":15`, `"pkts_in":0`} {
				if !strings.Contains(string(b), k) {
					t.Errorf("telemetry JSON lacks %s: %s", k, b)
				}
			}
		})
	}
}

func TestCollectFlowsSkipsPoPAndCapsBuffer(t *testing.T) {
	sys := &ctSys{fakeSys: &fakeSys{files: map[string]string{}}, conntrack: ctLine}
	a := quietAgent(sys)
	ds := state(1)
	ds.Role = desired.RolePoP
	a.current, a.steerMap = &ds, flowMap()
	a.collectFlows(context.Background())
	if sys.count("conntrack") != 0 {
		t.Fatal("the PoP should not read conntrack")
	}

	// Telemetry without flows leaves the field out.
	if b, _ := json.Marshal(Telemetry{}); strings.Contains(string(b), `"flows"`) {
		t.Fatalf("empty flows not omitted: %s", b)
	}

	// The buffer keeps the newest MaxBuffered aggregates.
	ds.Role = desired.RoleSite
	a.Cfg.MaxBuffered = 3
	for i := 1; i <= 6; i++ {
		sys.conntrack = strings.Replace(strings.Replace(ctLine, "packets=%d", "packets="+itoa(int64(i)), 1), "bytes=%d", "bytes="+itoa(int64(i*100)), 1)
		a.collectFlows(context.Background())
	}
	if len(a.buf.Flows) != 3 || a.buf.Flows[2].BytesOut != 100 {
		t.Fatalf("buffer %+v", a.buf.Flows)
	}
}

func TestQoSFailureReportedOnceAndSteeringContinues(t *testing.T) {
	sys := &ctSys{fakeSys: &fakeSys{files: map[string]string{}}, tcErr: errors.New("tc: Error: Specified qdisc kind is unknown.")}
	a := quietAgent(sys)
	a.Steerer = &steer.Steerer{Sys: sys, StateDir: "/state"}
	a.bfd = map[string]bfdPeer{}
	ds := state(1)
	ds.Tunnels[0].Path = "carrier-a"
	m := flowMap()
	m.QoS = &steer.QoS{Paths: []steer.QoSPath{{Tunnel: "wg-a", ShapeKbit: 100000}}}
	a.current, a.steerMap = &ds, m

	ctx := context.Background()
	a.steer(ctx, "controller")
	a.Steerer.RetryQoS()
	a.steer(ctx, "qos") // retried, same error: no second event
	if n := sys.count("tc qdisc replace dev wg-a root cake bandwidth 100000kbit diffserv4"); n != 2 {
		t.Fatalf("tc runs = %d, want 2", n)
	}
	if sys.count("fwmark 0x101 lookup 101") == 0 {
		t.Fatal("steering not applied")
	}
	var qosEvents int
	for _, e := range a.buf.Events {
		switch e.Kind {
		case "qos_failed":
			qosEvents++
			if !strings.Contains(e.Detail["error"], "qdisc kind is unknown") {
				t.Errorf("event %+v", e)
			}
		case "steering_failed":
			t.Fatalf("qos failure failed steering: %+v", e)
		}
	}
	if qosEvents != 1 {
		t.Fatalf("qos_failed events = %d, want 1", qosEvents)
	}
}
