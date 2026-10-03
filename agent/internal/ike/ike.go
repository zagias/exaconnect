// Package ike reads IKE and CHILD_SA state from strongSwan's swanctl, for
// circuit telemetry.
package ike

import (
	"context"
	"regexp"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/system"
)

const (
	Up         = "up"
	Connecting = "connecting"
	Down       = "down"
)

// States runs `swanctl --list-sas` and returns each connection's state. When
// charon cannot be asked, every connection is down: the map is empty.
func States(ctx context.Context, sys system.Runner) map[string]string {
	out, err := sys.Run(ctx, "swanctl", "--list-sas")
	if err != nil {
		return map[string]string{}
	}
	return Parse(string(out))
}

// An SA line: "<name>: #<n>, <rest>". IKE SAs start at column 0; their
// CHILD_SAs are indented under them.
var saLine = regexp.MustCompile(`^(\s*)([^\s:]+): #\d+, (.*)$`)

// Parse reads `swanctl --list-sas` output. A connection is up when an IKE SA
// of that name is ESTABLISHED and has a CHILD_SA INSTALLED; connecting while
// its IKE SA is CONNECTING or established with no CHILD_SA installed yet;
// otherwise down. With several SAs (a reauthentication) the best state wins.
// Connections with no SA at all are not listed: callers treat that as down.
func Parse(out string) map[string]string {
	rank := map[string]int{Down: 0, Connecting: 1, Up: 2}
	states := map[string]string{}
	set := func(conn, st string) {
		if old, ok := states[conn]; !ok || rank[st] > rank[old] {
			states[conn] = st
		}
	}
	ikeName, ikeState := "", ""
	childUp := false
	finish := func() {
		if ikeName == "" {
			return
		}
		switch {
		case (ikeState == "ESTABLISHED" || ikeState == "REKEYING") && childUp:
			set(ikeName, Up)
		case ikeState == "ESTABLISHED" || ikeState == "REKEYING" || ikeState == "CONNECTING":
			set(ikeName, Connecting)
		default:
			set(ikeName, Down)
		}
	}
	for _, line := range strings.Split(out, "\n") {
		m := saLine.FindStringSubmatch(strings.TrimRight(line, "\r"))
		if m == nil {
			continue
		}
		fields := strings.Split(m[3], ", ")
		if m[1] == "" { // a new IKE SA
			finish()
			ikeName, ikeState, childUp = m[2], strings.TrimSpace(fields[0]), false
			continue
		}
		st := strings.TrimSpace(fields[0])
		if strings.HasPrefix(st, "reqid ") && len(fields) > 1 {
			st = strings.TrimSpace(fields[1])
		}
		if st == "INSTALLED" || st == "REKEYING" {
			childUp = true
		}
	}
	finish()
	return states
}
