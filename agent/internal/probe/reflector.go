package probe

import (
	"context"
	"net"
	"time"
)

// Reflect answers probes on conn until ctx is done.
func Reflect(ctx context.Context, conn net.PacketConn) error {
	go func() { <-ctx.Done(); conn.Close() }()
	buf := make([]byte, 1500)
	out := make([]byte, Size)
	for {
		n, addr, err := conn.ReadFrom(buf)
		rx := time.Now().UnixNano()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		p, err := unmarshal(buf[:n])
		if err != nil {
			continue
		}
		p.T2 = rx
		p.T3 = time.Now().UnixNano()
		_, _ = conn.WriteTo(p.marshal(out), addr)
	}
}
