package console

// Software updates on the ExaCarib box image (ADR 0049). The image has two root
// slots; /usr/lib/exacarib/exacarib-update writes new software into the other
// slot, the box restarts into it once, and keeps it only if it comes up healthy
// (GET /api/health on loopback). Anything else, including a hang the hardware
// watchdog resets, boots the previous software again.
//
// StartUpdate is the entry point for whatever asks for an update: the console's
// Settings page, and (Connect's fleet management) the agent on the
// controller's word.

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

const updateTool = "/usr/lib/exacarib/exacarib-update"

// Release is software to install: a root file system and its SHA-256.
type Release struct {
	Version string `json:"version"`
	URL     string `json:"url"`
	SHA256  string `json:"sha256"`
}

// UpdateStatus is what exacarib-update status reports, plus the console's own state.
type UpdateStatus struct {
	AB       bool            `json:"ab"`
	Slot     string          `json:"slot,omitempty"`
	Version  string          `json:"version"`
	Trial    bool            `json:"trial"`
	Other    *slotInfo       `json:"other,omitempty"`
	Progress string          `json:"progress,omitempty"`
	Last     json.RawMessage `json:"last,omitempty"`
	Watchdog *struct {
		Device string `json:"device"`
		State  string `json:"state"`
	} `json:"watchdog,omitempty"`
	Running bool   `json:"running"`
	Error   string `json:"error,omitempty"`
}

type slotInfo struct {
	Slot    string `json:"slot"`
	Version string `json:"version"`
}

type updater struct {
	mu      sync.Mutex
	running bool
	err     string
	cache   *UpdateStatus
	cached  time.Time
}

var (
	versionRE = regexp.MustCompile(`^[0-9][0-9A-Za-z.+~-]{0,40}$`)
	sha256RE  = regexp.MustCompile(`^[0-9a-f]{64}$`)
	// Where box releases are published (GitHub releases box-v<version>).
	releasesRepo = envOr("EXA_BOX_RELEASES", "zagias/exaconnect")
	githubAPI    = "https://api.github.com"
	githubDL     = "https://github.com"
)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func (s *Server) hasUpdater() bool {
	_, err := os.Stat(s.Root + updateTool)
	return err == nil
}

// updateStatus runs exacarib-update status (cached for a minute unless fresh).
func (s *Server) updateStatus(ctx context.Context, fresh bool) UpdateStatus {
	u := &s.upd
	u.mu.Lock()
	if !fresh && u.cache != nil && time.Since(u.cached) < time.Minute {
		st := *u.cache
		st.Running, st.Error = u.running, u.err
		u.mu.Unlock()
		return st
	}
	u.mu.Unlock()
	st := UpdateStatus{Version: s.Version}
	if s.hasUpdater() {
		out, err := s.Sys.Run(ctx, updateTool, "status")
		if err == nil {
			_ = json.Unmarshal(out, &st)
		} else {
			st.Error = err.Error()
		}
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	c := st
	u.cache, u.cached = &c, time.Now()
	st.Running = u.running
	if u.err != "" {
		st.Error = u.err
	}
	return st
}

// UpdateInfo is a short summary for telemetry (BoxInfo).
type UpdateInfo struct {
	AB      bool            `json:"ab"`
	Slot    string          `json:"slot,omitempty"`
	Version string          `json:"version,omitempty"`
	Trial   bool            `json:"trial,omitempty"`
	Running bool            `json:"running,omitempty"`
	Last    json.RawMessage `json:"last,omitempty"`
}

// StartUpdate installs rel into the other slot and restarts into it. It
// returns once the install has started; progress shows in /api/update.
func (s *Server) StartUpdate(rel Release, by string) error {
	if !s.hasUpdater() {
		return errors.New("this box was not installed from the ExaCarib box image; update it with apt")
	}
	if rel.URL == "" && rel.Version != "" {
		r, err := resolveRelease(context.Background(), rel.Version)
		if err != nil {
			return err
		}
		rel = r
	}
	if !strings.HasPrefix(rel.URL, "https://") && !strings.HasPrefix(rel.URL, "http://") {
		return errors.New("the software address must start with https://")
	}
	if !sha256RE.MatchString(rel.SHA256) {
		return errors.New("the SHA-256 must be 64 hexadecimal digits")
	}
	if rel.Version != "" && !versionRE.MatchString(rel.Version) {
		return errors.New("that is not a version number")
	}
	u := &s.upd
	u.mu.Lock()
	if u.running {
		u.mu.Unlock()
		return errors.New("an update is already running")
	}
	u.running, u.err = true, ""
	u.mu.Unlock()
	s.change(by, "Software update to "+orUnknown(rel.Version)+" started")
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 45*time.Minute)
		defer cancel()
		_, err := s.Sys.Run(ctx, updateTool, "install", rel.URL, rel.SHA256, rel.Version)
		u.mu.Lock()
		u.running = false
		u.cache = nil
		if err != nil {
			u.err = "The update did not install: " + lastLine(err.Error())
		}
		u.mu.Unlock()
		if err != nil {
			s.Log.Warn("update failed", "err", err)
			s.report("box_update_failed", map[string]string{"version": rel.Version, "error": lastLine(err.Error())})
			return
		}
		s.report("box_update_restarting", map[string]string{"version": rel.Version})
		s.Log.Info("update installed; restarting into it", "version", rel.Version)
		time.Sleep(3 * time.Second)
		_, _ = s.Sys.Run(context.Background(), "systemctl", "reboot")
	}()
	return nil
}

