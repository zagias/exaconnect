// Package frrstate reads BFD session state from FRR and WireGuard handshake
// ages, for events and telemetry.
package frrstate

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"github.com/zagias/exaconnect/agent/internal/system"
)

type BFDPeer struct {
	Peer      string `json:"peer"`
	Interface string `json:"interface"`
	Status    string `json:"status"`
}

func BFDPeers(ctx context.Context, sys system.Runner) ([]BFDPeer, error) {
	out, err := sys.Run(ctx, "vtysh", "-c", "show bfd peers json")
	if err != nil {
		return nil, err
	}
	return ParseBFD(out)
}

func ParseBFD(b []byte) ([]BFDPeer, error) {
	var peers []BFDPeer
	err := json.Unmarshal(b, &peers)
	return peers, err
}

// HandshakeAge returns seconds since the newest handshake on a WireGuard
// interface, or -1 if there has been none.
func HandshakeAge(ctx context.Context, sys system.Runner, dev string, now time.Time) (int64, error) {
	out, err := sys.Run(ctx, "wg", "show", dev, "latest-handshakes")
	if err != nil {
		return -1, err
	}
	return ParseHandshakes(string(out), now), nil
}

func ParseHandshakes(out string, now time.Time) int64 {
	var newest int64
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		f := strings.Fields(line)
		if len(f) != 2 {
			continue
		}
		if ts, err := strconv.ParseInt(f[1], 10, 64); err == nil && ts > newest {
			newest = ts
		}
	}
	if newest == 0 {
		return -1
	}
	return now.Unix() - newest
}
