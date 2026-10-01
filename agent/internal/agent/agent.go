// Package agent is the long-running edge agent: it polls desired state,
// applies it with rollback, probes every path, and reports telemetry. It keeps
// running on the last good state while the controller is unreachable.
package agent

import (
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/zagias/exaconnect/agent/internal/apply"
	"github.com/zagias/exaconnect/agent/internal/client"
	"github.com/zagias/exaconnect/agent/internal/counters"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/frrstate"
	"github.com/zagias/exaconnect/agent/internal/probe"
	"github.com/zagias/exaconnect/agent/internal/system"
	"github.com/zagias/exaconnect/agent/internal/version"
)

type Config struct {
	PollInterval      time.Duration // desired state, default 10 s
	TelemetryInterval time.Duration // probe aggregates, default 10 s
	CounterInterval   time.Duration // byte counters, default 60 s
	SilentAfter       time.Duration // controller considered silent, default 60 s
	ProbeTimeout      time.Duration // default 2 s
	MaxBuffered       int           // telemetry windows kept while the controller is away
}

func (c *Config) defaults() {
	if c.PollInterval == 0 {
		c.PollInterval = 10 * time.Second
	}
	if c.TelemetryInterval == 0 {
		c.TelemetryInterval = 10 * time.Second
	}
	if c.CounterInterval == 0 {
		c.CounterInterval = 60 * time.Second
	}
	if c.SilentAfter == 0 {
		c.SilentAfter = 60 * time.Second
	}
	if c.ProbeTimeout == 0 {
		c.ProbeTimeout = 2 * time.Second
	}
	if c.MaxBuffered == 0 {
		c.MaxBuffered = 2000
	}
}

type Event struct {
	At     time.Time         `json:"at"`
	Kind   string            `json:"kind"`
	Detail map[string]string `json:"detail,omitempty"`
}

type TunnelState struct {
	Name          string `json:"name"`
	Path          string `json:"path"`
	HandshakeAgeS int64  `json:"handshake_age_s"`
	BFD           string `json:"bfd,omitempty"`
}

type Telemetry struct {
	At       time.Time         `json:"at"`
	Probes   []probe.Window    `json:"probes"`
	Counters []counters.Sample `json:"counters"`
	Events   []Event           `json:"events"`
	Tunnels  []TunnelState     `json:"tunnels"`
}

type Agent struct {
	Cfg     Config
	Client  *client.Client
	Applier *apply.Applier
	Sys     system.Runner
	Log     *slog.Logger

	mu          sync.Mutex
	current     *desired.State
	stats       map[string]*probe.Stats // by tunnel name
	probeCancel context.CancelFunc
	reflCancel  context.CancelFunc
	buf         Telemetry
	bfd         map[string]bfdPeer // by peer address
	lastContact time.Time
	silent      bool
	lastFailure string // version+error of the last failed apply, to avoid repeating events
}

func (a *Agent) Run(ctx context.Context) error {
	a.Cfg.defaults()
	a.stats = map[string]*probe.Stats{}
	a.bfd = map[string]bfdPeer{}
	a.lastContact = time.Now()

	// Survive restarts while the controller is away: bring back the last good state first.
	if lg := a.Applier.LastGood(); lg != nil {
		if err := a.Applier.Apply(ctx, lg); err != nil {
			a.Log.Error("re-applying last good state failed", "version", lg.Version, "err", err)
		} else {
			a.Log.Info("re-applied last good state", "version", lg.Version)
		}
		a.setCurrent(ctx, lg)
	}

	poll := time.NewTicker(a.Cfg.PollInterval)
	tele := time.NewTicker(a.Cfg.TelemetryInterval)
	cnt := time.NewTicker(a.Cfg.CounterInterval)
	bfd := time.NewTicker(2 * time.Second)
	defer poll.Stop()
	defer tele.Stop()
	defer cnt.Stop()
	defer bfd.Stop()

	a.poll(ctx)
	for {
		select {
		case <-ctx.Done():
			a.stopProbes()
			return nil
		case <-poll.C:
			a.poll(ctx)
		case <-tele.C:
			a.collectProbes()
			a.flush(ctx)
		case <-cnt.C:
			a.collectCounters()
		case <-bfd.C:
			a.checkBFD(ctx)
		}
	}
}

func (a *Agent) have() int64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.current == nil {
		return 0
	}
	return a.current.Version
}

