package apply

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/render"
)

// Internet breakout (docs/internet-contract.md): LAN traffic with no more
// specific route than a default goes to InternetTable, which points at the
// chosen exit.
const (
	InternetTable     = 251
	InternetPrefMain  = 31000
	InternetPrefTable = 31001
)

// Exit is one way out for internet traffic: the route in InternetTable and
// the interface or tunnel it points at ("" for unreachable).
type Exit struct {
	Route string
	Via   string
}

var unreachable = Exit{Route: "unreachable default"}

// InternetExits lists the exits in the order to try them. The first is the
// contract's choice: the first tunnel (pop mode) or uplink (local mode)
// whose tunnel is up, else the first listed. The others follow in case the
// kernel refuses it, and unreachable comes last so the table never keeps a
// stale exit. up reports a tunnel's BFD health; nil counts every tunnel up.
// The result is nil without an internet block.
func InternetExits(s *desired.State, up func(tunnel string) bool) []Exit {
	in := s.Internet
	if in == nil {
		return nil
	}
	if up == nil {
		up = func(string) bool { return true }
	}
	var good, bad []Exit
	add := func(e Exit, ok bool) {
		if ok {
			good = append(good, e)
		} else {
			bad = append(bad, e)
		}
	}
	switch {
	case s.Role == desired.RolePoP:
		for _, u := range in.Uplinks {
			add(Exit{Route: "default via " + u.Gateway + " dev " + u.Interface, Via: u.Interface}, true)
		}
	case in.Mode == desired.InternetPoP:
		for _, t := range in.Tunnels {
			add(Exit{Route: "default dev " + t, Via: t}, up(t))
		}
	case in.Mode == desired.InternetLocal:
		for _, u := range in.Uplinks {
			add(Exit{Route: "default via " + u.Gateway + " dev " + u.Interface, Via: u.Interface}, u.Tunnel == "" || up(u.Tunnel))
		}
	}
	out := append(good, bad...)
	return append(out, unreachable)
}

// internet applies the internet block of s: the internet table and its ip
// rules, then the NAT and firewall table. A refused route does not fail the
// apply (the next exit is used, and the preferred one is retried later).
func (a *Applier) internet(ctx context.Context, s *desired.State) error {
	a.inetMu.Lock()
	a.inetState = s
	a.inetMu.Unlock()
	rules := render.InternetRules(s.Internet, InternetPrefMain, InternetPrefTable, InternetTable)
	if s.Internet != nil {
		// The table first, so the rules never send traffic to an empty one.
		if _, err := a.route(ctx, false); err != nil {
			a.Log.Warn("internet route", "err", err)
		}
		if err := a.internetRules(ctx, rules); err != nil {
			return fmt.Errorf("rules: %w", err)
		}
	} else {
		if err := a.internetRules(ctx, nil); err != nil {
			return fmt.Errorf("rules: %w", err)
		}
		if _, err := a.route(ctx, false); err != nil {
			return err
		}
	}
	return a.internetNFT(ctx, render.InternetNFT(s))
}

// Reroute re-chooses the exit of the last applied internet block, for a BFD
// change. It rewrites the internet table only when the choice changed, and
// reports the exit before and after when it did.
func (a *Applier) Reroute(ctx context.Context) (from, to string, changed bool, err error) {
	before := a.InternetVia()
	changed, err = a.route(ctx, false)
	return before, a.InternetVia(), changed, err
}

// RetryInternet writes the preferred exit again when writing it failed last
// time, or when its route has gone (the kernel drops routes through an
// interface that goes down).
func (a *Applier) RetryInternet(ctx context.Context) error {
	a.inetMu.Lock()
	want, ok := a.inetWant, a.inetOK
	a.inetMu.Unlock()
	if want == "" {
		return nil
	}
	if ok {
		out, err := a.Sys.Run(ctx, "ip", "route", "show", "table", strconv.Itoa(InternetTable))
		gone := err == nil && !strings.Contains(string(out), "default") ||
			err != nil && strings.Contains(err.Error(), "does not exist")
		if !gone {
			return nil
		}
	}
	_, err := a.route(ctx, true)
	return err
}

// InternetVia is the interface or tunnel the internet table points at, or "".
func (a *Applier) InternetVia() string {
	a.inetMu.Lock()
	defer a.inetMu.Unlock()
	return a.inetVia
}

