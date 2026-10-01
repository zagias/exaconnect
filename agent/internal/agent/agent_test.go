package agent

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/apply"
	"github.com/zagias/exaconnect/agent/internal/client"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

// fakeSys records commands and keeps files in memory, like a node that never fails.
type fakeSys struct {
	mu    sync.Mutex
	cmds  []string
	files map[string]string
}

func (f *fakeSys) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cmds = append(f.cmds, name+" "+strings.Join(args, " "))
	if name == "vtysh" && len(args) > 1 && strings.Contains(args[1], "json") {
		return []byte("[]"), nil
	}
	return nil, nil
}
func (f *fakeSys) WriteFile(p string, d []byte, _ os.FileMode) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.files[p] = string(d)
	return nil
}
func (f *fakeSys) Exists(p string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.files[p]
	return ok
}
func (f *fakeSys) ReadFile(p string) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if d, ok := f.files[p]; ok {
		return []byte(d), nil
	}
	return nil, os.ErrNotExist
}
func (f *fakeSys) count(sub string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.cmds {
		if strings.Contains(c, sub) {
			n++
		}
	}
	return n
}

const key = "aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8gd28="

func state(v int64) desired.State {
	return desired.State{Schema: 1, Version: v, NodeName: "site-a", Role: desired.RoleSite, ASN: 65001, RouterID: "100.64.1.11",
		Tunnels: []desired.Tunnel{{Name: "wg-a", Address: "100.64.1.11/24",
			Peers: []desired.Peer{{Name: "pop", PublicKey: key, AllowedIPs: []string{"0.0.0.0/0"}}}}}}
}

func newAgent(base string, sys *fakeSys) *Agent {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return &Agent{
		Cfg:     Config{PollInterval: 20 * time.Millisecond, TelemetryInterval: 20 * time.Millisecond, CounterInterval: time.Hour, SilentAfter: 50 * time.Millisecond},
		Client:  &client.Client{Base: base, HTTP: &http.Client{Timeout: time.Second}},
		Sys:     sys,
		Log:     log,
		Applier: &apply.Applier{Sys: sys, StateDir: "/state", PrivateKey: "PRIV", FRRConf: "/frr.conf", Log: log},
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestAgentAppliesAndSurvivesControllerLoss(t *testing.T) {
	var mu sync.Mutex
	var statuses []client.Status
	var telemetry int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/agent/desired-state":
			if r.URL.Query().Get("have") == "1" {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			json.NewEncoder(w).Encode(state(1))
		case "/api/v1/agent/status":
			var s client.Status
			json.NewDecoder(r.Body).Decode(&s)
			mu.Lock()
			statuses = append(statuses, s)
			mu.Unlock()
		case "/api/v1/agent/telemetry":
			mu.Lock()
			telemetry++
			mu.Unlock()
		}
	}))

	sys := &fakeSys{files: map[string]string{}}
	ctx, cancel := context.WithCancel(context.Background())
	a := newAgent(srv.URL, sys)
	done := make(chan struct{})
	go func() { a.Run(ctx); close(done) }()

	waitFor(t, "status report", func() bool { mu.Lock(); defer mu.Unlock(); return len(statuses) > 0 })
	mu.Lock()
	if !statuses[0].OK || statuses[0].AppliedVersion != 1 {
		t.Fatalf("status %+v", statuses[0])
	}
	mu.Unlock()
	waitFor(t, "telemetry", func() bool { mu.Lock(); defer mu.Unlock(); return telemetry > 0 })

	// The controller goes away: the agent keeps running and does not touch the node.
	srv.Close()
	applied := sys.count("wg syncconf")
	time.Sleep(150 * time.Millisecond)
	if sys.count("wg syncconf") != applied {
		t.Fatal("agent re-applied config while the controller was down")
	}
	a.mu.Lock()
	silent := a.silent
	a.mu.Unlock()
	if !silent {
		t.Fatal("agent did not notice the controller was silent")
	}
	cancel()
	<-done

	// A restart with the controller still down brings back the last good state.
	ctx2, cancel2 := context.WithCancel(context.Background())
	defer cancel2()
	b := newAgent("http://127.0.0.1:1", sys)
	go b.Run(ctx2)
	waitFor(t, "re-apply of last good", func() bool { return sys.count("wg syncconf") > applied })
	if got := b.have(); got != 1 {
		t.Fatalf("restarted agent holds version %d, want 1", got)
	}
}

