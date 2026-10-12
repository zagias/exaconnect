package console

import (
	"context"
	"encoding/json"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

func portalNet(t *testing.T, at time.Time, n map[string]any) *desired.Box {
	t.Helper()
	b, _ := json.Marshal(n)
	return &desired.Box{Network: &desired.BoxNetwork{Network: b, At: at, By: "dana@example.org"}}
}

func waitPortal(t *testing.T, s *Server, state string) PortalChange {
	t.Helper()
	for i := 0; i < 200; i++ {
		if p := s.portalState(); p.State == state {
			return p
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("portal change never %s: %+v", state, s.portalState())
	return PortalChange{}
}

func TestPortsFromPortal(t *testing.T) {
	s, _, _, fb := newTestServer(t)
	ports, _ := Ports("")
	if len(ports) == 0 {
		t.Skip("no ports to set up in this sandbox")
	}
	iface := ports[0].Name
	var reachable atomic.Bool
	reachable.Store(true)
	s.ReachEvery = 20 * time.Millisecond
	s.Reach = func(context.Context) error {
		if reachable.Load() {
			return nil
		}
		return errors.New("no route")
	}
	if err := s.Join(context.Background(), JoinInfo{Name: "box-1", Token: "good-token-000"}); err != nil {
		t.Fatal(err)
	}
	t0 := time.Now().UTC().Truncate(time.Microsecond)

	// Managed on site: the portal's ports are not taken.
	if _, err := s.setManaged(Managed{Mode: ManagedSite, At: t0}); err != nil {
		t.Fatal(err)
	}
	s.FromPortal(portalNet(t, t0.Add(time.Second), map[string]any{"wan": []map[string]any{{"iface": iface, "mode": "dhcp"}}}))
	time.Sleep(50 * time.Millisecond)
	if len(fb.list()) != 0 || !s.portalState().At.IsZero() {
		t.Fatal("ports from the portal taken while managed on site")
	}

	// Switched to both: a portal set-up from before the switch is not taken
	// (switching keeps the box's set-up)...
	if _, err := s.setManaged(Managed{Mode: ManagedBoth, At: t0.Add(2 * time.Second)}); err != nil {
		t.Fatal(err)
	}
	s.FromPortal(portalNet(t, t0.Add(time.Second), map[string]any{"wan": []map[string]any{{"iface": iface, "mode": "dhcp"}}}))
	time.Sleep(50 * time.Millisecond)
	if len(fb.list()) != 0 {
		t.Fatal("a portal set-up older than the switch was taken")
	}

	// ...a newer one is, and kept once ExaCarib can be reached.
	at := t0.Add(3 * time.Second)
	s.FromPortal(portalNet(t, at, map[string]any{"wan": []map[string]any{{"iface": iface, "mode": "dhcp"}}}))
	p := waitPortal(t, s, "kept")
	if !p.At.Equal(at) || len(fb.list()) != 1 {
		t.Fatalf("kept %+v, applied %d", p, len(fb.list()))
	}
	info := s.BoxInfo().(BoxInfo)
	if info.NetworkAt == nil || !info.NetworkAt.Equal(at) || info.Portal == nil || info.Portal.State != "kept" ||
		info.LastChange == nil || info.LastChange.By != "dana@example.org" || len(info.Ports) == 0 {
		t.Fatalf("box info %+v", info)
	}
	// The same set-up again (a new desired state) changes nothing.
	s.FromPortal(portalNet(t, at, map[string]any{"wan": []map[string]any{{"iface": iface, "mode": "dhcp"}}}))
	time.Sleep(50 * time.Millisecond)
	if len(fb.list()) != 1 {
		t.Fatal("the same portal set-up was applied twice")
	}

	// A set-up the box cannot use is refused, and the ports stay.
	s.FromPortal(portalNet(t, at.Add(time.Second), map[string]any{"wan": []map[string]any{{"iface": "nosuchport0", "mode": "dhcp"}}}))
	if p := waitPortal(t, s, "refused"); p.Detail == "" {
		t.Fatal("refused without a reason")
	}
	if len(fb.list()) != 1 {
		t.Fatal("a refused set-up was applied")
	}

	// A change that cuts the box off from ExaCarib is put back.
	reachable.Store(false)
	cut := at.Add(2 * time.Second)
	s.FromPortal(portalNet(t, cut, map[string]any{"wan": []map[string]any{{"iface": iface, "mode": "static", "address": "10.9.9.2/24", "gateway": "10.9.9.1"}}}))
	waitPortal(t, s, "undone")
	applied := fb.list()
	if len(applied) != 3 || applied[2].WAN[0].Mode != "dhcp" {
		t.Fatalf("not put back: %+v", applied)
	}
	s.mu.Lock()
	back := s.net.WAN[0].Mode
	s.mu.Unlock()
	if back != "dhcp" {
		t.Fatalf("box kept %s", back)
	}
	// ...and not tried again, even after a restart.
	again := &Server{Dir: s.Dir, Backend: fb, Reach: s.Reach}
	if err := again.Init(); err != nil {
		t.Fatal(err)
	}
	again.FromPortal(portalNet(t, cut, map[string]any{"wan": []map[string]any{{"iface": iface, "mode": "static", "address": "10.9.9.2/24", "gateway": "10.9.9.1"}}}))
	time.Sleep(50 * time.Millisecond)
	if len(fb.list()) != 3 || again.portalState().State != "undone" {
		t.Fatal("an undone portal set-up was tried again")
	}
}
