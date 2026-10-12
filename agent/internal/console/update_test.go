package console

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeRunner stands in for exacarib-update and systemctl.
type fakeRunner struct {
	mu    sync.Mutex
	calls []string
	fail  bool
}

func (f *fakeRunner) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, name+" "+strings.Join(args, " "))
	if strings.HasSuffix(name, "exacarib-update") && len(args) > 0 {
		switch args[0] {
		case "status":
			return []byte(`{"ab": true, "slot": "A", "version": "1.0.20", "trial": false, "other": {"slot": "B", "version": ""}, "progress": "", "last": null, "watchdog": {"device": "i6300ESB timer", "state": "active"}}`), nil
		case "install":
			if f.fail {
				return nil, errors.New("exit status 5: the download does not match its sha256; nothing changed")
			}
		}
	}
	return nil, nil
}
func (f *fakeRunner) WriteFile(p string, b []byte, m os.FileMode) error { return os.WriteFile(p, b, m) }
func (f *fakeRunner) Exists(p string) bool                              { _, err := os.Stat(p); return err == nil }
func (f *fakeRunner) ReadFile(p string) ([]byte, error)                 { return os.ReadFile(p) }

func (f *fakeRunner) has(prefix string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.calls {
		if strings.HasPrefix(c, prefix) {
			return true
		}
	}
	return false
}

func TestSoftwareUpdate(t *testing.T) {
	s, ts, c, _ := newTestServer(t)
	fr := &fakeRunner{}
	s.Sys = fr
	s.Root = t.TempDir()
	_ = os.MkdirAll(filepath.Join(s.Root, filepath.Dir(updateTool)), 0o755)
	_ = os.WriteFile(filepath.Join(s.Root, updateTool), []byte("#!/bin/sh\n"), 0o755)
	code, _ := SetupCode(s.Dir)
	if st, out := call(t, c, ts.URL, "/api/claim", map[string]string{"code": code, "password": "a good long password"}); st != 200 {
		t.Fatalf("claim %d %v", st, out)
	}
	st, u := call(t, c, ts.URL, "/api/update", nil)
	if st != 200 || u["slot"] != "A" || u["ab"] != true {
		t.Fatalf("status %d %v", st, u)
	}
	sum := strings.Repeat("ab", 32)
	for _, bad := range []map[string]string{{"url": "ftp://x/root.tar.zst", "sha256": sum}, {"url": "https://x/root.tar.zst", "sha256": "abc"}, {}} {
		if st, out := call(t, c, ts.URL, "/api/update", bad); st != 400 {
			t.Fatalf("bad update %v answered %d %v", bad, st, out)
		}
	}
	if st, out := call(t, c, ts.URL, "/api/update", map[string]string{"url": "https://example.com/r.root.tar.zst", "sha256": sum, "version": "1.0.21"}); st != 202 {
		t.Fatalf("update %d %v", st, out)
	}
	deadline := time.Now().Add(6 * time.Second)
	for !fr.has("systemctl reboot") && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if !fr.has(updateTool+" install https://example.com/r.root.tar.zst "+sum+" 1.0.21") || !fr.has("systemctl reboot") {
		t.Fatalf("calls %v", fr.calls)
	}
	// A failed install says why and does not restart.
	fr.mu.Lock()
	fr.fail, fr.calls = true, nil
	fr.mu.Unlock()
	if st, _ := call(t, c, ts.URL, "/api/update", map[string]string{"url": "https://example.com/r.root.tar.zst", "sha256": sum}); st != 202 {
		t.Fatal("second update not started")
	}
	time.Sleep(300 * time.Millisecond)
	_, u = call(t, c, ts.URL, "/api/update", nil)
	if !strings.Contains(u["error"].(string), "does not match") || fr.has("systemctl reboot") {
		t.Fatalf("failed install: %v %v", u, fr.calls)
	}
	if b := s.BoxInfo().(BoxInfo); b.Software == nil || b.Software.Slot != "A" {
		t.Fatalf("telemetry software %+v", b.Software)
	}
}

func TestHealthLoopbackOnly(t *testing.T) {
	s, _, _, _ := newTestServer(t)
	h := s.Handler()
	req := httptest.NewRequest(http.MethodGet, "/api/health", nil)
	req.RemoteAddr = "127.0.0.1:5000"
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"ok":true`) {
		t.Fatalf("health %d %s", rec.Code, rec.Body)
	}
	req.RemoteAddr = "10.0.0.5:5000"
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 404 {
		t.Fatalf("health from the office answered %d", rec.Code)
	}
}
