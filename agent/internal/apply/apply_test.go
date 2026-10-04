package apply

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

type fake struct {
	cmds    []string
	files   map[string]string
	perms   map[string]os.FileMode
	failOn  string // fail any command containing this
	links   string // json for ip -j link show type wireguard
	ipLinks string // json for ip -d -j link show
	loAddrs string // json for the labelled loopback addresses
	rules   string // json for ip -j rule show
	charon  bool   // strongSwan is running; `ipsec start` starts it
}

func (f *fake) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	c := name + " " + strings.Join(args, " ")
	f.cmds = append(f.cmds, c)
	if f.failOn != "" && strings.Contains(c, f.failOn) {
		return nil, fmt.Errorf("boom: %s", c)
	}
	switch {
	case strings.HasPrefix(c, "ip -j link show type wireguard"):
		return []byte(f.links), nil
	case c == "ip -d -j link show":
		return []byte(f.ipLinks), nil
	case strings.HasPrefix(c, "ip -4 -o addr show dev eth"):
		dev := strings.TrimPrefix(c, "ip -4 -o addr show dev eth")
		return []byte(fmt.Sprintf("3: eth%s    inet 10.1%s.1.2/24 brd 10.1%s.1.255 scope global eth%s\n", dev, dev, dev, dev)), nil
	case strings.HasPrefix(c, "ip -j -4 address show dev lo"):
		return []byte(f.loAddrs), nil
	case c == "ip -j rule show":
		return []byte(f.rules), nil
	case c == "swanctl --stats" && !f.charon:
		return nil, fmt.Errorf("swanctl: connecting to 'unix:///var/run/charon.vici' failed")
	case c == "ipsec start":
		f.charon = true
	}
	return nil, nil
}
func (f *fake) WriteFile(p string, d []byte, m os.FileMode) error {
	f.files[p] = string(d)
	if f.perms != nil {
		f.perms[p] = m
	}
	return nil
}
func (f *fake) Glob(pattern string) ([]string, error) {
	var out []string
	for p := range f.files {
		if ok, _ := filepath.Match(pattern, p); ok {
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out, nil
}
func (f *fake) Remove(p string) error { delete(f.files, p); return nil }
func (f *fake) Exists(p string) bool  { _, ok := f.files[p]; return ok }
func (f *fake) ReadFile(p string) ([]byte, error) {
	if d, ok := f.files[p]; ok {
		return []byte(d), nil
	}
	return nil, os.ErrNotExist
}

const key = "aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8gd28="

func state(v int64, tunnels ...string) *desired.State {
	s := &desired.State{Schema: 1, Version: v, NodeName: "site-a", Role: desired.RoleSite, ASN: 65001, RouterID: "100.64.1.11"}
	for i, n := range tunnels {
		s.Tunnels = append(s.Tunnels, desired.Tunnel{
			Name: n, Address: fmt.Sprintf("100.64.%d.11/24", i+1),
			Peers: []desired.Peer{{Name: "pop", PublicKey: key, AllowedIPs: []string{"0.0.0.0/0"}}},
		})
	}
	return s
}

func applier(f *fake) *Applier {
	return &Applier{Sys: f, StateDir: "/state", PrivateKey: "PRIV", FRRConf: "/frr.conf", Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
}

func TestApplySavesLastGood(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	if err := a.Apply(context.Background(), state(1, "wg-a", "wg-b")); err != nil {
		t.Fatal(err)
	}
	if lg := a.LastGood(); lg == nil || lg.Version != 1 {
		t.Fatal("last good not saved")
	}
	if !strings.Contains(f.files["/state/wg/wg-a.conf"], "PrivateKey = PRIV") {
		t.Fatal("wg config not written")
	}
	if !strings.Contains(f.files["/frr.conf"], "router bgp 65001") {
		t.Fatal("frr config not written")
	}
	joined := strings.Join(f.cmds, "\n")
	for _, want := range []string{"wg syncconf wg-a /state/wg/wg-a.conf", "ip address replace 100.64.2.11/24 dev wg-b", "vtysh -f /frr.conf"} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing command %q", want)
		}
	}
}

func TestApplyRemovesStaleTunnels(t *testing.T) {
	f := &fake{files: map[string]string{}, links: `[{"ifname":"wg-a"},{"ifname":"wg-old"}]`}
	if err := applier(f).Apply(context.Background(), state(1, "wg-a")); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(f.cmds, "\n"), "ip link del dev wg-old") {
		t.Fatal("stale tunnel not removed")
	}
}

func TestApplyRollsBack(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	if err := a.Apply(context.Background(), state(1, "wg-a")); err != nil {
		t.Fatal(err)
	}
	f.failOn = "wg-sat" // version 2 adds a tunnel that fails to configure
	err := a.Apply(context.Background(), state(2, "wg-a", "wg-sat"))
	if !IsRolledBack(err) {
		t.Fatalf("want rollback, got %v", err)
	}
	if lg := a.LastGood(); lg.Version != 1 {
		t.Fatalf("last good is version %d, want 1", lg.Version)
	}
	if !strings.Contains(f.files["/frr.conf"], "version 1") {
		t.Fatal("frr not restored to version 1")
	}
}

func TestApplyRejectsInvalid(t *testing.T) {
	f := &fake{files: map[string]string{}}
	s := state(1, "wg-a")
	s.Tunnels[0].Name = "eth0"
	if err := applier(f).Apply(context.Background(), s); err == nil || len(f.cmds) != 0 {
		t.Fatalf("invalid state should be rejected before any command, err=%v cmds=%v", err, f.cmds)
	}
}
