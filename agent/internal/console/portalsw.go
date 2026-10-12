package console

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// Fleet software updates from the Connect portal (ADR 0054). While the box is
// managed from the cloud or both, a version asked for in the portal arrives
// with desired state and the box installs it with its own A/B update (ADR
// 0052): it restarts into the new software and keeps it only if it comes up
// healthy, otherwise it goes back to the old one by itself.

const portalSWFile = "console-software.json"

// SoftwareRequest is the last version the portal asked for and what became of it.
type SoftwareRequest struct {
	Version string    `json:"version"`
	At      time.Time `json:"at"`
	By      string    `json:"by,omitempty"`
	// State: installing | done | rolled_back | failed | refused | current
	State  string `json:"state"`
	Detail string `json:"detail,omitempty"`
	// Started is when the box started the install, on its own clock.
	Started time.Time `json:"started,omitempty"`
}

func loadSoftwareRequest(path string) (SoftwareRequest, error) {
	var r SoftwareRequest
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return r, nil
	}
	if err != nil {
		return r, err
	}
	return r, json.Unmarshal(b, &r)
}

func (s *Server) swRequest() SoftwareRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.swReq
}

func (s *Server) setSWRequest(r SoftwareRequest) {
	s.mu.Lock()
	s.swReq = r
	s.mu.Unlock()
	b, _ := json.Marshal(r)
	tmp := filepath.Join(s.Dir, portalSWFile+".tmp")
	if err := os.WriteFile(tmp, b, 0o600); err == nil {
		_ = os.Rename(tmp, filepath.Join(s.Dir, portalSWFile))
	}
}

// runningVersion is the box software version running now.
func (s *Server) runningVersion(fresh bool) string {
	if !s.hasUpdater() {
		return ""
	}
	return s.updateStatus(context.Background(), fresh).Version
}

// fromPortalSoftware starts the update the portal asked for, once.
func (s *Server) fromPortalSoftware(sw *desired.BoxSoftware) {
	m := s.managed()
	if sw == nil || sw.Version == "" || m.Mode == ManagedSite {
		return
	}
	// Newer than the last one handled, and than the last "Managed from" switch.
	at := sw.At.UTC()
	if !at.After(s.swRequest().At) || !at.After(m.At) {
		return
	}
	r := SoftwareRequest{Version: sw.Version, At: at, By: sw.By}
	switch {
	case !s.hasUpdater():
		r.State, r.Detail = "refused", "this box was not installed from the ExaCarib box image; it updates with apt"
	case s.runningVersion(true) == sw.Version:
		r.State = "current"
	default:
		by := sw.By
		if by == "" {
			by = "Connect portal"
		}
		if err := s.StartUpdate(Release{Version: sw.Version}, by); err != nil {
			r.State, r.Detail = "refused", err.Error()
		} else {
			r.State, r.Started = "installing", time.Now().UTC()
		}
	}
	s.setSWRequest(r)
	if r.State == "refused" {
		s.Log.Warn("software update from the portal refused", "version", r.Version, "why", r.Detail)
		s.report("box_update_refused", map[string]string{"version": r.Version, "why": r.Detail})
	}
}

// checkSoftwareRequest settles an update from the portal: after the restart
// the box runs the new version (done) or went back to the old one (rolled
// back); before it, the install itself may have failed.
func (s *Server) checkSoftwareRequest() {
	r := s.swRequest()
	if r.State != "installing" {
		return
	}
	u := &s.upd
	u.mu.Lock()
	running, failed := u.running, u.err
	u.mu.Unlock()
	switch {
	case running:
		return
	case failed != "":
		r.State, r.Detail = "failed", failed
	default:
		v := s.runningVersion(false)
		if v == "" {
			return
		}
		if v == r.Version {
			r.State, r.Detail = "done", ""
		} else if s.started.After(r.Started) && time.Since(s.started) > 3*time.Minute {
			// Restarted, and still on the old version after the health check: the box went back.
			r.State, r.Detail = "rolled_back", "the new software did not come up healthy, so the box went back to "+v
		} else {
			return
		}
	}
	s.setSWRequest(r)
}
