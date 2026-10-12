package console

import (
	"context"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

func TestManagedFrom(t *testing.T) {
	s, ts, c, _ := newTestServer(t)
	code, _ := SetupCode(s.Dir)
	if st, _ := call(t, c, ts.URL, "/api/claim", map[string]string{"code": code, "password": "a good long password"}); st != 200 {
		t.Fatal("claim")
	}
	// A new box is managed from both.
	if m := s.managed(); m.Mode != ManagedBoth || !m.At.IsZero() {
		t.Fatalf("default %+v", m)
	}
	// Not joined: it cannot be handed to the cloud yet.
	if st, _ := call(t, c, ts.URL, "/api/managed", map[string]string{"mode": "cloud"}); st != 400 {
		t.Fatalf("cloud before joining answered %d", st)
	}
	if st, _ := call(t, c, ts.URL, "/api/managed", map[string]string{"mode": "elsewhere"}); st != 400 {
		t.Fatal("unknown mode accepted")
	}
	if err := s.Join(context.Background(), JoinInfo{Name: "box-1", Token: "good-token-000"}); err != nil {
		t.Fatal(err)
	}
	if st, out := call(t, c, ts.URL, "/api/managed", map[string]string{"mode": "cloud"}); st != 200 {
		t.Fatalf("to cloud %d %v", st, out)
	}
	info := s.BoxInfo().(BoxInfo)
	if info.Managed.Mode != ManagedCloud || info.LastChange == nil || info.LastChange.What != "Set Managed from to Cloud" {
		t.Fatalf("box info %+v %+v", info.Managed, info.LastChange)
	}
	// Managed from the cloud: the console reads but does not change the ports.
	if st, _ := call(t, c, ts.URL, "/api/network", map[string]any{"wan": []map[string]any{{"iface": "eth0", "mode": "dhcp"}}}); st != 409 {
		t.Fatalf("port change in cloud mode answered %d", st)
	}
	if st, _ := call(t, c, ts.URL, "/api/network", nil); st != 200 {
		t.Fatal("reading the ports in cloud mode")
	}
	// From here only taking back control, and only with the password.
	if st, _ := call(t, c, ts.URL, "/api/managed", map[string]string{"mode": "both"}); st != 409 {
		t.Fatal("console switched cloud to both")
	}
	if st, _ := call(t, c, ts.URL, "/api/managed", map[string]string{"mode": "site", "password": "wrong password!"}); st != 403 {
		t.Fatal("took back control with a wrong password")
	}
	if st, out := call(t, c, ts.URL, "/api/managed", map[string]string{"mode": "site", "password": "a good long password"}); st != 200 {
		t.Fatalf("take back %d %v", st, out)
	}
	local := s.managed()
	if local.Mode != ManagedSite {
		t.Fatalf("after take back %+v", local)
	}

	// The portal's setting arrives with desired state: older than the box's is ignored...
	s.FromPortal(&desired.Box{ManagedFrom: &desired.ManagedFrom{Mode: "cloud", At: local.At.Add(-time.Minute), By: "dana@example.org"}})
	if s.managed().Mode != ManagedSite {
		t.Fatal("an older portal setting won")
	}
	// ...newer wins, and survives a restart.
	at := local.At.Add(time.Minute)
	s.FromPortal(&desired.Box{ManagedFrom: &desired.ManagedFrom{Mode: "cloud", At: at, By: "dana@example.org"}})
	if m := s.managed(); m.Mode != ManagedCloud || !m.At.Equal(at) || m.By != "dana@example.org" {
		t.Fatalf("newer portal setting not kept: %+v", m)
	}
	s.FromPortal(nil)
	s.FromPortal(&desired.Box{ManagedFrom: &desired.ManagedFrom{Mode: "nonsense", At: at.Add(time.Hour)}})
	again := &Server{Dir: s.Dir, Backend: &fakeBackend{}}
	if err := again.Init(); err != nil {
		t.Fatal(err)
	}
	if m := again.managed(); m.Mode != ManagedCloud || !m.At.Equal(at) {
		t.Fatalf("after restart %+v", m)
	}
	// A box clock behind the portal's still makes the console's change the newest.
	if st, _ := call(t, c, ts.URL, "/api/managed", map[string]string{"mode": "site", "password": "a good long password"}); st != 200 {
		t.Fatal("take back again")
	}
	if m := s.managed(); m.Mode != ManagedSite || !m.At.After(at) {
		t.Fatalf("clock skew %+v", m)
	}
}