func (a *Agent) poll(ctx context.Context) {
	ds, err := a.Client.DesiredState(ctx, a.have())
	if err != nil {
		a.controllerError(err)
		return
	}
	a.controllerOK()
	if ds == nil {
		return
	}
	a.Log.Info("new desired state", "version", ds.Version)
	st := client.Status{AppliedVersion: ds.Version, OK: true, AgentVersion: version.String()}
	if err := a.Applier.Apply(ctx, ds); err != nil {
		a.Log.Error("apply failed", "version", ds.Version, "err", err)
		st.OK, st.Error = false, err.Error()
		st.AppliedVersion = a.have()
		kind := "config_failed"
		if apply.IsRolledBack(err) {
			kind = "config_rolled_back"
		}
		if key := itoa(ds.Version) + err.Error(); key != a.lastFailure {
			a.lastFailure = key
			a.event(kind, map[string]string{"version": itoa(ds.Version), "error": err.Error()})
		}
	} else {
		a.lastFailure = ""
		a.setCurrent(ctx, ds)
		a.event("config_applied", map[string]string{"version": itoa(ds.Version)})
	}
	if err := a.Client.ReportStatus(ctx, st); err != nil {
		a.Log.Warn("report status", "err", err)
	}
}

func (a *Agent) controllerError(err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.silent && time.Since(a.lastContact) > a.Cfg.SilentAfter {
		a.silent = true
		a.Log.Warn("controller silent; holding the last good state and current paths", "since", a.lastContact.Format(time.RFC3339), "err", err)
		a.buf.Events = append(a.buf.Events, Event{At: time.Now(), Kind: "controller_silent"})
	} else if !a.silent {
		a.Log.Warn("controller unreachable", "err", err)
	}
}

func (a *Agent) controllerOK() {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.lastContact = time.Now()
	if a.silent {
		a.silent = false
		a.Log.Info("controller reachable again; reconciling")
		a.buf.Events = append(a.buf.Events, Event{At: time.Now(), Kind: "controller_back"})
	}
}

// setCurrent records the applied state and (re)starts probes and the reflector.
func (a *Agent) setCurrent(ctx context.Context, s *desired.State) {
	a.mu.Lock()
	prev := a.current
	a.current = s
	a.mu.Unlock()
	if prev == nil || probeKey(prev) != probeKey(s) {
		a.startProbes(ctx, s)
	}
	if (prev == nil || reflKey(prev) != reflKey(s)) && s.Reflector != nil {
		a.startReflector(ctx, s.Reflector.Listen)
	}
}

func probeKey(s *desired.State) string {
	var parts []string
	for _, t := range s.Tunnels {
		if t.Probe != nil {
			parts = append(parts, t.Name+"="+t.Probe.Target+"@"+itoa(int64(t.Probe.IntervalMS)))
		}
	}
	sort.Strings(parts)
	return strings.Join(parts, ",")
}

func reflKey(s *desired.State) string {
	if s.Reflector == nil {
		return ""
	}
	return s.Reflector.Listen
}

func (a *Agent) startProbes(ctx context.Context, s *desired.State) {
	a.stopProbes()
	pctx, cancel := context.WithCancel(ctx)
	a.mu.Lock()
	a.probeCancel = cancel
	stats := map[string]*probe.Stats{}
	for _, t := range s.Tunnels {
		if t.Probe == nil {
			continue
		}
		st := probe.NewStats(t.Name, a.Cfg.ProbeTimeout)
		stats[t.Name] = st
		snd := &probe.Sender{Path: t.Name, Device: t.Name, Target: t.Probe.Target,
			Interval: time.Duration(t.Probe.IntervalMS) * time.Millisecond, Stats: st}
		go func(name string) {
			// Retry: the tunnel interface may not exist yet right after apply.
			for pctx.Err() == nil {
				if err := snd.Run(pctx); err != nil {
					a.Log.Warn("probe sender", "path", name, "err", err)
				}
				select {
				case <-pctx.Done():
				case <-time.After(2 * time.Second):
				}
			}
		}(t.Name)
	}
	a.stats = stats
	a.mu.Unlock()
	a.Log.Info("probing", "paths", len(stats))
}

func (a *Agent) stopProbes() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.probeCancel != nil {
		a.probeCancel()
		a.probeCancel = nil
	}
}

func (a *Agent) startReflector(ctx context.Context, listen string) {
	if a.reflCancel != nil {
		a.reflCancel()
	}
	rctx, cancel := context.WithCancel(ctx)
	a.reflCancel = cancel
	go func() {
		for rctx.Err() == nil {
			pc, err := net.ListenPacket("udp", listen)
			if err == nil {
				a.Log.Info("probe reflector listening", "addr", listen)
				err = probe.Reflect(rctx, pc)
			}
			if err != nil {
				a.Log.Warn("reflector", "err", err)
			}
			select {
			case <-rctx.Done():
			case <-time.After(2 * time.Second):
			}
		}
	}()
}

