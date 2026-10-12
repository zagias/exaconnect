package console

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestNormaliseSerial(t *testing.T) {
	for in, want := range map[string]string{" ab 12-x ": "AB-12-X", "To Be Filled By O.E.M.": "TO-BE-FILLED-BY-O.E.M.", "x/y;z": "XYZ"} {
		if got := NormaliseSerial(in); got != want {
			t.Errorf("%q: %q", in, got)
		}
	}
	if !placeholder[NormaliseSerial("To Be Filled By O.E.M.")] || !placeholder[NormaliseSerial("Default string")] {
		t.Error("placeholders not recognised")
	}
	t.Setenv("EXA_BOX_SERIAL", "exa-box 0001")
	if Serial("") != "EXA-BOX-0001" {
		t.Fatal(Serial(""))
	}
}

func TestZeroTouch(t *testing.T) {
	t.Setenv("EXA_BOX_SERIAL", "EXA-TEST-42")
	var mu sync.Mutex
	calls, assigned := 0, false
	var pub string
	ts := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/ztp/hello" {
			http.NotFound(w, r)
			return
		}
		var h ztpHello
		_ = json.NewDecoder(r.Body).Decode(&h)
		k, _ := base64.StdEncoding.DecodeString(h.PublicKey)
		sig, _ := base64.StdEncoding.DecodeString(h.Signature)
		if h.Serial != "EXA-TEST-42" || !ed25519.Verify(k, []byte(ztpSignWhat+"\n"+h.Serial+"\n"+h.At), sig) {
			http.Error(w, `{"detail":"bad signature"}`, 403)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		calls++
		if pub != "" && pub != h.PublicKey {
			t.Error("the box's key changed between calls")
		}
		pub = h.PublicKey
		if !assigned {
			_ = json.NewEncoder(w).Encode(ztpAnswer{State: "waiting", Detail: "Not assigned yet"})
			return
		}
		_ = json.NewEncoder(w).Encode(ztpAnswer{State: "assigned", Controller: "https://ctl:8443", Token: "good-token-000",
			Fingerprint: strings.Repeat("ab", 32), Site: "kingston-hq"})
	}))
	defer ts.Close()
	s, _, _, _ := newTestServer(t)
	s.ZTPURL, s.ZTPClient, s.ZTPEvery = ts.URL, ts.Client(), 50*time.Millisecond
	s.ztp.Serial = Serial("")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() { s.ZeroTouch(ctx); close(done) }()
	time.Sleep(300 * time.Millisecond)
	if st := s.ztpStatus(); st.State != "waiting" || st.Serial != "EXA-TEST-42" || st.KeyID == "" {
		t.Fatalf("status while waiting %+v", st)
	}
	mu.Lock()
	assigned = true
	mu.Unlock()
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("zero-touch never joined")
	}
	if name, _, ok := s.identity(); !ok || name != "kingston-hq" {
		t.Fatalf("joined as %q %v", name, ok)
	}
	if st := s.ztpStatus(); st.State != "joined" {
		t.Fatalf("%+v", st)
	}
	if lc := s.BoxInfo().(BoxInfo); lc.LastChange == nil || !strings.Contains(lc.LastChange.What, "zero-touch") || lc.Serial != "EXA-TEST-42" {
		t.Fatalf("box info %+v", lc)
	}
	// Joined: zero-touch does nothing more.
	before := calls
	s.ZeroTouch(ctx)
	if calls != before {
		t.Fatal("called home after joining")
	}
	// Off means off.
	s2 := &Server{ZTPURL: "off"}
	if s2.ztpURL() != "" {
		t.Fatal("off is not off")
	}
}
