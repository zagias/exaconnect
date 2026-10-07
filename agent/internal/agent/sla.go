package agent

import (
	"context"
	"sort"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/probe"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

// SLARestoreWindows is how many good 10 s probe windows in a row bring a
// demoted path back for a class.
const SLARestoreWindows = 3

// slaState is the agent's own SLA view of one class on one path: demoted on
// a breaching window, restored after SLARestoreWindows good ones.
type slaState struct {
	breached bool
	good     int
}

func slaKey(class, path string) string { return class + "|" + path }

// updateSLA folds the windows just collected (by tunnel) into each class's
// view of each path. It runs whether or not the controller is reachable, so
// the view is current the moment the controller goes silent. Caller holds a.mu.
func (a *Agent) updateSLA(fresh map[string]probe.Window) {
	m := a.steerMap
	if m == nil {
		return
	}
	if a.sla == nil {
		a.sla = map[string]*slaState{}
	}
	if a.slaWin == nil {
		a.slaWin = map[string]probe.Window{}
	}
	for tunnel, w := range fresh {
		a.slaWin[tunnel] = w
	}
	for _, c := range m.Classes {
		if c.SLA == nil {
			continue
		}
		for _, p := range m.Paths {
			w, ok := fresh[p.Tunnel]
			if !ok || w.Sent == 0 {
				continue // no news: keep the current view
			}
			k := slaKey(c.Name, p.Name)
			st := a.sla[k]
			if st == nil {
				st = &slaState{}
				a.sla[k] = st
			}
			if c.SLA.Breached(w.RTTAvgMS, w.JitterMS, w.LossPct, w.Received > 0) {
				if !st.breached {
					a.Log.Info("path breaches class SLA", "class", c.Name, "path", p.Name,
						"latency_ms", w.RTTAvgMS, "jitter_ms", w.JitterMS, "loss_pct", w.LossPct)
				}
				st.breached, st.good = true, 0
				continue
			}
			if st.breached {
				st.good++
				if st.good >= SLARestoreWindows {
					st.breached, st.good = false, 0
				}
			}
		}
	}
}

// slaHealthy reports whether a class may use a path by the agent's own SLA
// view. Caller holds a.mu.
func (a *Agent) slaHealthy(class, path string) bool {
	st := a.sla[slaKey(class, path)]
	return st == nil || !st.breached
}

// slaSeverity ranks breaching paths by their latest window, so a class with
// nowhere healthy to go keeps the least bad one. Caller holds a.mu.
func (a *Agent) slaSeverity(m *steer.Map) func(class, path string) float64 {
	slaOf := map[string]*steer.SLA{}
	for _, c := range m.Classes {
		slaOf[c.Name] = c.SLA
	}
	tunnelOf := map[string]string{}
	for _, p := range m.Paths {
		tunnelOf[p.Name] = p.Tunnel
	}
	return func(class, path string) float64 {
		w, ok := a.slaWin[tunnelOf[path]]
		if !ok {
			return 0
		}
		return slaOf[class].Severity(w.RTTAvgMS, w.JitterMS, w.LossPct, w.Received > 0)
	}
}

// slaSignature names the demotions in force: none while the controller is
// heard, since it does the steering then. Caller holds a.mu.
func (a *Agent) slaSignature() string {
	if !a.silent {
		return ""
	}
	var keys []string
	for k, st := range a.sla {
		if st.breached {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return strings.Join(keys, ",")
}

// slaSteer re-steers when the demotions in force changed: a path breached
// or recovered while the controller is silent, or the controller went
// silent or came back.
func (a *Agent) slaSteer(ctx context.Context) {
	a.mu.Lock()
	changed := a.slaSignature() != a.slaSig
	a.mu.Unlock()
	if changed {
		a.steer(ctx, "sla")
	}
}
