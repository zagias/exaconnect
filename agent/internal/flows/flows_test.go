package flows

import (
	"net/netip"
	"testing"
	"time"
)

func TestParse(t *testing.T) {
	cases := []struct {
		name string
		line string
		want *Entry // nil: skipped
	}{
		{
			name: "proc format",
			line: "ipv4     2 udp      17 29 src=192.168.10.10 dst=192.168.20.10 sport=40000 dport=8801 packets=10 bytes=1600 src=192.168.20.10 dst=192.168.10.10 sport=8801 dport=40000 packets=9 bytes=1400 [ASSURED] mark=257 zone=0 use=1",
			want: &Entry{Proto: "udp", Src: netip.MustParseAddr("192.168.10.10"), Dst: netip.MustParseAddr("192.168.20.10"),
				SPort: 40000, DPort: 8801, PktsOut: 10, BytesOut: 1600, PktsIn: 9, BytesIn: 1400, Mark: 257},
		},
		{
			name: "conntrack tool, tcp with state",
			line: "tcp      6 431999 ESTABLISHED src=192.168.10.10 dst=52.113.1.1 sport=51000 dport=443 packets=120 bytes=9000 src=52.113.1.1 dst=100.64.1.11 sport=443 dport=51000 packets=200 bytes=250000 [ASSURED] mark=258 use=1",
			want: &Entry{Proto: "tcp", Src: netip.MustParseAddr("192.168.10.10"), Dst: netip.MustParseAddr("52.113.1.1"),
				SPort: 51000, DPort: 443, PktsOut: 120, BytesOut: 9000, PktsIn: 200, BytesIn: 250000, Mark: 258},
		},
		{
			name: "unreplied, no accounting, no mark",
			line: "udp      17 20 src=192.168.10.10 dst=8.8.8.8 sport=5353 dport=53 [UNREPLIED] src=8.8.8.8 dst=192.168.10.10 sport=53 dport=5353 use=1",
			want: &Entry{Proto: "udp", Src: netip.MustParseAddr("192.168.10.10"), Dst: netip.MustParseAddr("8.8.8.8"), SPort: 5353, DPort: 53},
		},
		{name: "icmp", line: "ipv4     2 icmp     1 29 src=192.168.10.10 dst=8.8.8.8 type=8 code=0 id=1 packets=1 bytes=84 src=8.8.8.8 dst=192.168.10.10 type=0 code=0 id=1 packets=1 bytes=84 mark=0 use=1"},
		{name: "ipv6", line: "ipv6     10 udp      17 29 src=fd00::1 dst=fd00::2 sport=1 dport=2 packets=1 bytes=1 src=fd00::2 dst=fd00::1 sport=2 dport=1 packets=1 bytes=1 mark=0 use=1"},
		{name: "summary line", line: "conntrack v1.4.8 (conntrack-tools): 3 flow entries have been shown."},
		{name: "empty", line: ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Parse([]byte(tc.line + "\n"))
			if tc.want == nil {
				if len(got) != 0 {
					t.Fatalf("parsed %+v, want nothing", got)
				}
				return
			}
			if len(got) != 1 || got[0] != *tc.want {
				t.Fatalf("got %+v\nwant %+v", got, *tc.want)
			}
		})
	}
}

func entry(proto, src string, sport int, dst string, dport int, out, in uint64, mark uint32) Entry {
	return Entry{Proto: proto, Src: netip.MustParseAddr(src), Dst: netip.MustParseAddr(dst), SPort: sport, DPort: dport,
		PktsOut: out / 100, BytesOut: out, PktsIn: in / 100, BytesIn: in, Mark: mark}
}

func TestTrackerDeltasAndAggregation(t *testing.T) {
	local := []netip.Prefix{netip.MustParsePrefix("192.168.10.0/24")}
	classes := map[uint32]string{257: "voice", 259: "bulk"}
	at := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	var tr Tracker

	first := []Entry{
		entry("udp", "192.168.10.10", 40000, "192.168.20.10", 8801, 1000, 900, 257),
		entry("udp", "192.168.10.11", 40002, "192.168.20.10", 8801, 5000, 5000, 257),
		entry("tcp", "192.168.10.10", 50000, "203.0.113.5", 443, 9000, 1000, 0),
		entry("tcp", "192.168.10.12", 50001, "198.51.100.9", 22, 400, 400, 259),
	}
	if got := tr.Update(first, local, classes, at, 100); got != nil {
		t.Fatalf("first read should only set a baseline, got %+v", got)
	}

	second := []Entry{
		// grew by 600/500
		entry("udp", "192.168.10.10", 40000, "192.168.20.10", 8801, 1600, 1400, 257),
		// counters went down: a new connection on the same tuple counts in full
		entry("udp", "192.168.10.11", 40002, "192.168.20.10", 8801, 300, 200, 257),
		// new tuple to the same destination and class: counts in full
		entry("udp", "192.168.10.12", 40004, "192.168.20.10", 8801, 100, 0, 257),
		// idle: zero delta, dropped
		entry("tcp", "192.168.10.10", 50000, "203.0.113.5", 443, 9000, 1000, 0),
		// same destination as the voice flows but unmarked: its own aggregate, class ""
		entry("udp", "192.168.10.13", 40006, "192.168.20.10", 8801, 50, 50, 0),
		// mark the map does not know: class ""
		entry("tcp", "192.168.10.12", 50001, "198.51.100.9", 22, 1400, 400, 999),
		// from outside the LAN: ignored
		entry("udp", "10.9.9.9", 1, "192.168.20.10", 8801, 99999, 0, 257),
		// LAN to LAN: ignored
		entry("udp", "192.168.10.10", 1, "192.168.10.20", 2, 99999, 0, 257),
	}
	got := tr.Update(second, local, classes, at, 100)
	want := []Flow{
		{At: at, Proto: "udp", Dst: "192.168.20.10", DPort: 8801, Class: "voice", Flows: 3, BytesOut: 600 + 300 + 100, BytesIn: 500 + 200, PktsOut: 6 + 3 + 1, PktsIn: 5 + 2},
		{At: at, Proto: "tcp", Dst: "198.51.100.9", DPort: 22, Class: "", Flows: 1, BytesOut: 1000, PktsOut: 10},
		{At: at, Proto: "udp", Dst: "192.168.20.10", DPort: 8801, Class: "", Flows: 1, BytesOut: 50, BytesIn: 50},
	}
	if len(got) != len(want) {
		t.Fatalf("got %d aggregates: %+v", len(got), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("aggregate %d:\n got %+v\nwant %+v", i, got[i], want[i])
		}
	}

	// Nothing moved: nothing to report.
	if got := tr.Update(second, local, classes, at, 100); len(got) != 0 {
		t.Fatalf("unchanged read reported %+v", got)
	}
}

func TestTrackerTopN(t *testing.T) {
	local := []netip.Prefix{netip.MustParsePrefix("192.168.10.0/24")}
	var tr Tracker
	tr.Update(nil, local, nil, time.Time{}, 2)
	var es []Entry
	for i, b := range []uint64{100, 300, 200, 50} {
		es = append(es, entry("tcp", "192.168.10.10", 1000+i, "203.0.113."+string(rune('1'+i)), 443, b, 0, 0))
	}
	got := tr.Update(es, local, nil, time.Time{}, 2)
	if len(got) != 2 || got[0].Dst != "203.0.113.2" || got[1].Dst != "203.0.113.3" {
		t.Fatalf("top 2 = %+v", got)
	}
}
