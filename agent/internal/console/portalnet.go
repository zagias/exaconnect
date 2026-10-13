package console

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// Ports set in the Connect portal (ADR 0051). While the box is managed from
// the cloud or both, a port set-up made in the portal arrives with desired
// state. The box puts it on its ports with the same safety as a change made
// in the console: it keeps the change only once it can still reach ExaCarib
// with it, and otherwise puts the ports back after RevertAfter (2 minutes).

const portalFile = "console-portal.json"

// PortalChange is the last port set-up that came from the portal and what
// became of it. It is saved, so a change the box undid is not tried again
// after a restart.
type PortalChange struct {
	At     time.Time `json:"at"`
	By     string    `json:"by,omitempty"`
	State  string    `json:"state"` // trying | kept | undone | refused
	Detail string    `json:"detail,omitempty"`
}

func loadPortal(path string) (PortalChange, error) {
	var p PortalChange
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return p, nil
	}
	if err != nil {
		return p, err
	}
	return p, json.Unmarshal(b, &p)
}

func (s *Server) setPortal(p PortalChange) {
	// Saved before it shows, so whoever sees the new state finds it on disk too.
	b, _ := json.Marshal(p)
	tmp := filepath.Join(s.Dir, portalFile+".tmp")
	if err := os.WriteFile(tmp, b, 0o600); err == nil {
		_ = os.Rename(tmp, filepath.Join(s.Dir, portalFile))
	}
	s.mu.Lock()
	s.portal = p
	s.mu.Unlock()
}

// reach checks the box can still get to ExaCarib: a connection to the
// controller address it joined with.
func (s *Server) reach(ctx context.Context) error {
	if s.Reach != nil {
		return s.Reach(ctx)
	}
	_, ctrl, ok := s.identity()
	if !ok {
		return errors.New("not joined")
	}
	u, err := url.Parse(ctrl)
	if err != nil || u.Host == "" {
		return errors.New("no controller address")
	}
	host := u.Host
	if u.Port() == "" {
		host = net.JoinHostPort(u.Hostname(), "443")
	}
	var d net.Dialer
	c, err := d.DialContext(ctx, "tcp", host)
	if err != nil {
		return err
	}
	return c.Close()
}

// fromPortalNetwork takes a port set-up from the portal when it is newer than
// the box's own ports, than the last portal set-up it handled, and than the
// last "Managed from" switch (switching keeps the box's set-up as it is).
func (s *Server) fromPortalNetwork(bn *desired.BoxNetwork) {
	m := s.managed()
	if bn == nil || m.Mode == ManagedSite {
		return
	}
	at := bn.At.UTC()
	s.mu.Lock()
	last := s.portal.At
	var netAt time.Time
	var old *Network
	if s.net != nil {
		netAt = s.net.UpdatedAt
		c := *s.net
		old = &c
	}
	s.mu.Unlock()
	if !at.After(last) || !at.After(netAt) || !at.After(m.At) {
		return
	}
	var n Network
	err := json.Unmarshal(bn.Network, &n)
	if err == nil {
		if old != nil {
			n.keepPasswords(*old)
		}
		_, have := s.ports()
		err = n.Validate(have)
	}
	if err != nil {
		s.Log.Warn("ports from the portal refused", "err", err)
		s.setPortal(PortalChange{At: at, By: bn.By, State: "refused", Detail: err.Error()})
		s.report("box_portal_refused", map[string]string{"by": bn.By, "why": err.Error()})
		return
	}
	s.setPortal(PortalChange{At: at, By: bn.By, State: "trying"})
	go s.applyFromPortal(n, at, bn.By)
}

func (s *Server) applyFromPortal(n Network, at time.Time, by string) {
	s.applyMu.Lock()
	defer s.applyMu.Unlock()
	s.mu.Lock()
	if !s.portal.At.Equal(at) {
		// A newer one from the portal is on its way.
		s.mu.Unlock()
		return
	}
	old := s.net
	s.mu.Unlock()
	n.UpdatedAt, n.UpdatedBy = at, by
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if err := s.Backend.Apply(ctx, n); err != nil {
		s.Log.Warn("ports from the portal failed", "err", err)
		if old != nil {
			if rerr := s.Backend.Apply(context.Background(), *old); rerr != nil {
				s.Log.Error("putting the ports back failed", "err", rerr)
			}
		}
		s.setPortal(PortalChange{At: at, By: by, State: "refused", Detail: "the ports were not changed: " + err.Error()})
		s.report("box_portal_refused", map[string]string{"by": by, "why": err.Error()})
		return
	}
	if err := saveNetwork(filepath.Join(s.Dir, networkFile), &n); err != nil {
		s.Log.Error("saving the ports from the portal", "err", err)
	}
	s.mu.Lock()
	if s.revert == nil {
		s.prev = old
	} else {
		s.revert.Stop()
	}
	s.net = &n
	s.revertAt = time.Now().Add(s.RevertAfter)
	s.gen++
	gen := s.gen
	s.portalGen = gen
	s.revert = time.AfterFunc(s.RevertAfter, func() { s.undo(gen) })
	deadline := s.revertAt
	s.mu.Unlock()
	s.Log.Info("ports from the portal applied; checking ExaCarib can still be reached", "by", by)
	go s.keepIfReachable(gen, deadline, n, by)
}

// keepIfReachable confirms a portal change once the box reaches ExaCarib
// with it. If it never does, the revert timer puts the ports back.
func (s *Server) keepIfReachable(gen int, deadline time.Time, n Network, by string) {
	every := s.ReachEvery
	if every == 0 {
		every = 3 * time.Second
	}
	for time.Now().Add(every).Before(deadline) {
		time.Sleep(every)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		err := s.reach(ctx)
		cancel()
		if err != nil {
			continue
		}
		s.mu.Lock()
		if s.revert == nil || s.gen != gen {
			s.mu.Unlock()
			return
		}
		s.revert.Stop()
		s.revert, s.prev, s.portalGen = nil, nil, 0
		at := s.portal.At
		c := &Change{At: time.Now().UTC(), By: "Connect portal", What: describe(&n)}
		if by != "" {
			c.By = by
		}
		s.lastChange = c
		s.mu.Unlock()
		s.setPortal(PortalChange{At: at, By: by, State: "kept"})
		s.Log.Info("ports from the portal kept", "by", by)
		s.report("box_portal_applied", map[string]string{"by": by, "what": c.What})
		return
	}
}

// portalUndone is called by undo when the change it put back came from the portal.
func (s *Server) portalUndone() {
	s.mu.Lock()
	p := s.portal
	s.mu.Unlock()
	p.State = "undone"
	p.Detail = "the box could not reach ExaCarib with these ports, so it put the earlier ones back"
	s.setPortal(p)
	s.report("box_portal_undone", map[string]string{"by": p.By, "why": p.Detail})
}

func (s *Server) portalState() PortalChange {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.portal
}
