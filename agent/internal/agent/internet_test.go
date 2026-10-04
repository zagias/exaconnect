package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// What `nft -j list table ip exa_inet` prints on a PoP (nft 1.0.9), trimmed.
const nftJSON = `{"nftables": [{"metainfo": {"version": "1.0.9", "json_schema_version": 1}},
{"table": {"family": "ip", "name": "exa_inet", "handle": 4}},
{"chain": {"family": "ip", "table": "exa_inet", "name": "filter_fwd", "handle": 2, "type": "filter", "hook": "forward", "prio": 0, "policy": "accept"}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "post", "handle": 5, "expr": [{"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": "eth9"}}, {"masquerade": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "filter_fwd", "handle": 6, "expr": [{"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": ["established", "related"]}}, {"accept": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "filter_fwd", "handle": 7, "comment": "pf3", "expr": [{"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "eth9"}}, {"counter": {"packets": 7, "bytes": 420}}, {"accept": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "filter_fwd", "handle": 8, "comment": "inbound", "expr": [{"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "eth9"}}, {"counter": {"packets": 2, "bytes": 120}}, {"drop": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "filter_fwd", "handle": 10, "comment": "fw12", "expr": [{"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "daddr"}}, "right": {"prefix": {"addr": "198.51.100.0", "len": 24}}}}, {"counter": {"packets": 40, "bytes": 3360}}, {"drop": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "filter_fwd", "handle": 11, "comment": "someone else's", "expr": [{"counter": {"packets": 1, "bytes": 1}}, {"drop": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "filter_fwd", "handle": 12, "comment": "fw13", "expr": [{"accept": null}]}}]}`

func TestParseInternetCounters(t *testing.T) {
	got := parseInternetCounters([]byte(nftJSON))
	want := []InternetCounter{
		{Kind: "forward", ID: 3, Packets: 7, Bytes: 420},
		{Kind: "inbound", ID: 0, Packets: 2, Bytes: 120},
		{Kind: "rule", ID: 12, Packets: 40, Bytes: 3360},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v", got)
	}
	if got := parseInternetCounters([]byte("not json")); got == nil || len(got) != 0 {
		t.Fatalf("bad input: %+v", got)
	}
}

// nftSys answers `nft -j list table` with nftJSON.
type nftSys struct{ fakeSys }

func (n *nftSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	n.fakeSys.Run(ctx, name, args...)
	if name+" "+strings.Join(args, " ") == "nft -j list table ip exa_inet" {
		return []byte(nftJSON), nil
	}
	return nil, nil
}

func TestInternetTelemetry(t *testing.T) {
	sys := &nftSys{fakeSys{files: map[string]string{}}}
	a := newAgent("http://unused", &sys.fakeSys)
	a.Sys, a.Applier.Sys = sys, sys
	pop := &desired.State{Schema: 1, Version: 1, Role: desired.RolePoP, ASN: 65000, RouterID: "100.64.1.1",
		Internet: &desired.Internet{Mode: "gateway", LANPrefixes: []string{"192.168.10.0/24"},
			Uplinks: []desired.Uplink{{Interface: "eth9", Gateway: "100.64.0.1"}}}}
	if err := a.Applier.Apply(context.Background(), pop); err != nil {
		t.Fatal(err)
	}
	got := a.internetState(context.Background(), pop)
	if got == nil || got.Mode != "gateway" || got.Via != "eth9" || len(got.Counters) != 3 {
		t.Fatalf("%+v", got)
	}
	b, _ := json.Marshal(got)
	if !strings.Contains(string(b), `{"kind":"rule","id":12,"packets":40,"bytes":3360}`) {
		t.Fatalf("json %s", b)
	}

	// A pop-mode site has no firewall of its own: no counters, and no nft run for them.
	site := &desired.State{Role: desired.RoleSite, Internet: &desired.Internet{Mode: "pop"}}
	n := sys.count("nft -j")
	if got := a.internetState(context.Background(), site); got == nil || got.Mode != "pop" || got.Counters == nil || len(got.Counters) != 0 {
		t.Fatalf("%+v", got)
	}
	if sys.count("nft -j") != n {
		t.Fatal("read counters on a node without the table")
	}
	if a.internetState(context.Background(), &desired.State{Role: desired.RoleSite}) != nil {
		t.Fatal("no block, no internet telemetry")
	}
}

