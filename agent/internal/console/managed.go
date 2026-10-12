package console

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// Where the box's own settings may be changed (ADR 0049).
const (
	ManagedSite  = "site"  // only the box console
	ManagedCloud = "cloud" // only the Connect portal; the console reads
	ManagedBoth  = "both"  // either; the newest change wins
)

const managedFile = "console-managed.json"

var managedNames = map[string]string{
	ManagedSite:  "On site",
	ManagedCloud: "Cloud",
	ManagedBoth:  "On site and cloud",
}

// Managed is the box's "Managed from" setting and when it was last set,
// from either side. A box that never had it set is managed from both.
type Managed struct {
	Mode string    `json:"mode"`
	At   time.Time `json:"at"`
	By   string    `json:"by,omitempty"`
}

func validMode(m string) bool { _, ok := managedNames[m]; return ok }

func loadManaged(path string) (Managed, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return Managed{Mode: ManagedBoth}, nil
	}
	if err != nil {
		return Managed{}, err
	}
	var m Managed
	if err := json.Unmarshal(b, &m); err != nil {
		return Managed{}, err
	}
	if !validMode(m.Mode) {
		m.Mode = ManagedBoth
	}
	return m, nil
}

func saveManaged(path string, m Managed) error {
	b, err := json.Marshal(m)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// managed is the current setting.
func (s *Server) managed() Managed {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.mgd
}

// setManaged keeps m if it is newer than what the box has. It reports
// whether it did.
func (s *Server) setManaged(m Managed) (bool, error) {
	s.mgdMu.Lock()
	defer s.mgdMu.Unlock()
	s.mu.Lock()
	cur := s.mgd
	s.mu.Unlock()
	if !m.At.After(cur.At) {
		return false, nil
	}
	if err := saveManaged(filepath.Join(s.Dir, managedFile), m); err != nil {
		return false, err
	}
	s.mu.Lock()
	s.mgd = m
	s.mu.Unlock()
	return true, nil
}

// FromPortal takes the portal's side of the box's settings, which arrives with
// every new desired state (the agent's BoxDesired hook). The newest setting
// wins, whichever side made it.
func (s *Server) FromPortal(b *desired.Box) {
	if b == nil {
		return
	}
	s.fromPortalManaged(b.ManagedFrom)
	s.fromPortalNetwork(b.Network)
	s.fromPortalSoftware(b.Software)
}

func (s *Server) fromPortalManaged(mf *desired.ManagedFrom) {
	if mf == nil || !validMode(mf.Mode) {
		return
	}
	in := Managed{Mode: mf.Mode, At: mf.At.UTC(), By: mf.By}
	cur := s.managed()
	if in.Mode == cur.Mode && !in.At.After(cur.At) {
		return
	}
	ok, err := s.setManaged(in)
	if err != nil {
		s.Log.Warn("saving the portal's managed-from setting failed", "err", err)
		return
	}
	if ok && in.Mode != cur.Mode {
		s.Log.Info("managed from changed in the portal", "mode", in.Mode, "by", in.By)
	}
}

// local wraps a handler that changes the box's own settings: it refuses while
// the box is managed from the cloud, where the portal is in charge.
func (s *Server) local(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.managed().Mode == ManagedCloud {
			fail(w, http.StatusConflict, "This box is managed from the Connect portal, so its settings are read-only here. "+
				"Change them in the portal, or take back control on the Settings page in an emergency.")
			return
		}
		h(w, r)
	}
}

func (s *Server) handleGetManaged(w http.ResponseWriter, r *http.Request) {
	_, _, joined := s.identity()
	writeJSON(w, http.StatusOK, map[string]any{"managed": s.managed(), "joined": joined})
}

// handleSetManaged switches where the box is managed from. The box keeps its
// settings as they are. While it is managed from the cloud the console can
// only take control back ("site"), and only with the admin password again:
// that is the emergency way out when the portal cannot be reached or a change
// from it has gone wrong.
func (s *Server) handleSetManaged(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Mode     string `json:"mode"`
		Password string `json:"password,omitempty"`
	}
	if err := readJSON(r, &in); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	if !validMode(in.Mode) {
		fail(w, http.StatusBadRequest, "Choose on site, the cloud, or both.")
		return
	}
	cur := s.managed()
	if in.Mode == cur.Mode {
		writeJSON(w, http.StatusOK, map[string]any{"managed": cur})
		return
	}
	if in.Mode == ManagedCloud {
		if _, _, joined := s.identity(); !joined {
			fail(w, http.StatusBadRequest, "Join the box to Connect before handing it to the cloud.")
			return
		}
	}
	emergency := cur.Mode == ManagedCloud
	if emergency {
		if in.Mode != ManagedSite {
			fail(w, http.StatusConflict, "This box is managed from the Connect portal. From here you can only take back control.")
			return
		}
		if err := s.auth.confirm(addrOf(r.RemoteAddr).String(), userOf(r), in.Password, time.Now()); err != nil {
			// Not 401: that would sign the person out of the console.
			code := http.StatusForbidden
			if errors.Is(err, errLocked) {
				code = http.StatusTooManyRequests
			}
			fail(w, code, "Type the admin password to take back control: "+err.Error()+".")
			return
		}
	}
	m := Managed{Mode: in.Mode, At: time.Now().UTC().Truncate(time.Microsecond), By: "box console (" + userOf(r) + ")"}
	if !m.At.After(cur.At) {
		// The box's clock is behind the portal's; still newest.
		m.At = cur.At.Add(time.Millisecond).Truncate(time.Microsecond)
	}
	if _, err := s.setManaged(m); err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	what := "Set Managed from to " + managedNames[in.Mode]
	if emergency {
		what = "Took back control on the box in an emergency: Managed from is now On site"
	}
	s.change(userOf(r), what)
	writeJSON(w, http.StatusOK, map[string]any{"managed": m})
}