func (a *Agent) collectProbes() {
	now := time.Now()
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, st := range a.stats {
		if w, ok := st.Collect(now); ok {
			a.buf.Probes = append(a.buf.Probes, w)
		}
	}
	if n := len(a.buf.Probes); n > a.Cfg.MaxBuffered {
		a.buf.Probes = a.buf.Probes[n-a.Cfg.MaxBuffered:]
	}
}

func (a *Agent) collectCounters() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.current == nil {
		return
	}
	var names []string
	for _, t := range a.current.Tunnels {
		names = append(names, t.Name)
		if t.Underlay != "" {
			names = append(names, t.Underlay)
		}
	}
	a.buf.Counters = append(a.buf.Counters, counters.Read(names, time.Now())...)
	if n := len(a.buf.Counters); n > a.Cfg.MaxBuffered {
		a.buf.Counters = a.buf.Counters[n-a.Cfg.MaxBuffered:]
	}
}

// bfdPeer is the last BFD state seen for one peer, and the tunnel it runs over.
type bfdPeer struct {
	Tunnel string
	Status string
}

func (a *Agent) checkBFD(ctx context.Context) {
	peers, err := frrstate.BFDPeers(ctx, a.Sys)
	if err != nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, p := range peers {
		tunnel := tunnelOf(a.current, p.Peer, p.Interface)
		if old, ok := a.bfd[p.Peer]; ok && old.Status != p.Status {
			a.buf.Events = append(a.buf.Events, Event{At: time.Now(), Kind: "bfd_" + p.Status,
				Detail: map[string]string{"peer": p.Peer, "tunnel": tunnel}})
			a.Log.Info("bfd change", "peer", p.Peer, "tunnel", tunnel, "status", p.Status)
		}
		a.bfd[p.Peer] = bfdPeer{Tunnel: tunnel, Status: p.Status}
	}
}

// tunnelOf names the tunnel a BFD peer runs over. FRR leaves the interface
// out of single-hop sessions that BGP creates, so fall back to the tunnel
// whose BGP neighbour has that address.
func tunnelOf(s *desired.State, peer, iface string) string {
	if iface != "" || s == nil {
		return iface
	}
	for _, t := range s.Tunnels {
		for _, n := range t.Neighbors {
			if n.Address == peer {
				return t.Name
			}
		}
	}
	return ""
}

// tunnelBFD is "up" when every BFD peer on the tunnel is up, otherwise the
// state of the first peer that is not; "" when the tunnel has no BFD peers.
func tunnelBFD(peers map[string]bfdPeer, tunnel string) string {
	state := ""
	for _, p := range peers {
		if p.Tunnel != tunnel {
			continue
		}
		if p.Status != "up" {
			return p.Status
		}
		state = "up"
	}
	return state
}

func (a *Agent) flush(ctx context.Context) {
	now := time.Now()
	a.mu.Lock()
	t := a.buf
	t.At = now
	if a.current != nil {
		for _, tn := range a.current.Tunnels {
			ts := TunnelState{Name: tn.Name, Path: tn.Path, HandshakeAgeS: -1}
			ts.BFD = tunnelBFD(a.bfd, tn.Name)
			t.Tunnels = append(t.Tunnels, ts)
		}
	}
	a.mu.Unlock()
	for i := range t.Tunnels {
		if age, err := frrstate.HandshakeAge(ctx, a.Sys, t.Tunnels[i].Name, now); err == nil {
			t.Tunnels[i].HandshakeAgeS = age
		}
	}
	if err := a.Client.PostTelemetry(ctx, t); err != nil {
		a.controllerError(err)
		return // keep the buffer and retry next time
	}
	a.controllerOK()
	a.mu.Lock()
	// Drop only what was sent; new items may have arrived meanwhile.
	a.buf.Probes = a.buf.Probes[len(t.Probes):]
	a.buf.Counters = a.buf.Counters[len(t.Counters):]
	a.buf.Events = a.buf.Events[len(t.Events):]
	a.mu.Unlock()
}

func (a *Agent) event(kind string, detail map[string]string) {
	a.mu.Lock()
	a.buf.Events = append(a.buf.Events, Event{At: time.Now(), Kind: kind, Detail: detail})
	a.mu.Unlock()
}

func itoa(n int64) string { b, _ := json.Marshal(n); return string(b) }
