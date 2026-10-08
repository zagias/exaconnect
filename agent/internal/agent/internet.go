package agent

import (
	"context"
	"encoding/json"
	"net/netip"
	"sort"
	"strconv"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/render"
)

// MaxAutoBlocked is how many automatically blocked sources a flush reports.
const MaxAutoBlocked = 100

// InternetState is where internet traffic leaves this node, the firewall's
// counters (docs/internet-contract.md), and on a protected PoP the sources
// blocked automatically (docs/protection-contract.md).
type InternetState struct {
	Mode        string            `json:"mode"`
	Via         string            `json:"via"`
	Counters    []InternetCounter `json:"counters"`
	AutoBlocked []AutoBlocked     `json:"auto_blocked"`
}

// InternetCounter is one cumulative nft counter: a firewall rule, a port
// forward, or (id 0) the inbound drop or one of the protection drops.
type InternetCounter struct {
	Kind    string `json:"kind"` // rule | forward | inbound | blocked | auto | flood | syn
	ID      int    `json:"id"`
	Packets uint64 `json:"packets"`
	Bytes   uint64 `json:"bytes"`
}

// AutoBlocked is one source in the PoP's automatic block set.
type AutoBlocked struct {
	Address  string `json:"address"`
	ExpiresS int    `json:"expires_s"` // seconds until the block lapses
}

// tunnelUp is the BFD health of a tunnel for choosing the internet exit,
// judged as for steering: unknown counts as up.
func (a *Agent) tunnelUp(tunnel string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	st := tunnelBFD(a.bfd, tunnel)
	return st == "" || st == "up" || st == "init"
}

// reroute re-chooses the internet exit after a BFD change, without waiting
// for the controller.
func (a *Agent) reroute(ctx context.Context) {
	from, to, changed, err := a.Applier.Reroute(ctx)
	if err != nil {
		if msg := err.Error(); msg != a.inetErr {
			a.inetErr = msg
			a.Log.Warn("internet exit", "err", err)
		}
	} else {
		a.inetErr = ""
	}
	if changed && from != to {
		a.Log.Info("internet exit changed", "from", from, "to", to)
		a.event("internet_exit_changed", map[string]string{"from": from, "to": to})
	}
}

// internetState reads the internet exit and, where this node filters, the
// nft counters, for one flush.
func (a *Agent) internetState(ctx context.Context, s *desired.State) *InternetState {
	if s == nil || s.Internet == nil {
		return nil
	}
	st := &InternetState{Mode: s.Internet.Mode, Via: a.Applier.InternetVia(), Counters: []InternetCounter{},
		AutoBlocked: []AutoBlocked{}}
	if s.Firewalled() {
		if out, err := a.Sys.Run(ctx, "nft", "-j", "list", "table", "ip", render.InternetNFTable); err == nil {
			st.Counters = parseInternetCounters(out)
		}
	}
	if s.Protected() {
		if out, err := a.Sys.Run(ctx, "nft", "-j", "list", "set", "ip", render.InternetNFTable, render.AutoBlockSet); err == nil {
			st.AutoBlocked = parseAutoBlocked(out, MaxAutoBlocked)
		}
	}
	return st
}

// protectionKinds are the protection drops, each counted under its comment.
var protectionKinds = map[string]bool{
	render.CommentBlocked: true, render.CommentAuto: true, render.CommentFlood: true, render.CommentSYN: true,
}

// parseInternetCounters picks the counters of the rules the agent named
// (fw<id>, pf<id>, inbound and the protection drops) out of `nft -j list table`.
func parseInternetCounters(b []byte) []InternetCounter {
	var doc struct {
		Nftables []struct {
			Rule *struct {
				Comment string            `json:"comment"`
				Expr    []json.RawMessage `json:"expr"`
			} `json:"rule"`
		} `json:"nftables"`
	}
	out := []InternetCounter{}
	if json.Unmarshal(b, &doc) != nil {
		return out
	}
	for _, item := range doc.Nftables {
		if item.Rule == nil {
			continue
		}
		c := InternetCounter{}
		switch cm := item.Rule.Comment; {
		case cm == render.CommentInbound || protectionKinds[cm]:
			c.Kind = cm
		case strings.HasPrefix(cm, render.CommentRule):
			c.Kind, c.ID = "rule", idOf(cm, render.CommentRule)
		case strings.HasPrefix(cm, render.CommentForward):
			c.Kind, c.ID = "forward", idOf(cm, render.CommentForward)
		}
		if c.Kind == "" || ((c.Kind == "rule" || c.Kind == "forward") && c.ID < 1) {
			continue
		}
		for _, e := range item.Rule.Expr {
			var x struct {
				Counter *struct {
					Packets uint64 `json:"packets"`
					Bytes   uint64 `json:"bytes"`
				} `json:"counter"`
			}
			if json.Unmarshal(e, &x) == nil && x.Counter != nil {
				c.Packets, c.Bytes = x.Counter.Packets, x.Counter.Bytes
				out = append(out, c)
				break
			}
		}
	}
	return out
}

// parseAutoBlocked reads the elements of `nft -j list set` with the seconds
// each has left, the blocks with longest to run (the newest) first, at most
// max. Elements are {"elem": {"val", "expires", ...}} in a set with timeouts,
// and plain strings in one without.
func parseAutoBlocked(b []byte, max int) []AutoBlocked {
	var doc struct {
		Nftables []struct {
			Set *struct {
				Elem []json.RawMessage `json:"elem"`
			} `json:"set"`
		} `json:"nftables"`
	}
	out := []AutoBlocked{}
	if json.Unmarshal(b, &doc) != nil {
		return out
	}
	for _, item := range doc.Nftables {
		if item.Set == nil {
			continue
		}
		for _, raw := range item.Set.Elem {
			var addr string
			var e struct {
				Elem *struct {
					Val     json.RawMessage `json:"val"`
					Expires int             `json:"expires"`
				} `json:"elem"`
			}
			x := AutoBlocked{}
			if json.Unmarshal(raw, &addr) == nil {
				x.Address = addr
			} else if json.Unmarshal(raw, &e) == nil && e.Elem != nil && json.Unmarshal(e.Elem.Val, &addr) == nil {
				x.Address, x.ExpiresS = addr, e.Elem.Expires
			}
			if a, err := netip.ParseAddr(x.Address); err == nil && a.Is4() {
				out = append(out, x)
			}
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].ExpiresS > out[j].ExpiresS })
	if len(out) > max {
		out = out[:max]
	}
	return out
}

func idOf(comment, prefix string) int {
	n, err := strconv.Atoi(strings.TrimPrefix(comment, prefix))
	if err != nil {
		return 0
	}
	return n
}
