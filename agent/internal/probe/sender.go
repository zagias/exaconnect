package probe

import (
	"context"
	"net"
	"time"
)

// Sender probes one path: it sends a probe every Interval to Target from a
// socket bound to Device (the path's tunnel interface) and feeds Stats.
type Sender struct {
	Path     string
	Device   string // "" = no binding (tests)
	Source   string // local address to send from, "" = the kernel's choice
	Target   string
	Interval time.Duration
	Stats    *Stats
}

func (s *Sender) Run(ctx context.Context) error {
	d := net.Dialer{Control: bindControl(s.Device)}
	if s.Source != "" {
		ip := net.ParseIP(s.Source)
		if ip == nil {
			return &net.AddrError{Err: "bad source address", Addr: s.Source}
		}
		d.LocalAddr = &net.UDPAddr{IP: ip}
	}
	conn, err := d.DialContext(ctx, "udp", s.Target)
	if err != nil {
		return err
	}
	defer conn.Close()
	go s.receive(ctx, conn)

	t := time.NewTicker(s.Interval)
	defer t.Stop()
	buf := make([]byte, Size)
	var seq uint32
	for {
		seq++
		now := time.Now()
		s.Stats.Sent(seq, now)
		// A send error (path down) is just a lost probe.
		_, _ = conn.Write(packet{Seq: seq, T1: now.UnixNano()}.marshal(buf))
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
		}
	}
}

func (s *Sender) receive(ctx context.Context, conn net.Conn) {
	go func() { <-ctx.Done(); conn.Close() }()
	buf := make([]byte, 1500)
	for {
		n, err := conn.Read(buf)
		t4 := time.Now().UnixNano()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			// ICMP unreachable surfaces as a read error on connected UDP; keep going.
			time.Sleep(50 * time.Millisecond)
			continue
		}
		p, err := unmarshal(buf[:n])
		if err != nil {
			continue
		}
		rtt := time.Duration((t4 - p.T1) - (p.T3 - p.T2))
		s.Stats.Received(p.Seq, rtt)
	}
}
