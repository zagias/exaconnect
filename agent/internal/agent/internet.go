package agent

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/render"
)

// InternetState is where internet traffic leaves this node, and the
// firewall's counters (docs/internet-contract.md).
type InternetState struct {
	Mode     string            `json:"mode"`
	Via      string            `json:"via"`
	Counters []InternetCounter `json:"counters"`
}

// InternetCounter is one cumulative nft counter: a firewall rule, a port
// forward, or the inbound drop (id 0).
type InternetCounter struct {
	Kind    string `json:"kind"` // rule | forward | inbound
	ID      int    `json:"id"`
	Packets uint64 `json:"packets"`
	Bytes   uint64 `json:"bytes"`
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
	st := &InternetState{Mode: s.Internet.Mode, Via: a.Applier.InternetVia(), Counters: []InternetCounter{}}
	if s.Firewalled() {
		if out, err := a.Sys.Run(ctx, "nft", "-j", "list", "table", "ip", render.InternetNFTable); err == nil {
			st.Counters = parseInternetCounters(out)
		}
	}
	return st
}

// parseInternetCounters picks the counters of the rules the agent named
// (fw<id>, pf<id>, inbound) out of `nft -j list table`.
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
		case cm == render.CommentInbound:
			c.Kind = "inbound"
		case strings.HasPrefix(cm, render.CommentRule):
			c.Kind, c.ID = "rule", idOf(cm, render.CommentRule)
		case strings.HasPrefix(cm, render.CommentForward):
			c.Kind, c.ID = "forward", idOf(cm, render.CommentForward)
		}
		if c.Kind == "" || (c.Kind != "inbound" && c.ID < 1) {
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

func idOf(comment, prefix string) int {
	n, err := strconv.Atoi(strings.TrimPrefix(comment, prefix))
	if err != nil {
		return 0
	}
	return n
}