func TestBFDMapsPeersToTunnels(t *testing.T) {
	s := state(1)
	s.Tunnels = append(s.Tunnels, desired.Tunnel{Name: "wg-b"})
	s.Tunnels[0].Neighbors = []desired.Neighbor{{Address: "100.64.1.1"}}
	s.Tunnels[1].Neighbors = []desired.Neighbor{{Address: "100.64.2.11"}, {Address: "100.64.2.12"}}

	// FRR omits the interface for BGP-created single-hop sessions.
	if got := tunnelOf(&s, "100.64.1.1", ""); got != "wg-a" {
		t.Fatalf("tunnelOf = %q, want wg-a", got)
	}
	if got := tunnelOf(&s, "100.64.9.9", "wg-x"); got != "wg-x" {
		t.Fatalf("interface from FRR should win, got %q", got)
	}

	peers := map[string]bfdPeer{
		"100.64.1.1":  {Tunnel: "wg-a", Status: "up"},
		"100.64.2.11": {Tunnel: "wg-b", Status: "up"},
		"100.64.2.12": {Tunnel: "wg-b", Status: "down"},
	}
	for tunnel, want := range map[string]string{"wg-a": "up", "wg-b": "down", "wg-sat": ""} {
		if got := tunnelBFD(peers, tunnel); got != want {
			t.Errorf("tunnelBFD(%s) = %q, want %q", tunnel, got, want)
		}
	}
}

// bfdSys reports BFD peers from a map the test can change.
type bfdSys struct {
	*fakeSys
	mu   sync.Mutex
	bfd  map[string]string // peer -> status
	tuns map[string]string // peer -> interface
}

func (b *bfdSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	if name == "vtysh" {
		b.mu.Lock()
		defer b.mu.Unlock()
		var peers []map[string]string
		for p, st := range b.bfd {
			peers = append(peers, map[string]string{"peer": p, "interface": b.tuns[p], "status": st})
		}
		return json.Marshal(peers)
	}
	if name == "ip" && len(args) > 0 && args[0] == "-batch" {
		b.fakeSys.mu.Lock()
		body := b.fakeSys.files[args[1]]
		b.fakeSys.mu.Unlock()
		return b.fakeSys.Run(ctx, name, "-batch", body)
	}
	return b.fakeSys.Run(ctx, name, args...)
}

func TestAgentSteersAndFailsOverOnBFD(t *testing.T) {
	ds := state(1)
	ds.Tunnels = []desired.Tunnel{
		{Name: "wg-a", Path: "carrier-a", Address: "100.64.1.11/24", Peers: ds.Tunnels[0].Peers, Neighbors: []desired.Neighbor{{Address: "100.64.1.1", ASN: 65000}}},
		{Name: "wg-b", Path: "carrier-b", Address: "100.64.2.11/24", Peers: ds.Tunnels[0].Peers, Neighbors: []desired.Neighbor{{Address: "100.64.2.1", ASN: 65000}}},
	}
	m := steer.Map{
		Version: 5,
		Paths:   []steer.Path{{Name: "carrier-a", Tunnel: "wg-a", Table: 101}, {Name: "carrier-b", Tunnel: "wg-b", Table: 102}},
		Classes: []steer.Class{{Name: "voice", Mark: 0x101, DSCP: []int{46}}},
		Rules:   []steer.Rule{{Class: "voice", Paths: []string{"carrier-b", "carrier-a"}}},
	}
	var evMu sync.Mutex
	var events []Event
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/agent/desired-state":
			if r.URL.Query().Get("have") == "1" {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			json.NewEncoder(w).Encode(ds)
		case "/api/v1/agent/steering":
			if r.URL.Query().Get("have") == "5" {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			json.NewEncoder(w).Encode(m)
		case "/api/v1/agent/telemetry":
			var tel Telemetry
			json.NewDecoder(r.Body).Decode(&tel)
			evMu.Lock()
			events = append(events, tel.Events...)
			evMu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer srv.Close()

	sys := &bfdSys{fakeSys: &fakeSys{files: map[string]string{}},
		bfd:  map[string]string{"100.64.1.1": "up", "100.64.2.1": "up"},
		tuns: map[string]string{"100.64.1.1": "wg-a", "100.64.2.1": "wg-b"}}
	a := newAgent(srv.URL, sys.fakeSys)
	a.Sys = sys
	a.Applier.Sys = sys
	a.Steerer = &steer.Steerer{Sys: sys, StateDir: "/state"}
	a.Cfg.SteerInterval = 20 * time.Millisecond
	a.Cfg.BFDInterval = 10 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go a.Run(ctx)

	waitFor(t, "voice on carrier-b", func() bool { return sys.count("fwmark 0x101 lookup 102") > 0 })
	if !sys.Exists("/state/steering.json") {
		t.Fatal("steering map not saved for restarts")
	}

	sys.mu.Lock()
	sys.bfd["100.64.2.1"] = "down"
	sys.mu.Unlock()
	waitFor(t, "voice failover to carrier-a", func() bool { return sys.count("fwmark 0x101 lookup 101") > 0 })

	waitFor(t, "class_moved event reported", func() bool {
		evMu.Lock()
		defer evMu.Unlock()
		for _, e := range events {
			if e.Kind == "class_moved" && e.Detail["to"] == "carrier-a" && e.Detail["why"] == "bfd" && e.Detail["failover"] == "true" {
				return true
			}
		}
		return false
	})
}
