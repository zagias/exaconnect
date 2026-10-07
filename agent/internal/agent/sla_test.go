package agent

import (
	"context"
	"io"
	"log/slog"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/probe"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

func fp(v float64) *float64 { return &v }

func slaAgent(t *testing.T) *Agent {
	t.Helper()
	sys := &fakeSys{files: map[string]string{}}
	ds := state(1)
	ds.Tunnels = []desired.Tunnel{
		{Name: "wg-a", Path: "carrier-a", Address: "100.64.1.11/24"},
		{Name: "wg-b", Path: "carrier-b", Address: "100.64.2.11/24"},
	}
	m := &steer.Map{
		Version: 5,
		Paths:   []steer.Path{{Name: "carrier-a", Tunnel: "wg-a", Table: 101}, {Name: "carrier-b", Tunnel: "wg-b", Table: 102}},
		Classes: []steer.Class{
			{Name: "voice", Mark: 0x101, DSCP: []int{46}, SLA: &steer.SLA{MaxLatencyMS: fp(150), MaxJitterMS: fp(30), MaxLossPct: fp(1)}},
			{Name: "bulk", Mark: 0x103, DSCP: []int{8}, SLA: &steer.SLA{MaxLossPct: fp(5)}},
			{Name: "other", Mark: 0x104, DSCP: []int{0}},
		},
		Rules: []steer.Rule{
			{Class: "voice", Paths: []string{"carrier-b", "carrier-a"}},
			{Class: "bulk", Paths: []string{"carrier-b", "carrier-a"}, PauseIfNone: true},
			{Class: "other", Paths: []string{"carrier-b", "carrier-a"}},
		},
	}
	return &Agent{
		Log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
		Sys:      sys,
		Steerer:  &steer.Steerer{Sys: sys, StateDir: t.TempDir()},
		current:  &ds,
		steerMap: m,
		bfd:      map[string]bfdPeer{},
	}
}

func win(tunnel string, rtt, jitter, loss float64) probe.Window {
	recv := 10
	if loss >= 100 {
		recv = 0
	}
	return probe.Window{Path: tunnel, Sent: 10, Received: recv, RTTAvgMS: rtt, JitterMS: jitter, LossPct: loss}
}

// tick is one 10 s telemetry round: new windows, then the SLA re-steer.
func (a *Agent) tick(ctx context.Context, ws ...probe.Window) {
	fresh := map[string]probe.Window{}
	for _, w := range ws {
		fresh[w.Path] = w
	}
	a.mu.Lock()
	a.updateSLA(fresh)
	a.mu.Unlock()
	a.slaSteer(ctx)
}

func (a *Agent) pathOf(class string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, c := range a.choices {
		if c.Class == class {
			if c.Paused {
				return "paused"
			}
			return c.Path
		}
	}
	return ""
}

func TestSLAFailSafeOnlyWhileTheControllerIsSilent(t *testing.T) {
	ctx := context.Background()
	a := slaAgent(t)
	a.steer(ctx, "start")
	if a.pathOf("voice") != "carrier-b" {
		t.Fatalf("voice on %s", a.pathOf("voice"))
	}
	good := win("wg-a", 30, 2, 0)

	// The controller is heard: a breach is noted but the controller steers.
	a.tick(ctx, good, win("wg-b", 40, 3, 2.5))
	if a.pathOf("voice") != "carrier-b" {
		t.Fatalf("agent steered while the controller is heard: voice on %s", a.pathOf("voice"))
	}

	// The controller goes silent: voice leaves carrier-b at once (it was
	// already breaching); bulk's 5% limit is not breached, so it stays.
	a.mu.Lock()
	a.silent = true
	a.mu.Unlock()
	a.tick(ctx, good, win("wg-b", 40, 3, 2.5))
	if a.pathOf("voice") != "carrier-a" || a.pathOf("bulk") != "carrier-b" || a.pathOf("other") != "carrier-b" {
		t.Fatalf("silent breach: voice %s bulk %s other %s", a.pathOf("voice"), a.pathOf("bulk"), a.pathOf("other"))
	}
	found := false
	for _, e := range a.buf.Events {
		if e.Kind == "class_moved" && e.Detail["class"] == "voice" && e.Detail["to"] == "carrier-a" && e.Detail["why"] == "sla" && e.Detail["failover"] == "true" {
			found = true
		}
	}
	if !found {
		t.Fatalf("no class_moved event for the SLA demotion: %+v", a.buf.Events)
	}

	// Hysteresis: two good windows are not enough, the third restores it.
	a.tick(ctx, good, win("wg-b", 40, 3, 0))
	a.tick(ctx, good, win("wg-b", 40, 3, 0))
	if a.pathOf("voice") != "carrier-a" {
		t.Fatalf("restored after 2 good windows")
	}
	// A window with nothing sent is no news, and does not reset the count.
	a.tick(ctx, good, probe.Window{Path: "wg-b"})
	if a.pathOf("voice") != "carrier-a" {
		t.Fatal("restored on an empty window")
	}
	a.tick(ctx, good, win("wg-b", 40, 3, 0))
	if a.pathOf("voice") != "carrier-b" {
		t.Fatalf("not restored after %d good windows: voice on %s", SLARestoreWindows, a.pathOf("voice"))
	}

	// A breach in between restarts the count.
	a.tick(ctx, good, win("wg-b", 400, 3, 0))
	a.tick(ctx, good, win("wg-b", 40, 3, 0))
	a.tick(ctx, good, win("wg-b", 40, 3, 0))
	a.tick(ctx, good, win("wg-b", 400, 3, 0))
	a.tick(ctx, good, win("wg-b", 40, 3, 0))
	a.tick(ctx, good, win("wg-b", 40, 3, 0))
	if a.pathOf("voice") != "carrier-a" {
		t.Fatal("restored although the good windows were not in a row")
	}

	// The controller is back: its map applies again, demotions or not.
	a.mu.Lock()
	a.silent = false
	a.mu.Unlock()
	a.tick(ctx)
	if a.pathOf("voice") != "carrier-b" {
		t.Fatalf("controller back: voice on %s", a.pathOf("voice"))
	}
}

func TestSLAFailSafeNeverStrandsAClass(t *testing.T) {
	ctx := context.Background()
	a := slaAgent(t)
	a.silent = true
	a.steer(ctx, "start")

	// Both paths breach voice; carrier-a less badly (200 ms against 150, loss
	// 3% against 1% on carrier-b). Voice stays on the least bad path.
	a.tick(ctx, win("wg-a", 200, 5, 0), win("wg-b", 40, 3, 3))
	if a.pathOf("voice") != "carrier-a" {
		t.Fatalf("voice on %q, want the least bad carrier-a", a.pathOf("voice"))
	}
	// Both lose everything: bulk is not paused, it keeps a path.
	a.tick(ctx, win("wg-a", 0, 0, 100), win("wg-b", 0, 0, 100))
	if a.pathOf("bulk") == "paused" || a.pathOf("bulk") == "" || a.pathOf("voice") == "" {
		t.Fatalf("stranded: voice %q bulk %q", a.pathOf("voice"), a.pathOf("bulk"))
	}

	// BFD still wins: carrier-a down leaves only carrier-b, breaching or not.
	a.mu.Lock()
	a.bfd["100.64.1.1"] = bfdPeer{Tunnel: "wg-a", Status: "down"}
	a.mu.Unlock()
	a.steer(ctx, "bfd")
	if a.pathOf("voice") != "carrier-b" {
		t.Fatalf("voice on %q with carrier-a down", a.pathOf("voice"))
	}
}
