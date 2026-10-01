// Package agent is the long-running edge agent: it polls desired state,
// applies it with rollback, probes every path, and reports telemetry. It keeps
// running on the last good state while the controller is unreachable.
package agent

import (
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/zagias/exaconnect/agent/internal/apply"
	"github.com/zagias/exaconnect/agent/internal/client"
	"github.com/zagias/exaconnect/agent/internal/counters"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/flows"
	"github.com/zagias/exaconnect/agent/internal/frrstate"
	"github.com/zagias/exaconnect/agent/internal/probe"
	"github.com/zagias/exaconnect/agent/internal/steer"
	"github.com/zagias/exaconnect/agent/internal/system"
	"github.com/zagias/exaconnect/agent/internal/version"
)

type Config struct {
	PollInterval      time.Duration // desired state, default 10 s
	TelemetryInterval time.Duration // probe aggregates, default 10 s
	CounterInterval   time.Duration // byte counters and flows, default 60 s
	SilentAfter       time.Duration // controller considered silent, default 60 s
	SteerInterval     time.Duration // steering map poll, default 5 s
	BFDInterval       time.Duration // BFD state check, default 500 ms
	ProbeTimeout      time.Duration // default 2 s
	ResolveInterval   time.Duration // re-resolve match domains, retry failed QoS; default 5 min
	MaxBuffered       int           // telemetry windows kept while the controller is away
}

// MaxFlows is how many flow aggregates one conntrack read reports.
const MaxFlows = 100

const (
	conntrackProc = "/proc/net/nf_conntrack"
	conntrackAcct = "/proc/sys/net/netfilter/nf_conntrack_acct"
)

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
	if c.SteerInterval == 0 {
		c.SteerInterval = 5 * time.Second
	}
	if c.BFDInterval == 0 {
		c.BFDInterval = 500 * time.Millisecond
	}
	if c.ProbeTimeout == 0 {
		c.ProbeTimeout = 2 * time.Second
	}
	if c.ResolveInterval == 0 {
		c.ResolveInterval = 5 * time.Minute
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
	Flows    []flows.Flow      `json:"flows,omitempty"`
	Events   []Event           `json:"events"`
	Tunnels  []TunnelState     `json:"tunnels"`
	// Steering is where each class is right now, against SteeringVersion.
	Steering        []steer.Choice `json:"steering,omitempty"`
	SteeringVersion int64          `json:"steering_version,omitempty"`
}

type Agent struct {
	Cfg     Config
	Client  *client.Client
	Applier *apply.Applier
	Steerer *steer.Steerer // nil disables steering
	Sys     system.Runner
	Log     *slog.Logger

	mu          sync.Mutex
	current     *desired.State
	stats       map[string]*probe.Stats // by tunnel name
	probeCancel context.CancelFunc
	reflCancel  context.CancelFunc
	buf         Telemetry
	bfd         map[string]bfdPeer // by peer address
	bfdErr      string
	lastContact time.Time
	silent      bool
	lastFailure string // version+error of the last failed apply, to avoid repeating events
	steerMap    *steer.Map
	choices     []steer.Choice
	steerErr    string
	qosErr      string
	resolveErr  string
	kick        chan struct{} // new steering map: look up its domains
	refreshed   chan struct{} // lookups changed: re-apply the classifier
	flows       flows.Tracker
	flowErr     string
	// writeProc writes a /proc/sys file; nil uses os.WriteFile (tests stub it).
	writeProc func(path string, data []byte) error
}

