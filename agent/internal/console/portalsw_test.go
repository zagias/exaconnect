package console

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

func TestSoftwareFromPortal(t *testing.T) {
	sum := strings.Repeat("cd", 32)
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "box-v1.0.21/") {
			fmt.Fprintf(w, "%s  exacarib-box-1.0.21-amd64.root.tar.zst\n", sum)
			return
		}
		http.NotFound(w, r)
	}))
	defer gh.Close()
	old := githubDL
	githubDL = gh.URL
	defer func() { githubDL = old }()

	s, _, _, _ := newTestServer(t)
	t0 := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
	sw := func(v string, at time.Time) *desired.Box {
		return &desired.Box{Software: &desired.BoxSoftware{Version: v, At: at, By: "dana@example.org"}}
	}

	// Managed on site: ignored.
	if _, err := s.setManaged(Managed{Mode: ManagedSite, At: t0}); err != nil {
		t.Fatal(err)
	}
	s.FromPortal(sw("1.0.21", t0.Add(time.Second)))
	if !s.swRequest().At.IsZero() {
		t.Fatal("software from the portal taken while managed on site")
	}
	if _, err := s.setManaged(Managed{Mode: ManagedCloud, At: t0.Add(2 * time.Second)}); err != nil {
		t.Fatal(err)
	}

	// A box not installed from the image says it updates with apt.
	s.FromPortal(sw("1.0.21", t0.Add(3*time.Second)))
	if r := s.swRequest(); r.State != "refused" || !strings.Contains(r.Detail, "apt") {
		t.Fatalf("no updater: %+v", r)
	}

	fr := &fakeRunner{}
	s.Sys = fr
	s.Root = t.TempDir()
	_ = os.MkdirAll(filepath.Join(s.Root, filepath.Dir(updateTool)), 0o755)
	_ = os.WriteFile(filepath.Join(s.Root, updateTool), []byte("#!/bin/sh\n"), 0o755)

	// The version already running: nothing to do.
	s.FromPortal(sw("1.0.20", t0.Add(4*time.Second)))
	if r := s.swRequest(); r.State != "current" || fr.has(updateTool+" install") {
		t.Fatalf("current: %+v %v", r, fr.calls)
	}
	// A new version is installed once, then the box restarts into it.
	s.FromPortal(sw("1.0.21", t0.Add(5*time.Second)))
	s.FromPortal(sw("1.0.21", t0.Add(5*time.Second)))
	if r := s.swRequest(); r.State != "installing" {
		t.Fatalf("installing: %+v", r)
	}
	deadline := time.Now().Add(6 * time.Second)
	for !fr.has("systemctl reboot") && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if !fr.has(updateTool + " install " + gh.URL + "/zagias/exaconnect/releases/download/box-v1.0.21/exacarib-box-1.0.21-amd64.root.tar.zst " + sum + " 1.0.21") {
		t.Fatalf("calls %v", fr.calls)
	}
	// Between the install and the restart, still on the old version: not a rollback yet.
	s.BoxInfo()
	if r := s.swRequest(); r.State != "installing" {
		t.Fatalf("judged before the restart: %+v", r)
	}

	// After the restart the box is still on 1.0.20 past the health check: it went back.
	again := &Server{Dir: s.Dir, Backend: s.Backend, Sys: fr, Root: s.Root}
	if err := again.Init(); err != nil {
		t.Fatal(err)
	}
	// (Pretend the install started ten minutes ago and the box came back up five minutes ago.)
	again.swReq.Started = time.Now().Add(-10 * time.Minute)
	again.started = time.Now().Add(-5 * time.Minute)
	again.upd.cache = nil
	b := again.BoxInfo().(BoxInfo)
	if b.SoftwarePortal == nil || b.SoftwarePortal.State != "rolled_back" || b.Software == nil {
		t.Fatalf("rolled back: %+v", b.SoftwarePortal)
	}
}

func TestSoftwareFromPortalFails(t *testing.T) {
	s, _, _, _ := newTestServer(t)
	fr := &fakeRunner{fail: true}
	s.Sys = fr
	s.Root = t.TempDir()
	_ = os.MkdirAll(filepath.Join(s.Root, filepath.Dir(updateTool)), 0o755)
	_ = os.WriteFile(filepath.Join(s.Root, updateTool), []byte("#!/bin/sh\n"), 0o755)
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "%s  exacarib-box-1.0.22-amd64.root.tar.zst\n", strings.Repeat("ef", 32))
	}))
	defer gh.Close()
	old := githubDL
	githubDL = gh.URL
	defer func() { githubDL = old }()
	if _, err := s.setManaged(Managed{Mode: ManagedBoth, At: time.Now().Add(-time.Hour)}); err != nil {
		t.Fatal(err)
	}
	s.FromPortal(&desired.Box{Software: &desired.BoxSoftware{Version: "1.0.22", At: time.Now()}})
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		s.BoxInfo()
		if s.swRequest().State == "failed" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if r := s.swRequest(); r.State != "failed" || !strings.Contains(r.Detail, "does not match") || fr.has("systemctl reboot") {
		t.Fatalf("failed install: %+v", r)
	}
}
