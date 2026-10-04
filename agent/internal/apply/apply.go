// Package apply makes the node match a desired state: the loopback, WireGuard
// interfaces and peers, cloud (IPsec) and layer 2 (VXLAN) circuits, internet
// breakout, then FRR.
// It is idempotent, keeps the last good state on disk, and
// rolls back to it if applying a new version fails.
package apply

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/render"
	"github.com/zagias/exaconnect/agent/internal/system"
)

const (
	FRRConf   = "/etc/frr/frr.conf"
	FRRReload = "/usr/lib/frr/frr-reload.py"
)

type Applier struct {
	Sys        system.Runner
	StateDir   string
	PrivateKey string
	Log        *slog.Logger
	FRRConf    string // defaults to FRRConf
	// SwanctlDir and StrongSwanConf default to the constants of the same names.
	SwanctlDir     string
	StrongSwanConf string
	// Sleep waits while strongSwan starts; nil uses time.Sleep (tests stub it).
	Sleep func(time.Duration)

	// TunnelUp reports whether BFD says a tunnel works, for choosing the
	// internet exit; nil counts every tunnel up.
	TunnelUp func(tunnel string) bool

	shaped   map[string]string // circuit interface -> shaping last applied
	shapeErr map[string]string // circuit interface -> last shaping error logged

	inetMu       sync.Mutex
	inetState    *desired.State // the state whose internet block was applied last
	inetWant     string         // the preferred internet route last written or tried
	inetOK       bool           // inetWant is in place
	inetVia      string
	inetFlushed  bool   // the internet table was flushed and nothing written since
	inetNFT      string // the NAT and firewall table last applied
	inetNFTKnown bool   // inetNFT is what the host has
}

func (a *Applier) lastGoodPath() string { return filepath.Join(a.StateDir, "last-good.json") }

// LastGood returns the last state that applied cleanly, or nil.
func (a *Applier) LastGood() *desired.State {
	b, err := a.Sys.ReadFile(a.lastGoodPath())
	if err != nil {
		return nil
	}
	var s desired.State
	if json.Unmarshal(b, &s) != nil || s.Validate() != nil {
		return nil
	}
	return &s
}

// Apply applies s. On failure it re-applies the last good state (if any and
// different) and returns an error saying so.
func (a *Applier) Apply(ctx context.Context, s *desired.State) error {
	if err := s.Validate(); err != nil {
		return fmt.Errorf("invalid desired state: %w", err)
	}
	err := a.apply(ctx, s)
	if err == nil {
		b, _ := json.MarshalIndent(s, "", "  ")
		return a.Sys.WriteFile(a.lastGoodPath(), b, 0o600)
	}
	prev := a.LastGood()
	if prev == nil || prev.Version == s.Version {
		return fmt.Errorf("apply version %d: %w", s.Version, err)
	}
	a.Log.Warn("apply failed, rolling back", "version", s.Version, "to", prev.Version, "err", err)
	if rbErr := a.apply(ctx, prev); rbErr != nil {
		return fmt.Errorf("apply version %d: %w; rollback to %d also failed: %v", s.Version, err, prev.Version, rbErr)
	}
	return &RolledBack{Version: s.Version, To: prev.Version, Err: err}
}

// RolledBack reports a failed apply that was undone.
type RolledBack struct {
	Version, To int64
	Err         error
}

func (r *RolledBack) Error() string {
	return fmt.Sprintf("apply version %d failed (%v); rolled back to version %d", r.Version, r.Err, r.To)
}
func (r *RolledBack) Unwrap() error { return r.Err }

func IsRolledBack(err error) bool { var r *RolledBack; return errors.As(err, &r) }

func (a *Applier) apply(ctx context.Context, s *desired.State) error {
	if err := a.loopback(ctx, s); err != nil {
		return fmt.Errorf("loopback: %w", err)
	}
	want := map[string]bool{}
	for _, t := range s.Tunnels {
		want[t.Name] = true
		if err := a.tunnel(ctx, t); err != nil {
			return fmt.Errorf("%s: %w", t.Name, err)
		}
	}
	if err := a.removeStale(ctx, want); err != nil {
		return err
	}
	if err := a.circuits(ctx, s); err != nil {
		return fmt.Errorf("circuits: %w", err)
	}
	if err := a.internet(ctx, s); err != nil {
		return fmt.Errorf("internet: %w", err)
	}
	return a.frr(ctx, s)
}

func (a *Applier) tunnel(ctx context.Context, t desired.Tunnel) error {
	conf := filepath.Join(a.StateDir, "wg", t.Name+".conf")
	if err := a.Sys.WriteFile(conf, []byte(render.WireGuard(t, a.PrivateKey)), 0o600); err != nil {
		return err
	}
	if _, err := a.Sys.Run(ctx, "ip", "link", "show", "dev", t.Name); err != nil {
		if _, err := a.Sys.Run(ctx, "ip", "link", "add", "dev", t.Name, "type", "wireguard"); err != nil {
			return err
		}
	}
	steps := [][]string{
		{"wg", "syncconf", t.Name, conf},
		{"ip", "address", "replace", t.Address, "dev", t.Name},
	}
	mtu := t.MTU
	if mtu == 0 {
		mtu = 1420
	}
	steps = append(steps, []string{"ip", "link", "set", "dev", t.Name, "mtu", strconv.Itoa(mtu), "up"})
	for _, st := range steps {
		if _, err := a.Sys.Run(ctx, st[0], st[1:]...); err != nil {
			return err
		}
	}
	return nil
}

// removeStale deletes wg-* interfaces the desired state no longer lists.
func (a *Applier) removeStale(ctx context.Context, want map[string]bool) error {
	out, err := a.Sys.Run(ctx, "ip", "-j", "link", "show", "type", "wireguard")
	if err != nil {
		return err
	}
	var links []struct {
		Name string `json:"ifname"`
	}
	if len(out) > 0 {
		if err := json.Unmarshal(out, &links); err != nil {
			return fmt.Errorf("parse ip link: %w", err)
		}
	}
	for _, l := range links {
		if !want[l.Name] && len(l.Name) > 3 && l.Name[:3] == "wg-" {
			if _, err := a.Sys.Run(ctx, "ip", "link", "del", "dev", l.Name); err != nil {
				return err
			}
		}
	}
	return nil
}

func (a *Applier) frr(ctx context.Context, s *desired.State) error {
	path := a.FRRConf
	if path == "" {
		path = FRRConf
	}
	if err := a.Sys.WriteFile(path, []byte(render.FRR(s)), 0o640); err != nil {
		return err
	}
	if a.Sys.Exists(FRRReload) {
		_, err := a.Sys.Run(ctx, "python3", FRRReload, "--reload", path)
		return err
	}
	// Without frr-reload.py, vtysh can only add configuration, not remove it.
	_, err := a.Sys.Run(ctx, "vtysh", "-f", path)
	return err
}
