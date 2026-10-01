// Package flows turns the conntrack table into per-destination traffic
// figures, so the controller can see which applications a site uses and which
// class carried them. The agent reads conntrack every 60 s; this package
// parses the table, works out what moved since the last read, and aggregates
// by protocol, destination, port and class.
package flows

import (
	"bufio"
	"bytes"
	"net/netip"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Entry is one conntrack entry: the original tuple and the counters in each
// direction (out is the original direction, in the reply).
type Entry struct {
	Proto    string
	Src, Dst netip.Addr
	SPort    int
	DPort    int
	PktsOut  uint64
	BytesOut uint64
	PktsIn   uint64
	BytesIn  uint64
	Mark     uint32
}

// Parse reads /proc/net/nf_conntrack or `conntrack -L -o extended` output
// (the same, with or without the leading "ipv4 2"). Only IPv4 TCP and UDP
// entries are kept; lines it cannot read are skipped.
func Parse(text []byte) []Entry {
	var out []Entry
	sc := bufio.NewScanner(bytes.NewReader(text))
	sc.Buffer(make([]byte, 64<<10), 64<<10)
	for sc.Scan() {
		if e, ok := parseLine(sc.Text()); ok {
			out = append(out, e)
		}
	}
	return out
}

func parseLine(line string) (Entry, bool) {
	f := strings.Fields(line)
	if len(f) > 2 && (f[0] == "ipv4" || f[0] == "ipv6") {
		if f[0] != "ipv4" {
			return Entry{}, false
		}
		f = f[2:]
	}
	if len(f) < 2 || (f[0] != "tcp" && f[0] != "udp") {
		return Entry{}, false
	}
	e := Entry{Proto: f[0]}
	tuple := -1 // 0 original, 1 reply
	var src, dst string
	for _, tok := range f[2:] {
		k, v, ok := strings.Cut(tok, "=")
		if !ok {
			continue
		}
		if k == "src" {
			tuple++
		}
		switch {
		case k == "mark":
			n, _ := strconv.ParseUint(v, 0, 32)
			e.Mark = uint32(n)
		case tuple == 0:
			switch k {
			case "src":
				src = v
			case "dst":
				dst = v
			case "sport":
				e.SPort, _ = strconv.Atoi(v)
			case "dport":
				e.DPort, _ = strconv.Atoi(v)
			case "packets":
				e.PktsOut, _ = strconv.ParseUint(v, 10, 64)
			case "bytes":
				e.BytesOut, _ = strconv.ParseUint(v, 10, 64)
			}
		case tuple == 1:
			switch k {
			case "packets":
				e.PktsIn, _ = strconv.ParseUint(v, 10, 64)
			case "bytes":
				e.BytesIn, _ = strconv.ParseUint(v, 10, 64)
			}
		}
	}
	var err1, err2 error
	e.Src, err1 = netip.ParseAddr(src)
	e.Dst, err2 = netip.ParseAddr(dst)
	if err1 != nil || err2 != nil || !e.Src.Is4() || !e.Dst.Is4() {
		return Entry{}, false
	}
	return e, true
}

// Flow is one aggregate in telemetry: what flows from the site's LAN to one
// destination and port, in one class, moved since the last read.
type Flow struct {
	At       time.Time `json:"at"`
	Proto    string    `json:"proto"`
	Dst      string    `json:"dst"`
	DPort    int       `json:"dport"`
	Class    string    `json:"class"`
	Flows    int       `json:"flows"`
	BytesOut uint64    `json:"bytes_out"`
	BytesIn  uint64    `json:"bytes_in"`
	PktsOut  uint64    `json:"pkts_out"`
	PktsIn   uint64    `json:"pkts_in"`
}

type tuple struct {
	proto      string
	src, dst   netip.Addr
	sport, dpt int
}

type counts struct{ pktsOut, bytesOut, pktsIn, bytesIn uint64 }

// Tracker keeps the previous read so each report carries only what moved.
type Tracker struct {
	prev   map[tuple]counts
	primed bool
}

// Update takes a fresh conntrack read and returns the top aggregates by
// bytes. Only flows from inside local to outside it count. The first call
// only records a baseline and returns nothing. A tuple not seen last time
// counts in full, and so does one whose counters went down (a new
// connection reusing the tuple). classOf names the class of a conntrack mark.
func (t *Tracker) Update(entries []Entry, local []netip.Prefix, classOf map[uint32]string, at time.Time, top int) []Flow {
	inside := func(a netip.Addr) bool {
		for _, p := range local {
			if p.Contains(a) {
				return true
			}
		}
		return false
	}
	cur := map[tuple]counts{}
	marks := map[tuple]uint32{}
	for _, e := range entries {
		if !inside(e.Src) || inside(e.Dst) {
			continue
		}
		k := tuple{e.Proto, e.Src, e.Dst, e.SPort, e.DPort}
		c := cur[k]
		c.pktsOut += e.PktsOut
		c.bytesOut += e.BytesOut
		c.pktsIn += e.PktsIn
		c.bytesIn += e.BytesIn
		cur[k], marks[k] = c, e.Mark
	}
	prev, primed := t.prev, t.primed
	t.prev, t.primed = cur, true
	if !primed {
		return nil
	}

	type aggKey struct {
		proto, dst string
		dport      int
		class      string
	}
	agg := map[aggKey]*Flow{}
	for k, c := range cur {
		d := c
		if p, ok := prev[k]; ok && c.pktsOut >= p.pktsOut && c.bytesOut >= p.bytesOut && c.pktsIn >= p.pktsIn && c.bytesIn >= p.bytesIn {
			d = counts{c.pktsOut - p.pktsOut, c.bytesOut - p.bytesOut, c.pktsIn - p.pktsIn, c.bytesIn - p.bytesIn}
		}
		if d.bytesOut+d.bytesIn == 0 && d.pktsOut+d.pktsIn == 0 {
			continue
		}
		ak := aggKey{k.proto, k.dst.String(), k.dpt, classOf[marks[k]]}
		f := agg[ak]
		if f == nil {
			f = &Flow{At: at, Proto: ak.proto, Dst: ak.dst, DPort: ak.dport, Class: ak.class}
			agg[ak] = f
		}
		f.Flows++
		f.BytesOut += d.bytesOut
		f.BytesIn += d.bytesIn
		f.PktsOut += d.pktsOut
		f.PktsIn += d.pktsIn
	}
	out := make([]Flow, 0, len(agg))
	for _, f := range agg {
		if f.BytesOut+f.BytesIn > 0 {
			out = append(out, *f)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if x, y := a.BytesOut+a.BytesIn, b.BytesOut+b.BytesIn; x != y {
			return x > y
		}
		if a.Proto != b.Proto {
			return a.Proto < b.Proto
		}
		if a.Dst != b.Dst {
			return a.Dst < b.Dst
		}
		if a.DPort != b.DPort {
			return a.DPort < b.DPort
		}
		return a.Class < b.Class
	})
	if len(out) > top {
		out = out[:top]
	}
	return out
}
