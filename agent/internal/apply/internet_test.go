package apply

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/render"
)

func inetState(v int64, mode string) *desired.State {
	s := state(v, "wg-a", "wg-b")
	s.Internet = &desired.Internet{Mode: mode, LANPrefixes: []string{"192.168.10.0/24"},
		Tunnels: []string{"wg-a", "wg-b"},
		Uplinks: []desired.Uplink{
			{Interface: "eth1", Gateway: "10.11.1.1", Path: "carrier-a", Tunnel: "wg-a"},
			{Interface: "eth2", Gateway: "10.12.1.1", Path: "carrier-b", Tunnel: "wg-b"},
		}}
	if mode == desired.InternetLocal {
		s.Internet.Firewall = []desired.FirewallRule{{ID: 12, Action: "deny", Dst: []string{"198.51.100.0/24"}, Protocol: "icmp"}}
	}
	return s
}

// What `ip -j rule show` prints once the rules for 192.168.10.0/24 are in.
const inetRules = `[{"priority":0,"src":"all","table":"local"},
{"priority":1000,"src":"all","fwmark":"0x101","table":"101"},
{"priority":31000,"src":"192.168.10.0","srclen":24,"table":"main","suppress_prefixlen":0},
{"priority":31001,"src":"192.168.10.0","srclen":24,"table":"251"},
{"priority":32766,"src":"all","table":"main"}]`

func since(f *fake, n int) []string { return append([]string{}, f.cmds[n:]...) }

func TestInternetLocalApply(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	s := inetState(1, desired.InternetLocal)
	if err := a.Apply(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds,
		"ip route replace default via 10.11.1.1 dev eth1 table 251",
		"ip rule add pref 31000 from 192.168.10.0/24 lookup main suppress_prefixlength 0",
		"ip rule add pref 31001 from 192.168.10.0/24 lookup 251",
		"nft -f /state/internet.nft")
	if got := f.files["/state/internet.nft"]; got != render.InternetNFT(s) {
		t.Fatalf("nft file:\n%s", got)
	}
	if a.InternetVia() != "eth1" {
		t.Fatalf("via %q", a.InternetVia())
	}

	// The same state again, with the rules in place: nothing to do.
	f.rules = inetRules
	n := len(f.cmds)
	if err := a.Apply(context.Background(), inetState(2, desired.InternetLocal)); err != nil {
		t.Fatal(err)
	}
	hasNone(t, since(f, n), "ip route", "ip rule", "nft")
}

