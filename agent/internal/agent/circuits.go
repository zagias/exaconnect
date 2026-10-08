package agent

import (
	"context"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/zagias/exaconnect/agent/internal/counters"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/frrstate"
	"github.com/zagias/exaconnect/agent/internal/ike"
	"github.com/zagias/exaconnect/agent/internal/probe"
)

// MaxCircuitRoutes is how many accepted prefixes a circuit reports.
const MaxCircuitRoutes = 20

// CircuitState is one circuit in telemetry (docs/fabric-contract.md).
type CircuitState struct {
	ID               int      `json:"id"`
	Name             string   `json:"name"`
	IKE              string   `json:"ike"` // up | connecting | down; "" for layer 2
	BGP              string   `json:"bgp"` // FRR's state string; "" for layer 2
	PrefixesReceived int      `json:"prefixes_received"`
	Routes           []string `json:"routes"`
	// Probes since the last flush (layer 2); 0, 0 and null for cloud circuits.
	Sent     int      `json:"sent"`
	Received int      `json:"received"`
	RTTMS    *float64 `json:"rtt_ms"`
	BytesIn  uint64   `json:"bytes_in"`
	BytesOut uint64   `json:"bytes_out"`
	// The IKE and ESP algorithms and the IKE SA's age (cloud circuits, ADR
	// 0012); "", "" and -1 with no SA, and for layer 2.
	IKECipher    string `json:"ike_cipher"`
	ESPCipher    string `json:"esp_cipher"`
	EstablishedS int    `json:"established_s"`
}

func circuitProbeKey(s *desired.State) string {
	var parts []string
	for _, c := range s.L2Circuits {
		if c.Probe != nil {
			parts = append(parts, c.Name+"="+c.Probe.Target+"@"+strconv.Itoa(c.Probe.IntervalMS)+"<"+s.LoopbackAddr())
		}
	}
	sort.Strings(parts)
	return strings.Join(parts, ",")
}

// startCircuitProbes probes each layer 2 circuit's far end from this node's
// loopback, the address the far end routes back to. These stats are kept
// apart from the per-path ones.
func (a *Agent) startCircuitProbes(ctx context.Context, s *desired.State) {
	a.stopCircuitProbes()
	pctx, cancel := context.WithCancel(ctx)
	stats := map[string]*probe.Stats{}
	for _, c := range s.L2Circuits {
		if c.Probe == nil {
			continue
		}
		interval := time.Duration(c.Probe.IntervalMS) * time.Millisecond
		if interval <= 0 {
			interval = time.Second
		}
		st := probe.NewStats(c.Name, a.Cfg.ProbeTimeout)
		stats[c.Name] = st
		snd := &probe.Sender{Path: c.Name, Source: s.LoopbackAddr(), Target: c.Probe.Target, Interval: interval, Stats: st}
		go func(name string) {
			for pctx.Err() == nil {
				if err := snd.Run(pctx); err != nil {
					a.Log.Warn("circuit probe sender", "circuit", name, "err", err)
				}
				select {
				case <-pctx.Done():
				case <-time.After(2 * time.Second):
				}
			}
		}(c.Name)
	}
	a.mu.Lock()
	a.circCancel = cancel
	a.circStats = stats
	a.mu.Unlock()
	if len(stats) > 0 {
		a.Log.Info("probing circuits", "circuits", len(stats))
	}
}

func (a *Agent) stopCircuitProbes() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.circCancel != nil {
		a.circCancel()
		a.circCancel = nil
	}
}

// circuitStates reads every circuit's state for one flush: IKE from swanctl,
// BGP from FRR, probes since the last flush and the interface counters.
func (a *Agent) circuitStates(ctx context.Context, s *desired.State, now time.Time) []CircuitState {
	if s == nil {
		return nil
	}
	var out []CircuitState
	if cloud := s.ActiveCircuits(); len(cloud) > 0 {
		sas := ike.SAs(ctx, a.Sys)
		for _, c := range cloud {
			sa, ok := sas[c.Conn()]
			if !ok {
				sa = ike.NoSA
			}
			cs := CircuitState{ID: c.ID, Name: c.Name, IKE: sa.State, Routes: []string{},
				IKECipher: sa.IKECipher, ESPCipher: sa.ESPCipher, EstablishedS: sa.EstablishedS}
			if n, err := frrstate.BGPNeighbor(ctx, a.Sys, c.PeerInside); err == nil {
				cs.BGP, cs.PrefixesReceived = n.State, n.PrefixesReceived
			}
			if cs.PrefixesReceived > 0 {
				if r, err := frrstate.NeighborRoutes(ctx, a.Sys, c.PeerInside, MaxCircuitRoutes); err == nil {
					cs.Routes = r
				}
			}
			cs.BytesIn, cs.BytesOut = bytesOf(c.Name, now)
			out = append(out, cs)
		}
	}
	a.mu.Lock()
	stats := a.circStats
	a.mu.Unlock()
	for _, c := range s.L2Circuits {
		cs := CircuitState{ID: c.ID, Name: c.Name, Routes: []string{}, EstablishedS: -1}
		if st := stats[c.Name]; st != nil {
			if w, ok := st.Collect(now); ok {
				cs.Sent, cs.Received = w.Sent, w.Received
				if w.Received > 0 {
					rtt := w.RTTAvgMS
					cs.RTTMS = &rtt
				}
			}
		}
		cs.BytesIn, cs.BytesOut = bytesOf(c.Name, now)
		out = append(out, cs)
	}
	return out
}

func bytesOf(dev string, now time.Time) (in, out uint64) {
	if s := counters.Read([]string{dev}, now); len(s) == 1 {
		return s[0].RxBytes, s[0].TxBytes
	}
	return 0, 0
}
