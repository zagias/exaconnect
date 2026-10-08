// Package ike reads IKE and CHILD_SA state from strongSwan's swanctl, for
// circuit telemetry.
package ike

import (
	"context"
	"regexp"
	"strconv"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/system"
)

const (
	Up         = "up"
	Connecting = "connecting"
	Down       = "down"
)

// SA is one connection's best IKE SA: its state, the algorithms negotiated
// and how long ago it was established (-1 unknown).
type SA struct {
	State        string
	IKECipher    string // "AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048"
	ESPCipher    string // "AES_CBC-256/HMAC_SHA2_256_128", of its first installed CHILD_SA
	EstablishedS int
}

// NoSA is what a connection with no SA reports.
var NoSA = SA{State: Down, EstablishedS: -1}

// States runs `swanctl --list-sas` and returns each connection's state. When
// charon cannot be asked, every connection is down: the map is empty.
func States(ctx context.Context, sys system.Runner) map[string]string {
	out, err := sys.Run(ctx, "swanctl", "--list-sas")
	if err != nil {
		return map[string]string{}
	}
	return Parse(string(out))
}

// SAs runs `swanctl --list-sas` and returns each connection's best SA, as
// for States but with the ciphers and age (ADR 0012).
func SAs(ctx context.Context, sys system.Runner) map[string]SA {
	out, err := sys.Run(ctx, "swanctl", "--list-sas")
	if err != nil {
		return map[string]SA{}
	}
	return ParseSAs(string(out))
}

// An SA line: "<name>: #<n>, <rest>". IKE SAs start at column 0; their
// CHILD_SAs are indented under them.
var saLine = regexp.MustCompile(`^(\s*)([^\s:]+): #\d+, (.*)$`)

// The IKE SA's algorithms, "AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048"
// or, with an AEAD cipher, "AES_GCM_16-256/PRF_HMAC_SHA2_256/MODP_2048".
var algLine = regexp.MustCompile(`^\s+([A-Z][A-Z0-9_]*(?:-[0-9]+)?(?:/[A-Z][A-Z0-9_-]*)+)$`)

// "  established 120s ago, rekeying in 13000s"
var establishedLine = regexp.MustCompile(`^\s+established (\d+)s ago`)

// Parse reads `swanctl --list-sas` output. A connection is up when an IKE SA
// of that name is ESTABLISHED and has a CHILD_SA INSTALLED; connecting while
// its IKE SA is CONNECTING or established with no CHILD_SA installed yet;
// otherwise down. With several SAs (a reauthentication) the best state wins.
// Connections with no SA at all are not listed: callers treat that as down.
func Parse(out string) map[string]string {
	states := map[string]string{}
	for conn, sa := range ParseSAs(out) {
		states[conn] = sa.State
	}
	return states
}

// ParseSAs reads `swanctl --list-sas` output as Parse does, keeping each
// connection's best SA with its ciphers and age. Between SAs in the same
// state, the newer one wins: it is the one that stays after a reauthentication.
func ParseSAs(out string) map[string]SA {
	rank := map[string]int{Down: 0, Connecting: 1, Up: 2}
	sas := map[string]SA{}
	set := func(conn string, sa SA) {
		old, ok := sas[conn]
		if !ok || rank[sa.State] > rank[old.State] ||
			(rank[sa.State] == rank[old.State] && sa.EstablishedS >= 0 &&
				(old.EstablishedS < 0 || sa.EstablishedS < old.EstablishedS)) {
			sas[conn] = sa
		}
	}
	ikeName, ikeState := "", ""
	var cur SA
	childUp, inChild := false, false
	finish := func() {
		if ikeName == "" {
			return
		}
		established := ikeState == "ESTABLISHED" || ikeState == "REKEYING"
		switch {
		case established && childUp:
			cur.State = Up
		case established || ikeState == "CONNECTING":
			cur.State = Connecting
		default:
			cur.State = Down
		}
		set(ikeName, cur)
	}
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimRight(line, "\r")
		m := saLine.FindStringSubmatch(line)
		if m == nil {
			if ikeName == "" || inChild {
				continue // a CHILD_SA's details
			}
			if a := algLine.FindStringSubmatch(line); a != nil && cur.IKECipher == "" {
				cur.IKECipher = a[1]
			} else if e := establishedLine.FindStringSubmatch(line); e != nil {
				if n, err := strconv.Atoi(e[1]); err == nil {
					cur.EstablishedS = n
				}
			}
			continue
		}
		fields := strings.Split(m[3], ", ")
		if m[1] == "" { // a new IKE SA
			finish()
			ikeName, ikeState = m[2], strings.TrimSpace(fields[0])
			cur, childUp, inChild = SA{EstablishedS: -1}, false, false
			continue
		}
		inChild = true
		st := strings.TrimSpace(fields[0])
		if strings.HasPrefix(st, "reqid ") && len(fields) > 1 {
			st = strings.TrimSpace(fields[1])
		}
		if st != "INSTALLED" && st != "REKEYING" {
			continue
		}
		childUp = true
		for _, f := range fields {
			if v, ok := strings.CutPrefix(strings.TrimSpace(f), "ESP:"); ok && cur.ESPCipher == "" {
				cur.ESPCipher = v
			}
		}
	}
	finish()
	return sas
}
