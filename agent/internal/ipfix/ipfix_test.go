package ipfix

import (
	"encoding/binary"
	"fmt"
	"net"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/flows"
)

// decoded is one data record read back with the template in the message,
// the way a collector would.
type decoded map[string]any

func decode(t *testing.T, msg []byte) (hdr [4]uint32, recs []decoded) {
	t.Helper()
	if v := binary.BigEndian.Uint16(msg); v != Version {
		t.Fatalf("version %d", v)
	}
	if l := int(binary.BigEndian.Uint16(msg[2:])); l != len(msg) {
		t.Fatalf("length %d, message %d", l, len(msg))
	}
	hdr = [4]uint32{uint32(len(msg)), binary.BigEndian.Uint32(msg[4:]), binary.BigEndian.Uint32(msg[8:]), binary.BigEndian.Uint32(msg[12:])}
	var tmpl []Field
	p := 16
	for p < len(msg) {
		id := binary.BigEndian.Uint16(msg[p:])
		l := int(binary.BigEndian.Uint16(msg[p+2:]))
		body := msg[p+4 : p+l]
		switch {
		case id == setTemplate:
			if tid := binary.BigEndian.Uint16(body); tid != TemplateID {
				t.Fatalf("template id %d", tid)
			}
			n := int(binary.BigEndian.Uint16(body[2:]))
			q := 4
			for i := 0; i < n; i++ {
				f := Field{ID: binary.BigEndian.Uint16(body[q:]), Length: binary.BigEndian.Uint16(body[q+2:])}
				q += 4
				if f.ID&0x8000 != 0 {
					f.ID &^= 0x8000
					f.Enterprise = binary.BigEndian.Uint32(body[q:])
					q += 4
				}
				tmpl = append(tmpl, f)
			}
		case id == TemplateID:
			q := 0
			for q < len(body) {
				r := decoded{}
				for _, f := range tmpl {
					name := fmt.Sprintf("%d/%d", f.Enterprise, f.ID)
					n := int(f.Length)
					if f.Length == 65535 {
						n = int(body[q])
						q++
					}
					v := body[q : q+n]
					q += n
					switch {
					case f.Length == 65535:
						r[name] = string(v)
					case f.ID == 12 && f.Enterprise == 0:
						r[name] = net.IP(v).String()
					case n == 1:
						r[name] = uint64(v[0])
					case n == 2:
						r[name] = uint64(binary.BigEndian.Uint16(v))
					case n == 4:
						r[name] = uint64(binary.BigEndian.Uint32(v))
					case n == 8:
						r[name] = binary.BigEndian.Uint64(v)
					}
				}
				recs = append(recs, r)
			}
		default:
			t.Fatalf("unexpected set %d", id)
		}
		p += l
	}
	return hdr, recs
}

func sample(n int) []flows.Flow {
	at := time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)
	var out []flows.Flow
	for i := 0; i < n; i++ {
		out = append(out, flows.Flow{
			At: at, Proto: "udp", Dst: fmt.Sprintf("52.112.%d.%d", i/250, i%250+1), DPort: 3478,
			Class: "voice", Flows: 2, BytesOut: 1000 + uint64(i), BytesIn: 2000, PktsOut: 10, PktsIn: 20,
		})
	}
	return out
}

func TestMessageRoundTrip(t *testing.T) {
	fl := sample(2)
	fl[1].Proto, fl[1].DPort, fl[1].Class = "tcp", 443, "business"
	fl = append(fl, flows.Flow{Dst: "2001:db8::1", Proto: "tcp"}) // IPv6 is not in the template: skipped
	at := time.Date(2026, 10, 7, 12, 0, 5, 0, time.UTC)
	msgs, n := Messages(fl, 42, 7, at)
	if len(msgs) != 1 || n != 2 {
		t.Fatalf("messages %d records %d", len(msgs), n)
	}
	hdr, recs := decode(t, msgs[0])
	if hdr[1] != uint32(at.Unix()) || hdr[2] != 7 || hdr[3] != 42 {
		t.Fatalf("header %v", hdr)
	}
	if len(recs) != 2 {
		t.Fatalf("records %d", len(recs))
	}
	r := recs[0]
	want := decoded{
		"0/151": uint64(fl[0].At.Unix()), "0/4": uint64(17), "0/12": "52.112.0.1", "0/11": uint64(3478),
		"0/1": uint64(1000), "0/2": uint64(10), "29305/1": uint64(2000), "29305/2": uint64(20), "0/3": uint64(2),
		"32473/1": "voice",
	}
	for k, v := range want {
		if r[k] != v {
			t.Errorf("%s = %v, want %v", k, r[k], v)
		}
	}
	if recs[1]["0/4"] != uint64(6) || recs[1]["32473/1"] != "business" || recs[1]["0/11"] != uint64(443) {
		t.Errorf("second record %v", recs[1])
	}
}

func TestLargeExportsSplitAndCountSequence(t *testing.T) {
	msgs, n := Messages(sample(100), 1, 1000, time.Now())
	if n != 100 || len(msgs) < 2 {
		t.Fatalf("records %d messages %d", n, len(msgs))
	}
	seq, total := uint32(1000), 0
	for _, m := range msgs {
		if len(m) > MaxMessage {
			t.Fatalf("message of %d bytes", len(m))
		}
		hdr, recs := decode(t, m)
		if hdr[2] != seq {
			t.Fatalf("sequence %d, want %d", hdr[2], seq)
		}
		seq += uint32(len(recs))
		total += len(recs)
	}
	if total != 100 {
		t.Fatalf("decoded %d", total)
	}
	if msgs, n := Messages(nil, 1, 0, time.Now()); len(msgs) != 0 || n != 0 {
		t.Fatal("no flows, no message")
	}
}

func TestExporterSendsOverUDP(t *testing.T) {
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer pc.Close()
	var e Exporter
	defer e.Close()
	n, err := e.Export(pc.LocalAddr().String(), 9, sample(3), time.Now())
	if err != nil || n != 3 {
		t.Fatalf("export %d %v", n, err)
	}
	buf := make([]byte, 65535)
	pc.SetReadDeadline(time.Now().Add(5 * time.Second))
	k, _, err := pc.ReadFrom(buf)
	if err != nil {
		t.Fatal(err)
	}
	hdr, recs := decode(t, buf[:k])
	if hdr[2] != 0 || hdr[3] != 9 || len(recs) != 3 {
		t.Fatalf("header %v records %d", hdr, len(recs))
	}
	// The next export continues the sequence.
	if _, err := e.Export(pc.LocalAddr().String(), 9, sample(1), time.Now()); err != nil {
		t.Fatal(err)
	}
	k, _, _ = pc.ReadFrom(buf)
	if hdr, _ := decode(t, buf[:k]); hdr[2] != 3 {
		t.Fatalf("sequence %d", hdr[2])
	}
	if _, err := e.Export("not an address", 9, sample(1), time.Now()); err == nil {
		t.Fatal("a bad collector must fail")
	}
}