func (a *Applier) route(ctx context.Context, force bool) (changed bool, err error) {
	a.inetMu.Lock()
	s := a.inetState
	want, flushed := a.inetWant, a.inetFlushed
	a.inetMu.Unlock()
	if s == nil {
		return false, nil
	}
	exits := InternetExits(s, a.TunnelUp) // outside the lock: TunnelUp takes the agent's
	if len(exits) == 0 {
		if flushed {
			return false, nil
		}
		// A table never used does not exist; that is as good as flushed.
		if err := a.run(ctx, "ip", "route", "flush", "table", strconv.Itoa(InternetTable)); err != nil && !strings.Contains(err.Error(), "does not exist") {
			return false, err
		}
		a.setRoute("", false, "", true)
		return want != "", nil
	}
	if !force && exits[0].Route == want {
		return false, nil // in place, or refused and waiting for RetryInternet
	}
	var errs []string
	for i, e := range exits {
		args := append([]string{"ip", "route", "replace"}, strings.Fields(e.Route)...)
		if err := a.run(ctx, append(args, "table", strconv.Itoa(InternetTable))...); err != nil {
			errs = append(errs, err.Error())
			continue
		}
		a.setRoute(exits[0].Route, i == 0, e.Via, false)
		if len(errs) > 0 {
			return true, fmt.Errorf("preferred internet exit refused, using %q: %s", e.Route, strings.Join(errs, "; "))
		}
		return true, nil
	}
	a.setRoute(exits[0].Route, false, "", false)
	return true, fmt.Errorf("no internet exit could be set: %s", strings.Join(errs, "; "))
}

func (a *Applier) setRoute(want string, ok bool, via string, flushed bool) {
	a.inetMu.Lock()
	a.inetWant, a.inetOK, a.inetVia, a.inetFlushed = want, ok, via, flushed
	a.inetMu.Unlock()
}

// internetRules makes the rules at the internet preferences exactly want,
// adding before deleting so LAN traffic is never without them.
func (a *Applier) internetRules(ctx context.Context, want []string) error {
	have, err := a.internetRuleLines(ctx)
	if err != nil {
		return err
	}
	wanted, count := map[string]bool{}, map[string]int{}
	for _, w := range want {
		wanted[w] = true
	}
	for _, h := range have {
		count[h]++
	}
	for _, w := range want {
		if count[w] == 0 {
			if err := a.run(ctx, append([]string{"ip", "rule", "add"}, strings.Fields(w)...)...); err != nil {
				return err
			}
		}
	}
	for _, h := range have {
		if wanted[h] && count[h] == 1 {
			continue
		}
		count[h]--
		if err := a.run(ctx, append([]string{"ip", "rule", "del"}, strings.Fields(h)...)...); err != nil {
			return err
		}
	}
	return nil
}

// internetRuleLines reads the rules at the internet preferences in the form
// render.InternetRules writes them.
func (a *Applier) internetRuleLines(ctx context.Context) ([]string, error) {
	out, err := a.Sys.Run(ctx, "ip", "-j", "rule", "show")
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Priority int    `json:"priority"`
		Src      string `json:"src"`
		SrcLen   *int   `json:"srclen"`
		Table    string `json:"table"`
		Suppress *int   `json:"suppress_prefixlen"`
	}
	if len(strings.TrimSpace(string(out))) > 0 {
		if err := json.Unmarshal(out, &rows); err != nil {
			return nil, fmt.Errorf("parse ip rules: %w", err)
		}
	}
	var lines []string
	for _, r := range rows {
		if r.Priority != InternetPrefMain && r.Priority != InternetPrefTable {
			continue
		}
		src := r.Src
		switch {
		case src == "" || src == "all":
			src = "0.0.0.0/0"
		case r.SrcLen != nil:
			src += "/" + strconv.Itoa(*r.SrcLen)
		default:
			src += "/32"
		}
		line := fmt.Sprintf("pref %d from %s lookup %s", r.Priority, src, r.Table)
		if r.Suppress != nil {
			line += " suppress_prefixlength " + strconv.Itoa(*r.Suppress)
		}
		lines = append(lines, line)
	}
	return lines, nil
}

// internetNFT replaces the NAT and firewall table with text in one `nft -f`
// run, or deletes it when text is "". Unchanged text is not re-applied, which
// would reset the counters, even across an agent restart.
func (a *Applier) internetNFT(ctx context.Context, text string) error {
	if a.inetNFTKnown && text == a.inetNFT {
		return nil
	}
	path := filepath.Join(a.StateDir, "internet.nft")
	if text == "" {
		_, err := a.Sys.Run(ctx, "nft", "delete", "table", "ip", render.InternetNFTable)
		// No table, or no nft at all (so no table either), is the goal.
		if err != nil && !strings.Contains(err.Error(), "No such file or directory") && !errors.Is(err, exec.ErrNotFound) {
			return err
		}
		a.inetNFT, a.inetNFTKnown = "", true
		return nil
	}
	if !a.inetNFTKnown {
		if old, err := a.Sys.ReadFile(path); err == nil && string(old) == text {
			if _, err := a.Sys.Run(ctx, "nft", "list", "table", "ip", render.InternetNFTable); err == nil {
				a.inetNFT, a.inetNFTKnown = text, true
				return nil
			}
		}
	}
	if err := a.Sys.WriteFile(path, []byte(text), 0o600); err != nil {
		return err
	}
	if err := a.run(ctx, "nft", "-f", path); err != nil {
		a.inetNFTKnown = false
		return err
	}
	a.inetNFT, a.inetNFTKnown = text, true
	return nil
}