func (a *Agent) Run(ctx context.Context) error {
	a.Cfg.defaults()
	a.stats = map[string]*probe.Stats{}
	a.bfd = map[string]bfdPeer{}
	a.lastContact = time.Now()
	a.kick = make(chan struct{}, 1)
	a.refreshed = make(chan struct{}, 1)

	// Flow telemetry needs per-flow byte counters; best effort.
	if a.writeProc == nil {
		a.writeProc = func(p string, d []byte) error { return os.WriteFile(p, d, 0o644) }
	}
	if err := a.writeProc(conntrackAcct, []byte("1\n")); err != nil {
		a.Log.Debug("cannot enable conntrack accounting", "err", err)
	}

	// Survive restarts while the controller is away: bring back the last good state first.
	if lg := a.Applier.LastGood(); lg != nil {
		if err := a.Applier.Apply(ctx, lg); err != nil {
			a.Log.Error("re-applying last good state failed", "version", lg.Version, "err", err)
		} else {
			a.Log.Info("re-applied last good state", "version", lg.Version)
		}
		a.setCurrent(ctx, lg)
	}
	if a.Steerer != nil {
		go a.lookupLoop(ctx)
		if m, err := steer.Load(a.steerPath()); err == nil {
			a.mu.Lock()
			a.steerMap = m
			a.mu.Unlock()
			a.Log.Info("loaded last steering map", "version", m.Version)
			a.kickLookups()
		}
		a.checkBFD(ctx)
		a.steer(ctx, "start")
	}

	poll := time.NewTicker(a.Cfg.PollInterval)
	tele := time.NewTicker(a.Cfg.TelemetryInterval)
	cnt := time.NewTicker(a.Cfg.CounterInterval)
	bfd := time.NewTicker(a.Cfg.BFDInterval)
	str := time.NewTicker(a.Cfg.SteerInterval)
	qos := time.NewTicker(a.Cfg.ResolveInterval)
	defer str.Stop()
	defer qos.Stop()
	defer poll.Stop()
	defer tele.Stop()
	defer cnt.Stop()
	defer bfd.Stop()

	a.poll(ctx)
	a.pollSteering(ctx)
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
			a.collectFlows(ctx)
		case <-bfd.C:
			if a.checkBFD(ctx) {
				a.steer(ctx, "bfd")
			}
		case <-str.C:
			a.pollSteering(ctx)
		case <-a.refreshed:
			a.steer(ctx, "resolve")
		case <-qos.C:
			if a.Steerer != nil && a.Steerer.RetryQoS() {
				a.steer(ctx, "qos")
			}
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
	if prev != nil {
		if a.Steerer != nil {
			a.Steerer.RetryQoS() // a tunnel tc failed on may exist now
		}
		a.steer(ctx, "config")
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

func (a *Agent) checkBFD(ctx context.Context) (changed bool) {
	peers, err := frrstate.BFDPeers(ctx, a.Sys)
	a.mu.Lock()
	defer a.mu.Unlock()
	if err != nil {
		// Log each distinct failure once; this runs twice a second.
		if msg := err.Error(); msg != a.bfdErr {
			a.bfdErr = msg
			a.Log.Warn("cannot read BFD state", "err", err)
		}
		return false
	}
	a.bfdErr = ""
	for _, p := range peers {
		tunnel := tunnelOf(a.current, p.Peer, p.Interface)
		old, ok := a.bfd[p.Peer]
		if ok && old.Status != p.Status {
			a.buf.Events = append(a.buf.Events, Event{At: time.Now(), Kind: "bfd_" + p.Status,
				Detail: map[string]string{"peer": p.Peer, "tunnel": tunnel}})
			a.Log.Info("bfd change", "peer", p.Peer, "tunnel", tunnel, "status", p.Status)
		}
		if !ok || old.Status != p.Status || old.Tunnel != tunnel {
			changed = true
		}
		a.bfd[p.Peer] = bfdPeer{Tunnel: tunnel, Status: p.Status}
	}
	return changed
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
	if a.steerMap != nil {
		t.Steering, t.SteeringVersion = a.choices, a.steerMap.Version
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
	a.buf.Flows = a.buf.Flows[len(t.Flows):]
	a.buf.Events = a.buf.Events[len(t.Events):]
	a.mu.Unlock()
}

func (a *Agent) event(kind string, detail map[string]string) {
	a.mu.Lock()
	a.buf.Events = append(a.buf.Events, Event{At: time.Now(), Kind: kind, Detail: detail})
	a.mu.Unlock()
}

func itoa(n int64) string { b, _ := json.Marshal(n); return string(b) }

func (a *Agent) steerPath() string { return filepath.Join(a.Applier.StateDir, "steering.json") }

// pollSteering fetches a new steering map, keeps it on disk for restarts and
// controller outages, and applies it.
func (a *Agent) pollSteering(ctx context.Context) {
	if a.Steerer == nil {
		return
	}
	a.mu.Lock()
	var have int64
	if a.steerMap != nil {
		have = a.steerMap.Version
	}
	a.mu.Unlock()
	m, err := a.Client.Steering(ctx, have)
	if err != nil {
		a.controllerError(err)
		return
	}
	a.controllerOK()
	if m == nil {
		return
	}
	if err := m.Validate(); err != nil {
		a.Log.Error("rejected steering map", "version", m.Version, "err", err)
		a.event("steering_rejected", map[string]string{"version": itoa(m.Version), "error": err.Error()})
		return
	}
	if b, err := json.Marshal(m); err == nil {
		if err := a.Sys.WriteFile(a.steerPath(), b, 0o600); err != nil {
			a.Log.Warn("save steering map", "err", err)
		}
	}
	a.mu.Lock()
	a.steerMap = m
	a.mu.Unlock()
	a.Log.Info("new steering map", "version", m.Version, "storm", m.Storm)
	a.kickLookups()
	a.steer(ctx, "controller")
}

func (a *Agent) kickLookups() {
	select {
	case a.kick <- struct{}{}:
	default: // one is already pending; it reads the latest map
	}
}

// lookupLoop resolves match domains and re-reads VLAN subinterfaces off the
// main loop, so a slow resolver never delays a BFD failover: new domains as
// soon as a map arrives, all of them every ResolveInterval. A change wakes
// the main loop to re-apply the classifier.
func (a *Agent) lookupLoop(ctx context.Context) {
	t := time.NewTicker(a.Cfg.ResolveInterval)
	defer t.Stop()
	for {
		all := false
		select {
		case <-ctx.Done():
			return
		case <-a.kick:
		case <-t.C:
			all = true
		}
		a.mu.Lock()
		m := a.steerMap
		a.mu.Unlock()
		if m == nil {
			continue
		}
		changed, err := a.Steerer.Refresh(ctx, m, all)
		msg := ""
		if err != nil {
			msg = err.Error()
		}
		a.mu.Lock()
		if msg != a.resolveErr && msg != "" {
			a.Log.Warn("match lookups", "err", msg)
		}
		a.resolveErr = msg
		a.mu.Unlock()
		if changed {
			select {
			case a.refreshed <- struct{}{}:
			default:
			}
		}
	}
}

// collectFlows reads conntrack on a site and buffers what its LAN sent to
// each destination since the last read, by class.
func (a *Agent) collectFlows(ctx context.Context) {
	a.mu.Lock()
	cur, m := a.current, a.steerMap
	a.mu.Unlock()
	if cur == nil || cur.Role != desired.RoleSite || m == nil {
		return
	}
	var out []byte
	var err error
	if a.Sys.Exists(conntrackProc) {
		out, err = a.Sys.ReadFile(conntrackProc)
	} else {
		out, err = a.Sys.Run(ctx, "conntrack", "-L", "-o", "extended")
	}
	if err != nil {
		if msg := err.Error(); msg != a.flowErr {
			a.flowErr = msg
			a.Log.Warn("cannot read conntrack", "err", err)
		}
		return
	}
	a.flowErr = ""
	var local []netip.Prefix
	for _, s := range m.LocalPrefixes {
		if p, err := netip.ParsePrefix(s); err == nil {
			local = append(local, p)
		}
	}
	classOf := map[uint32]string{}
	for _, c := range m.Classes {
		classOf[uint32(c.Mark)] = c.Name
	}
	fl := a.flows.Update(flows.Parse(out), local, classOf, time.Now(), MaxFlows)
	if len(fl) == 0 {
		return
	}
	a.mu.Lock()
	a.buf.Flows = append(a.buf.Flows, fl...)
	if n := len(a.buf.Flows); n > a.Cfg.MaxBuffered {
		a.buf.Flows = a.buf.Flows[n-a.Cfg.MaxBuffered:]
	}
	a.mu.Unlock()
}

// usable reports whether a path can carry traffic: its tunnel exists and BFD
// does not say it is down. Unknown BFD state counts as up, so a fresh start
// steers as told until BFD has an opinion; so does "init", the brief handshake
// state a session passes through on its way up.
func (a *Agent) usable(m *steer.Map) func(string) bool {
	tunnels := map[string]bool{}
	if a.current != nil {
		for _, t := range a.current.Tunnels {
			tunnels[t.Name] = true
		}
	}
	tunnelOfPath := map[string]string{}
	for _, p := range m.Paths {
		tunnelOfPath[p.Name] = p.Tunnel
	}
	return func(path string) bool {
		tn := tunnelOfPath[path]
		if !tunnels[tn] {
			return false
		}
		st := tunnelBFD(a.bfd, tn)
		return st == "" || st == "up" || st == "init"
	}
}

// steer re-evaluates every class against path health and applies the result.
func (a *Agent) steer(ctx context.Context, why string) {
	if a.Steerer == nil {
		return
	}
	a.mu.Lock()
	m := a.steerMap
	// Until desired state is loaded no tunnel is known, and every class would
	// look pathless (bulk would pause). Wait for it.
	if m == nil || a.current == nil {
		a.mu.Unlock()
		return
	}
	choices := steer.Choose(m, a.usable(m))
	prev := a.choices
	a.mu.Unlock()

	if err := a.Steerer.Apply(ctx, m, choices); err != nil {
		if msg := err.Error(); msg != a.steerErr {
			a.steerErr = msg
			a.Log.Error("apply steering", "why", why, "err", err)
			a.event("steering_failed", map[string]string{"error": msg})
		}
		return
	}
	a.steerErr = ""
	if msg := a.Steerer.QoSError(); msg != a.qosErr {
		a.qosErr = msg
		if msg != "" {
			a.Log.Warn("qos not applied; steering continues without it", "err", msg)
			a.event("qos_failed", map[string]string{"error": msg})
		}
	}
	a.mu.Lock()
	a.choices = choices
	for _, ev := range moves(prev, choices, why) {
		a.buf.Events = append(a.buf.Events, ev)
		a.Log.Info("class moved", "class", ev.Detail["class"], "dst", ev.Detail["dst"], "from", ev.Detail["from"], "to", ev.Detail["to"], "why", why)
	}
	a.mu.Unlock()
}

// moves lists the classes whose path changed between two evaluations.
func moves(prev, next []steer.Choice, why string) []Event {
	where := func(c steer.Choice) string {
		switch {
		case c.Path != "":
			return c.Path
		case c.Paused:
			return "paused"
		default:
			return "bgp"
		}
	}
	old := map[string]string{}
	for _, c := range prev {
		old[c.Class+"|"+c.Dst] = where(c)
	}
	var out []Event
	for _, c := range next {
		from, ok := old[c.Class+"|"+c.Dst]
		to := where(c)
		if ok && from == to {
			continue
		}
		if !ok {
			from = "none"
		}
		d := map[string]string{"class": c.Class, "from": from, "to": to, "why": why}
		if c.Dst != "" {
			d["dst"] = c.Dst
		}
		if c.Failover {
			d["failover"] = "true"
		}
		out = append(out, Event{At: time.Now(), Kind: "class_moved", Detail: d})
	}
	return out
}