func TestInternetSurvivesRestartWithoutResettingCounters(t *testing.T) {
	f := &fake{files: map[string]string{}}
	s := inetState(1, desired.InternetLocal)
	if err := applier(f).Apply(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	n := len(f.cmds)
	if err := applier(f).Apply(context.Background(), s); err != nil { // a new agent process
		t.Fatal(err)
	}
	hasNone(t, since(f, n), "nft -f")
	has(t, since(f, n), "nft list table ip exa_inet")
}

func TestInternetPopAndOffModes(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	if err := a.Apply(context.Background(), inetState(1, desired.InternetLocal)); err != nil {
		t.Fatal(err)
	}
	n := len(f.cmds)
	if err := a.Apply(context.Background(), inetState(2, desired.InternetPoP)); err != nil {
		t.Fatal(err)
	}
	has(t, since(f, n), "ip route replace default dev wg-a table 251", "nft delete table ip exa_inet")
	if a.InternetVia() != "wg-a" {
		t.Fatalf("via %q", a.InternetVia())
	}
	n = len(f.cmds)
	if err := a.Apply(context.Background(), inetState(3, desired.InternetOff)); err != nil {
		t.Fatal(err)
	}
	has(t, since(f, n), "ip route replace unreachable default table 251")
	hasNone(t, since(f, n), "nft") // already gone
	if a.InternetVia() != "" {
		t.Fatalf("via %q", a.InternetVia())
	}
}

func TestInternetPoPGateway(t *testing.T) {
	f := &fake{files: map[string]string{}}
	s := popState(1)
	s.LANPrefixes = []string{"192.168.0.0/24"}
	s.Internet = &desired.Internet{Mode: "gateway", LANPrefixes: []string{"192.168.0.0/24", "192.168.10.0/24"},
		Uplinks: []desired.Uplink{{Interface: "eth9", Gateway: "100.64.0.1"}}, PublicAddress: "100.64.0.2",
		PortForwards: []desired.PortForward{{ID: 3, Protocol: "tcp", Port: 8080, ToAddress: "192.168.10.10", ToPort: 8080}}}
	if err := applier(f).Apply(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds,
		"ip route replace default via 100.64.0.1 dev eth9 table 251",
		"ip rule add pref 31000 from 192.168.0.0/24 lookup main suppress_prefixlength 0",
		"ip rule add pref 31001 from 192.168.0.0/24 lookup 251",
		"ip rule add pref 31000 from 192.168.10.0/24 lookup main suppress_prefixlength 0",
		"ip rule add pref 31001 from 192.168.10.0/24 lookup 251",
		"nft -f /state/internet.nft")
}

func TestInternetRemovesStaleRules(t *testing.T) {
	f := &fake{files: map[string]string{}, rules: `[
{"priority":31000,"src":"192.168.10.0","srclen":24,"table":"main","suppress_prefixlen":0},
{"priority":31000,"src":"192.168.10.0","srclen":24,"table":"main","suppress_prefixlen":0},
{"priority":31000,"src":"192.168.99.0","srclen":24,"table":"main","suppress_prefixlen":0},
{"priority":31001,"src":"10.9.9.9","table":"251"},
{"priority":31001,"src":"192.168.10.0","srclen":24,"table":"251"}]`}
	a := applier(f)
	if err := a.Apply(context.Background(), inetState(1, desired.InternetPoP)); err != nil {
		t.Fatal(err)
	}
	var rules []string
	for _, c := range f.cmds {
		if len(c) > 7 && c[:7] == "ip rule" {
			rules = append(rules, c)
		}
	}
	want := []string{
		"ip rule del pref 31000 from 192.168.10.0/24 lookup main suppress_prefixlength 0", // the duplicate
		"ip rule del pref 31000 from 192.168.99.0/24 lookup main suppress_prefixlength 0",
		"ip rule del pref 31001 from 10.9.9.9/32 lookup 251",
	}
	if !reflect.DeepEqual(rules, want) {
		t.Fatalf("rule commands:\n%q\nwant\n%q", rules, want)
	}

	// The block goes: every rule at the internet preferences and the table go
	// too (pop mode had no nft table already).
	f.rules = inetRules
	n := len(f.cmds)
	if err := a.Apply(context.Background(), state(2, "wg-a", "wg-b")); err != nil {
		t.Fatal(err)
	}
	got := since(f, n)
	has(t, got,
		"ip rule del pref 31000 from 192.168.10.0/24 lookup main suppress_prefixlength 0",
		"ip rule del pref 31001 from 192.168.10.0/24 lookup 251",
		"ip route flush table 251")
	hasNone(t, got, "ip rule del pref 1000", "ip rule add", "nft")
	if a.InternetVia() != "" {
		t.Fatalf("via %q", a.InternetVia())
	}
	// Once flushed, not again.
	n = len(f.cmds)
	if err := a.Apply(context.Background(), state(3, "wg-a", "wg-b")); err != nil {
		t.Fatal(err)
	}
	hasNone(t, since(f, n), "ip route flush", "nft")
}

func TestInternetExitFollowsBFD(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	up := map[string]bool{"wg-a": true, "wg-b": true}
	a.TunnelUp = func(t string) bool { return up[t] }
	if err := a.Apply(context.Background(), inetState(1, desired.InternetLocal)); err != nil {
		t.Fatal(err)
	}
	if a.InternetVia() != "eth1" {
		t.Fatalf("via %q", a.InternetVia())
	}

	up["wg-a"] = false // carrier A failing: out of eth2
	n := len(f.cmds)
	from, to, changed, err := a.Reroute(context.Background())
	if err != nil || !changed || from != "eth1" || to != "eth2" {
		t.Fatalf("reroute: %q -> %q changed=%v err=%v", from, to, changed, err)
	}
	if got := since(f, n); !reflect.DeepEqual(got, []string{"ip route replace default via 10.12.1.1 dev eth2 table 251"}) {
		t.Fatalf("commands %q", got)
	}

	n = len(f.cmds) // no change: no command
	if _, _, changed, _ := a.Reroute(context.Background()); changed || len(f.cmds) != n {
		t.Fatalf("rewrote an unchanged exit: %q", since(f, n))
	}

	up["wg-b"] = false // both failing: the first
	if _, to, _, _ := a.Reroute(context.Background()); to != "eth1" {
		t.Fatalf("both down: via %q", to)
	}
	up["wg-a"], up["wg-b"] = true, true
	if _, to, changed, _ := a.Reroute(context.Background()); to != "eth1" || changed {
		t.Fatalf("back up: via %q changed=%v", to, changed)
	}
}

func TestInternetExitFallsBackWhenRefused(t *testing.T) {
	f := &fake{files: map[string]string{}, failOn: "dev eth1 table 251"}
	a := applier(f)
	if err := a.Apply(context.Background(), inetState(1, desired.InternetLocal)); err != nil {
		t.Fatalf("a refused route must not fail the apply: %v", err)
	}
	if a.InternetVia() != "eth2" {
		t.Fatalf("via %q", a.InternetVia())
	}
	n := len(f.cmds)
	if _, _, changed, _ := a.Reroute(context.Background()); changed || len(f.cmds) != n {
		t.Fatal("a BFD check with nothing changed should not retry")
	}
	f.failOn = ""
	if err := a.RetryInternet(context.Background()); err != nil {
		t.Fatal(err)
	}
	if a.InternetVia() != "eth1" {
		t.Fatalf("after retry via %q", a.InternetVia())
	}
	// In place now: a retry only looks. The fake prints no routes, as if the
	// kernel dropped it, so it is written again.
	n = len(f.cmds)
	if err := a.RetryInternet(context.Background()); err != nil {
		t.Fatal(err)
	}
	has(t, since(f, n), "ip route show table 251", "ip route replace default via 10.11.1.1 dev eth1 table 251")
}

func TestInternetExits(t *testing.T) {
	routes := func(e []Exit) []string {
		var out []string
		for _, x := range e {
			out = append(out, x.Route)
		}
		return out
	}
	s := inetState(1, desired.InternetPoP)
	down := func(t string) bool { return t != "wg-a" }
	if got := routes(InternetExits(s, down)); !reflect.DeepEqual(got, []string{"default dev wg-b", "default dev wg-a", "unreachable default"}) {
		t.Fatalf("pop: %q", got)
	}
	if got := InternetExits(s, nil)[0]; got.Via != "wg-a" {
		t.Fatalf("pop, BFD unknown: %+v", got)
	}
	s.Internet.Tunnels = nil
	if got := routes(InternetExits(s, nil)); !reflect.DeepEqual(got, []string{"unreachable default"}) {
		t.Fatalf("pop without tunnels: %q", got)
	}
	s = inetState(1, desired.InternetLocal)
	s.Internet.Uplinks[1].Tunnel = "" // no tunnel to judge it by: counts as working
	if got := InternetExits(s, func(string) bool { return false }); got[0].Via != "eth2" {
		t.Fatalf("local: %+v", got)
	}
	if got := routes(InternetExits(inetState(1, desired.InternetOff), nil)); !reflect.DeepEqual(got, []string{"unreachable default"}) {
		t.Fatalf("off: %q", got)
	}
	if InternetExits(state(1, "wg-a"), nil) != nil {
		t.Fatal("no block, no exits")
	}
}

// errSys fails the listed commands with the given messages.
type errSys struct {
	*fake
	errs map[string]string
}

func (e errSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	out, err := e.fake.Run(ctx, name, args...)
	if msg, ok := e.errs[name+" "+strings.Join(args, " ")]; ok {
		return nil, errors.New(msg)
	}
	return out, err
}

func TestInternetAbsentOnFreshNode(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	a.Sys = errSys{f, map[string]string{
		"ip route flush table 251":     "ip route flush table 251: exit status 2: Error: ipv4: FIB table does not exist.\nFlush terminated",
		"nft delete table ip exa_inet": "nft delete table ip exa_inet: exit status 1: Error: Could not process rule: No such file or directory",
	}}
	if err := a.Apply(context.Background(), state(1, "wg-a")); err != nil {
		t.Fatalf("nothing to remove is not an error: %v", err)
	}
	has(t, f.cmds, "ip route flush table 251", "nft delete table ip exa_inet")
}

func TestInternetRollsBack(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	if err := a.Apply(context.Background(), inetState(1, desired.InternetPoP)); err != nil {
		t.Fatal(err)
	}
	f.failOn = "nft -f" // version 2 breaks out locally, and nft refuses the table
	n := len(f.cmds)
	err := a.Apply(context.Background(), inetState(2, desired.InternetLocal))
	if !IsRolledBack(err) {
		t.Fatalf("want rollback, got %v", err)
	}
	if a.InternetVia() != "wg-a" {
		t.Fatalf("via %q after rollback", a.InternetVia())
	}
	got := since(f, n)
	has(t, got, "ip route replace default via 10.11.1.1 dev eth1 table 251", "nft -f /state/internet.nft",
		"ip route replace default dev wg-a table 251", "nft delete table ip exa_inet")
}
