// Package ipfix exports the agent's flow aggregates as IPFIX (RFC 7011)
// over UDP, so a customer's own collector (nfdump, ntopng, Elastic,
// Kentik, ...) sees what each site sends, and in which class.
//
// Each message carries the template and the data records, so a collector
// that starts late or loses a packet decodes the next message without
// waiting (RFC 7011 §8.4 lets UDP exporters resend templates at will).
// The collector and observation domain come from desired state (ADR 0026).
package ipfix

import (
	"encoding/binary"
	"fmt"
	"net"
	"net/netip"
	"sync"
	"time"

	"github.com/zagias/exaconnect/agent/internal/flows"
)

const (
	Version     = 10
	TemplateID  = 256
	setTemplate = 2

	// PENReverse is the enterprise number for reverse-direction elements
	// (RFC 5103 §6.1).
	PENReverse = 29305
	// PENExaCarib is ExaCarib's IANA private enterprise number. 32473 is the
	// documentation number (RFC 5612) until ExaCarib registers its own.
	PENExaCarib = 32473
	// IEClassName is ExaCarib's element 1: the Connect application class.
	IEClassName = 1

	// MaxMessage keeps a message within one unfragmented datagram on a
	// 1500-byte path, with room for IP, UDP and a tunnel.
	MaxMessage = 1380
)

// Field is one template field.
type Field struct {
	ID         uint16
	Length     uint16 // 65535: variable length
	Enterprise uint32
}

// Fields is the template, in record order.
var Fields = []Field{
	{ID: 151, Length: 4},                       // flowEndSeconds
	{ID: 4, Length: 1},                         // protocolIdentifier
	{ID: 12, Length: 4},                        // destinationIPv4Address
	{ID: 11, Length: 2},                        // destinationTransportPort
	{ID: 1, Length: 8},                         // octetDeltaCount
	{ID: 2, Length: 8},                         // packetDeltaCount
	{ID: 1, Length: 8, Enterprise: PENReverse}, // reverseOctetDeltaCount
	{ID: 2, Length: 8, Enterprise: PENReverse}, // reversePacketDeltaCount
	{ID: 3, Length: 8},                         // deltaFlowCount
	{ID: IEClassName, Length: 65535, Enterprise: PENExaCarib}, // exacaribClassName
}

func protoNumber(p string) uint8 {
	switch p {
	case "tcp":
		return 6
	case "udp":
		return 17
	}
	return 0
}

func templateSet() []byte {
	b := make([]byte, 0, 8+len(Fields)*8)
	b = binary.BigEndian.AppendUint16(b, setTemplate)
	b = binary.BigEndian.AppendUint16(b, 0) // length, below
	b = binary.BigEndian.AppendUint16(b, TemplateID)
	b = binary.BigEndian.AppendUint16(b, uint16(len(Fields)))
	for _, f := range Fields {
		id := f.ID
		if f.Enterprise != 0 {
			id |= 0x8000
		}
		b = binary.BigEndian.AppendUint16(b, id)
		b = binary.BigEndian.AppendUint16(b, f.Length)
		if f.Enterprise != 0 {
			b = binary.BigEndian.AppendUint32(b, f.Enterprise)
		}
	}
	binary.BigEndian.PutUint16(b[2:], uint16(len(b)))
	return b
}

func record(f flows.Flow) ([]byte, bool) {
	dst, err := netip.ParseAddr(f.Dst)
	if err != nil || !dst.Is4() {
		return nil, false
	}
	b := make([]byte, 0, 64)
	b = binary.BigEndian.AppendUint32(b, uint32(f.At.Unix()))
	b = append(b, protoNumber(f.Proto))
	a4 := dst.As4()
	b = append(b, a4[:]...)
	b = binary.BigEndian.AppendUint16(b, uint16(f.DPort))
	b = binary.BigEndian.AppendUint64(b, f.BytesOut)
	b = binary.BigEndian.AppendUint64(b, f.PktsOut)
	b = binary.BigEndian.AppendUint64(b, f.BytesIn)
	b = binary.BigEndian.AppendUint64(b, f.PktsIn)
	b = binary.BigEndian.AppendUint64(b, uint64(f.Flows))
	name := f.Class
	if len(name) > 254 {
		name = name[:254]
	}
	b = append(b, byte(len(name))) // variable length, short form (RFC 7011 §7)
	b = append(b, name...)
	return b, true
}

// Messages encodes flows into one or more IPFIX messages, each under
// MaxMessage bytes. seq is the sequence number of the first message: the
// count of data records sent before it (RFC 7011 §3.1). It returns the
// messages and the number of records encoded.
func Messages(fl []flows.Flow, domain uint32, seq uint32, at time.Time) ([][]byte, int) {
	tmpl := templateSet()
	var out [][]byte
	var recs [][]byte
	n := 0
	flush := func() {
		if len(recs) == 0 {
			return
		}
		msg := make([]byte, 16, MaxMessage)
		binary.BigEndian.PutUint16(msg[0:], Version)
		binary.BigEndian.PutUint32(msg[4:], uint32(at.Unix()))
		binary.BigEndian.PutUint32(msg[8:], seq)
		binary.BigEndian.PutUint32(msg[12:], domain)
		msg = append(msg, tmpl...)
		start := len(msg)
		msg = binary.BigEndian.AppendUint16(msg, TemplateID)
		msg = binary.BigEndian.AppendUint16(msg, 0)
		for _, r := range recs {
			msg = append(msg, r...)
		}
		binary.BigEndian.PutUint16(msg[start+2:], uint16(len(msg)-start))
		binary.BigEndian.PutUint16(msg[2:], uint16(len(msg)))
		out = append(out, msg)
		seq += uint32(len(recs))
		recs = nil
	}
	size := 16 + len(tmpl) + 4
	for _, f := range fl {
		r, ok := record(f)
		if !ok {
			continue
		}
		if size+len(r) > MaxMessage {
			flush()
			size = 16 + len(tmpl) + 4
		}
		recs = append(recs, r)
		size += len(r)
		n++
	}
	flush()
	return out, n
}

// Exporter sends to one collector and keeps the sequence number.
type Exporter struct {
	mu        sync.Mutex
	collector string
	conn      net.Conn
	seq       uint32
	// Dial is net.Dial unless a test replaces it.
	Dial func(network, address string) (net.Conn, error)
}

// Export sends the flows to collector ("host:port"), reconnecting when the
// collector changes. It returns the number of records sent.
func (e *Exporter) Export(collector string, domain uint32, fl []flows.Flow, at time.Time) (int, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if collector != e.collector && e.conn != nil {
		e.conn.Close()
		e.conn = nil
	}
	if e.conn == nil {
		dial := e.Dial
		if dial == nil {
			dial = net.Dial
		}
		c, err := dial("udp", collector)
		if err != nil {
			return 0, fmt.Errorf("ipfix collector %s: %w", collector, err)
		}
		e.conn, e.collector = c, collector
	}
	msgs, n := Messages(fl, domain, e.seq, at)
	for _, m := range msgs {
		if _, err := e.conn.Write(m); err != nil {
			return 0, fmt.Errorf("ipfix send: %w", err)
		}
	}
	e.seq += uint32(n)
	return n, nil
}

// Close drops the connection (the collector was removed from desired state).
func (e *Exporter) Close() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.conn != nil {
		e.conn.Close()
		e.conn = nil
	}
	e.collector = ""
}
