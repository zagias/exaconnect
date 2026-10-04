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
{"rule": {"family": "ip", "table": "exa_inet", "chain": "filter_fwd", "handle": 12, "comment": "fw13", "expr": [{"accept": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "guard", "handle": 13, "comment": "blocked", "expr": [{"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": "@blocklist"}}, {"counter": {"packets": 5, "bytes": 300}}, {"drop": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "guard", "handle": 14, "comment": "auto", "expr": [{"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": "@auto_block"}}, {"counter": {"packets": 49, "bytes": 2940}}, {"drop": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "guard", "handle": 15, "comment": "flood", "expr": [{"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": "new"}}, {"set": {"op": "update", "elem": {"payload": {"protocol": "ip", "field": "saddr"}}, "set": "@rate", "stmt": [{"limit": {"rate": 50, "burst": 100, "per": "second", "inv": true}}]}}, {"set": {"op": "add", "elem": {"payload": {"protocol": "ip", "field": "saddr"}}, "set": "@auto_block"}}, {"counter": {"packets": 1, "bytes": 60}}, {"drop": null}]}},
{"rule": {"family": "ip", "table": "exa_inet", "chain": "guard", "handle": 16, "comment": "syn", "expr": [{"limit": {"rate": 2000, "burst": 2000, "per": "second", "inv": true}}, {"counter": {"packets": 0, "bytes": 0}}, {"drop": null}]}}]}`

// What `nft -j list set ip exa_inet auto_block` prints (nft 1.0.9): an
// element added by the rule carries only expires; one added by hand with its
// own timeout carries both.
const autoBlockJSON = `{"nftables": [{"metainfo": {"version": "1.0.9", "release_name": "Old Doc Yak #3", "json_schema_version": 1}},
{"set": {"family": "ip", "name": "auto_block", "table": "exa_inet", "type": "ipv4_addr", "handle": 7, "size": 65536,
 "flags": ["timeout", "dynamic"], "timeout": 600,
 "elem": [{"elem": {"val": "203.0.113.9", "expires": 540}}, {"elem": {"val": "203.0.113.10", "timeout": 300, "expires": 299}},
  {"elem": {"val": "198.51.100.7", "expires": 599}}, "192.0.2.1", {"elem": {"val": {"prefix": {"addr": "10.0.0.0", "len": 8}}}}]}}]}`

func TestParseInternetCounters(t *testing.T) {
	got := parseInternetCounters([]byte(nftJSON))
	want := []InternetCounter{
		{Kind: "forward", ID: 3, Packets: 7, Bytes: 420},
		{Kind: "inbound", ID: 0, Packets: 2, Bytes: 120},
		{Kind: "rule", ID: 12, Packets: 40, Bytes: 3360},
		{Kind: "blocked", ID: 0, Packets: 5, Bytes: 300},
		{Kind: "auto", ID: 0, Packets: 49, Bytes: 2940},
		{Kind: "flood", ID: 0, Packets: 1, Bytes: 60},
		{Kind: "syn", ID: 0, Packets: 0, Bytes: 0},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v", got)
	}
	if got := parseInternetCounters([]byte("not json")); got == nil || len(got) != 0 {
		t.Fatalf("bad input: %+v", got)
	}
}

func TestParseAutoBlocked(t *testing.T) {
	got := parseAutoBlocked([]byte(autoBlockJSON), 100)
	want := []AutoBlocked{
		{Address: "198.51.100.7", ExpiresS: 599},
		{Address: "203.0.113.9", ExpiresS: 540},
		{Address: "203.0.113.10", ExpiresS: 299},
		{Address: "192.0.2.1", ExpiresS: 0}, // no timeout of its own: nothing to count down
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v", got)
	}
	if got := parseAutoBlocked([]byte(autoBlockJSON), 2); len(got) != 2 || got[1].Address != "203.0.113.9" {
		t.Fatalf("capped: %+v", got)
	}
	empty := `{"nftables": [{"metainfo": {"version": "1.0.9"}}, {"set": {"family": "ip", "name": "auto_block", "table": "exa_inet", "type": "ipv4_addr", "flags": ["timeout", "dynamic"], "timeout": 600}}]}`
	for _, in := range []string{empty, "not json", ""} {
		if got := parseAutoBlocked([]byte(in), 100); got == nil || len(got) != 0 {
			t.Fatalf("%q: %+v", in, got)
		}
	}
}

// nftSys answers `nft -j list table` with nftJSON and `nft -j list set` with
// autoBlockJSON.
type nftSys struct{ fakeSys }

func (n *nftSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	n.fakeSys.Run(ctx, name, args...)
	switch name + " " + strings.Join(args, " ") {
	case "nft -j list table ip exa_inet":
		return []byte(nftJSON), nil
	case "nft -j list set ip exa_inet auto_block":
		return []byte(autoBlockJSON), nil
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
	if got == nil || got.Mode != "gateway" || got.Via != "eth9" || len(got.Counters) != 7 {
		t.Fatalf("%+v", got)
	}
	b, _ := json.Marshal(got)
	if !strings.Contains(string(b), `{"kind":"rule","id":12,"packets":40,"bytes":3360}`) ||
		!strings.Contains(string(b), `"auto_blocked":[]`) {
		t.Fatalf("json %s", b)
	}
	if sys.count("nft -j list set") != 0 {
		t.Fatal("read the auto block set without protection")
	}

	// Protected: the sources blocked automatically too.
	pop.Version = 2
	pop.Internet.PublicAddress = "100.64.0.2"
	pop.Internet.Protection = &desired.Protection{Enabled: true, NewPerSource: 50, SynPerS: 2000, BlockMinutes: 10}
	if err := a.Applier.Apply(context.Background(), pop); err != nil {
		t.Fatal(err)
	}
	got = a.internetState(context.Background(), pop)
	b, _ = json.Marshal(got)
	if len(got.AutoBlocked) != 4 || !strings.Contains(string(b), `"auto_blocked":[{"address":"198.51.100.7","expires_s":599},`) ||
		!strings.Contains(string(b), `{"kind":"flood","id":0,"packets":1,"bytes":60}`) {
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