// routeSys prints a default route in the internet table, as the kernel would
// once one is written, so the poll's retry leaves it alone.
type routeSys struct{ *bfdSys }

func (r routeSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	out, err := r.bfdSys.Run(ctx, name, args...)
	if name+" "+strings.Join(args, " ") == "ip route show table 251" {
		return []byte("default via 10.11.1.1 dev eth1\n"), nil
	}
	return out, err
}

// A local-mode site fails over from eth1 to eth2 when BFD on wg-a goes down,
// on the agent's own and without steering.
func TestAgentInternetFailover(t *testing.T) {
	ds := state(1)
	ds.Tunnels = []desired.Tunnel{
		{Name: "wg-a", Path: "carrier-a", Address: "100.64.1.11/24", Peers: ds.Tunnels[0].Peers, Neighbors: []desired.Neighbor{{Address: "100.64.1.1", ASN: 65000}}},
		{Name: "wg-b", Path: "carrier-b", Address: "100.64.2.11/24", Peers: ds.Tunnels[0].Peers, Neighbors: []desired.Neighbor{{Address: "100.64.2.1", ASN: 65000}}},
	}
	ds.Internet = &desired.Internet{Mode: "local", LANPrefixes: []string{"192.168.10.0/24"},
		Uplinks: []desired.Uplink{
			{Interface: "eth1", Gateway: "10.11.1.1", Path: "carrier-a", Tunnel: "wg-a"},
			{Interface: "eth2", Gateway: "10.12.1.1", Path: "carrier-b", Tunnel: "wg-b"},
		}}
	var mu sync.Mutex
	var events []Event
	var via string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/agent/desired-state":
			if r.URL.Query().Get("have") == "1" {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			json.NewEncoder(w).Encode(ds)
		case "/api/v1/agent/telemetry":
			var tel Telemetry
			json.NewDecoder(r.Body).Decode(&tel)
			mu.Lock()
			events = append(events, tel.Events...)
			if tel.Internet != nil {
				via = tel.Internet.Via
			}
			mu.Unlock()
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
	a.Sys, a.Applier.Sys = routeSys{sys}, routeSys{sys}
	a.Cfg.BFDInterval = 10 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go a.Run(ctx)

	toEth1 := "ip route replace default via 10.11.1.1 dev eth1 table 251"
	toEth2 := "ip route replace default via 10.12.1.1 dev eth2 table 251"
	waitFor(t, "internet out of eth1", func() bool { return sys.count(toEth1) > 0 })
	waitFor(t, "via eth1 reported", func() bool { mu.Lock(); defer mu.Unlock(); return via == "eth1" })
	if sys.count(toEth2) != 0 {
		t.Fatal("eth2 chosen while carrier A is up")
	}

	sys.mu.Lock()
	sys.bfd["100.64.1.1"] = "down"
	sys.mu.Unlock()
	waitFor(t, "internet failover to eth2", func() bool { return sys.count(toEth2) > 0 })
	waitFor(t, "via eth2 reported", func() bool { mu.Lock(); defer mu.Unlock(); return via == "eth2" })
	waitFor(t, "internet_exit_changed event", func() bool {
		mu.Lock()
		defer mu.Unlock()
		for _, e := range events {
			if e.Kind == "internet_exit_changed" && e.Detail["from"] == "eth1" && e.Detail["to"] == "eth2" {
				return true
			}
		}
		return false
	})
	if n := sys.count(toEth2); n != 1 {
		t.Fatalf("eth2 route written %d times; only a change should write it", n)
	}

	sys.mu.Lock()
	sys.bfd["100.64.1.1"] = "up"
	sys.mu.Unlock()
	waitFor(t, "internet back on eth1", func() bool { return sys.count(toEth1) > 1 })
}