func orUnknown(v string) string {
	if v == "" {
		return "a new version"
	}
	return v
}

func lastLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.LastIndex(s, "\n"); i >= 0 {
		s = s[i+1:]
	}
	return s
}

type ghRelease struct {
	Tag    string `json:"tag_name"`
	Assets []struct {
		Name string `json:"name"`
		URL  string `json:"browser_download_url"`
	} `json:"assets"`
}

var httpClient = &http.Client{Timeout: 30 * time.Second}

// latestRelease is the newest box-v* release with a root file system.
func latestRelease(ctx context.Context) (string, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("%s/repos/%s/releases?per_page=30", githubAPI, releasesRepo), nil)
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("could not reach GitHub: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("GitHub answered %d", resp.StatusCode)
	}
	var rs []ghRelease
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&rs); err != nil {
		return "", err
	}
	for _, r := range rs {
		v, ok := strings.CutPrefix(r.Tag, "box-v")
		if !ok {
			continue
		}
		for _, a := range r.Assets {
			if strings.HasSuffix(a.Name, "-amd64.root.tar.zst") {
				return v, nil
			}
		}
	}
	return "", errors.New("no box release with updates yet")
}

// resolveRelease finds a version's root file system and its SHA-256.
func resolveRelease(ctx context.Context, ver string) (Release, error) {
	if !versionRE.MatchString(ver) {
		return Release{}, errors.New("that is not a version number")
	}
	base := fmt.Sprintf("%s/%s/releases/download/box-v%s/exacarib-box-%s-amd64", githubDL, releasesRepo, ver, ver)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+".sha256", nil)
	resp, err := httpClient.Do(req)
	if err != nil {
		return Release{}, fmt.Errorf("could not reach GitHub: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return Release{}, fmt.Errorf("there is no box release %s (GitHub answered %d)", ver, resp.StatusCode)
	}
	sc := bufio.NewScanner(io.LimitReader(resp.Body, 64<<10))
	for sc.Scan() {
		f := strings.Fields(sc.Text())
		if len(f) == 2 && strings.HasSuffix(f[1], ".root.tar.zst") && sha256RE.MatchString(f[0]) {
			return Release{Version: ver, URL: base + ".root.tar.zst", SHA256: f[0]}, nil
		}
	}
	return Release{}, fmt.Errorf("box release %s has no update file", ver)
}

// ---- API ----

// handleHealth is for the box itself (loopback only): the update check and
// the agent's watchdog. ok means the console answers and, once the box has
// joined Connect, the controller has answered too.
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if a := addrOf(r.RemoteAddr); !a.IsLoopback() {
		http.NotFound(w, r)
		return
	}
	_, _, joined := s.identity()
	s.mu.Lock()
	a := s.agent
	s.mu.Unlock()
	out := map[string]any{"ok": true, "joined": joined}
	if joined {
		ok := a != nil && a.ControllerOK()
		out["controller"] = ok
		if !ok {
			out["ok"], out["why"] = false, "ExaCarib Connect has not answered yet"
		}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleGetUpdate(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.updateStatus(r.Context(), true))
}

func (s *Server) handleLatest(w http.ResponseWriter, r *http.Request) {
	v, err := latestRelease(r.Context())
	if err != nil {
		fail(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"version": v})
}

func (s *Server) handleStartUpdate(w http.ResponseWriter, r *http.Request) {
	var q Release
	if err := readJSON(r, &q); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	if q.URL == "" && q.Version == "" {
		fail(w, http.StatusBadRequest, "Say which version to install.")
		return
	}
	if err := s.StartUpdate(q, userOf(r)); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"detail": "Installing. The box restarts into the new software and keeps it only if it comes up healthy."})
}

func (s *Server) handleRollback(w http.ResponseWriter, r *http.Request) {
	if !s.hasUpdater() {
		fail(w, http.StatusConflict, "This box was not installed from the ExaCarib box image.")
		return
	}
	if _, err := s.Sys.Run(r.Context(), updateTool, "rollback"); err != nil {
		fail(w, http.StatusConflict, lastLine(err.Error()))
		return
	}
	s.change(userOf(r), "Went back to the previous software")
	go func() {
		time.Sleep(3 * time.Second)
		_, _ = s.Sys.Run(context.Background(), "systemctl", "reboot")
	}()
	writeJSON(w, http.StatusAccepted, map[string]string{"detail": "Restarting into the previous software."})
}
