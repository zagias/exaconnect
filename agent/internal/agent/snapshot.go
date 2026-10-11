package agent

import (
	"time"

	"github.com/zagias/exaconnect/agent/internal/probe"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

// Snapshot is the agent's state for the box console (ADR 0046): what it
// applied, when the controller last answered, and each tunnel's health.
type Snapshot struct {
	LastContact    time.Time       `json:"last_contact"`
	Silent         bool            `json:"silent"`
	AppliedVersion int64           `json:"applied_version"`
	Role           string          `json:"role,omitempty"`
	Tunnels        []TunnelView    `json:"tunnels"`
	Steering       []steer.Choice  `json:"steering"`
	LastError      string          `json:"last_error,omitempty"`
	Neighbors      []NeighborOfRef `json:"-"`
}

// TunnelView is one tunnel with its latest probe window.
type TunnelView struct {
	TunnelState
	Underlay string        `json:"underlay,omitempty"`
	Probe    *probe.Window `json:"probe,omitempty"`
}

// NeighborOfRef is a BGP neighbour and the tunnel it runs over.
type NeighborOfRef struct {
	Tunnel  string
	Address string
}

// Snapshot is safe to call from any goroutine while Run is going.
func (a *Agent) Snapshot() Snapshot {
	a.mu.Lock()
	defer a.mu.Unlock()
	s := Snapshot{LastContact: a.lastContact, Silent: a.silent,
		Steering: append([]steer.Choice(nil), a.choices...), LastError: a.applyErr}
	if a.current == nil {
		return s
	}
	s.AppliedVersion, s.Role = a.current.Version, string(a.current.Role)
	for _, t := range a.current.Tunnels {
		v := TunnelView{TunnelState: TunnelState{Name: t.Name, Path: t.Path, HandshakeAgeS: -1,
			BFD: tunnelBFD(a.bfd, t.Name)}, Underlay: t.Underlay}
		if w, ok := a.slaWin[t.Name]; ok {
			w := w
			v.Probe = &w
		}
		s.Tunnels = append(s.Tunnels, v)
		for _, n := range t.Neighbors {
			s.Neighbors = append(s.Neighbors, NeighborOfRef{Tunnel: t.Name, Address: n.Address})
		}
	}
	return s
}
