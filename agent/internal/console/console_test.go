package console

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestPBKDF2Vector(t *testing.T) {
	// RFC 7914 section 11, PBKDF2-HMAC-SHA256 with one iteration.
	got := hex.EncodeToString(pbkdf2([]byte("passwd"), []byte("salt"), 1, 64))
	want := "55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc49ca9cccf179b645991664b39d77ef317c71b845b1e30bd509112041d3a19783"
	if got != want {
		t.Fatalf("pbkdf2 = %s", got)
	}
	c := hashPassword("correct horse")
	if !c.check("admin", "correct horse") || c.check("admin", "wrong horse") || c.check("root", "correct horse") {
		t.Fatal("password check wrong")
	}
}

func TestSetupCodeAndReset(t *testing.T) {
	dir := t.TempDir()
	a, err := SetupCode(dir)
	if err != nil || len(a) != 11 || a[5] != '-' {
		t.Fatalf("code %q %v", a, err)
	}
	if b, _ := SetupCode(dir); b != a {
		t.Fatal("setup code should stay the same until used")
	}
	st, _ := os.Stat(filepath.Join(dir, setupCodeFile))
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("setup code file mode %v", st.Mode())
	}
	au := newAuth(dir)
	if _, err := au.claim("10.0.0.5", "wrong-code", "long enough pw", time.Now()); err == nil {
		t.Fatal("claim with a wrong code")
	}
	if _, err := au.claim("10.0.0.5", strings.ToLower(a), "short", time.Now()); err == nil {
		t.Fatal("short password accepted")
	}
	if _, err := au.claim("10.0.0.5", strings.ToLower(a), "long enough pw", time.Now()); err != nil {
		t.Fatal(err)
	}
	if c, _ := SetupCode(dir); c != "" {
		t.Fatal("no setup code once a password is set")
	}
	if _, err := au.claim("10.0.0.5", a, "another password", time.Now()); err == nil {
		t.Fatal("claimed twice")
	}
	n, _ := ResetPassword(dir)
	if n == "" || n == a {
		t.Fatalf("reset gave %q", n)
	}
}

func TestLoginLockout(t *testing.T) {
	au := newAuth(t.TempDir())
	_ = au.save(hashPassword("the right one!"))
	now := time.Now()
	for i := 0; i < maxFails; i++ {
		if _, err := au.login("10.0.0.9", "admin", "nope", now); err == nil {
			t.Fatal("bad password accepted")
		}
	}
	if _, err := au.login("10.0.0.9", "admin", "the right one!", now); !errors.Is(err, errLocked) {
		t.Fatalf("want lockout, got %v", err)
	}
	if _, err := au.login("10.0.0.10", "admin", "the right one!", now); err != nil {
		t.Fatalf("another address is not locked: %v", err)
	}
	if _, err := au.login("10.0.0.9", "admin", "the right one!", now.Add(lockout+time.Second)); err != nil {
		t.Fatalf("lockout should end: %v", err)
	}
}

func TestSessionsExpire(t *testing.T) {
	au := newAuth(t.TempDir())
	now := time.Now()
	tok := au.open("admin", now)
	if _, ok := au.session(tok, now.Add(sessionIdle-time.Minute)); !ok {
		t.Fatal("session ended early")
	}
	if _, ok := au.session(tok, now.Add(2*sessionIdle)); ok {
		t.Fatal("idle session still open")
	}
}

func TestValidateNetwork(t *testing.T) {
	have := map[string]bool{"eth0": true, "eth1": true, "eth2": true}
	good := Network{WAN: []Port{{Iface: "eth0", Mode: "dhcp"}, {Iface: "eth1", Path: "carrier-b", Mode: "static", Address: "203.0.113.10/29", Gateway: "203.0.113.9"}},
		LAN: &Port{Iface: "eth2", Mode: "static", Address: "192.168.10.1/24"}, LANDHCP: true}
	if err := good.Validate(have); err != nil {
		t.Fatal(err)
	}
	if good.WAN[0].Path != "carrier-a" {
		t.Fatalf("first WAN port defaults to carrier-a, got %q", good.WAN[0].Path)
	}
	bad := []Network{
		{},
		{WAN: []Port{{Iface: "eth9", Mode: "dhcp"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "dhcp"}, {Iface: "eth0", Path: "carrier-b", Mode: "dhcp"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "dhcp", Path: "sat"}, {Iface: "eth1", Path: "sat", Mode: "dhcp"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "static", Address: "203.0.113.10/29"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "static", Address: "203.0.113.10/29", Gateway: "198.51.100.1"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "static", Address: "203.0.113.8/29", Gateway: "203.0.113.9"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "pppoe", PPPoEUser: "u"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "dhcp"}}, LAN: &Port{Iface: "eth1", Mode: "dhcp"}},
		{WAN: []Port{{Iface: "eth0", Mode: "dhcp"}}, LANDHCP: true},
		{WAN: []Port{{Iface: "eth0", Mode: "dhcp"}}, LAN: &Port{Iface: "eth1", Mode: "static", Address: "10.0.0.1/30"}, LANDHCP: true},
		{WAN: []Port{{Iface: "eth0", Mode: "pppoe", PPPoEUser: "u\"x", PPPoEPassword: "p"}}},
		{WAN: []Port{{Iface: "eth0", Mode: "dhcp", DNS: []string{"not-an-ip"}}}},
	}
	for i, n := range bad {
		if err := n.Validate(have); err == nil {
			t.Errorf("case %d: accepted %+v", i, n)
		}
	}
}

func TestRenderers(t *testing.T) {
	n := Network{WAN: []Port{{Iface: "eth0", Path: "carrier-a", Mode: "dhcp"}, {Iface: "eth1", Path: "carrier-b", Mode: "static", Address: "203.0.113.10/29", Gateway: "203.0.113.9", DNS: []string{"1.1.1.1"}}, {Iface: "eth3", Path: "sat", Mode: "keep"}},
		LAN: &Port{Iface: "eth2", Mode: "static", Address: "192.168.10.1/24"}}
	np := Netplan(n)
	for _, want := range []string{"    eth0:\n      dhcp4: true\n      dhcp4-overrides:\n        route-metric: 100", "addresses: [203.0.113.10/29]", "via: 203.0.113.9\n          metric: 200", "nameservers:\n        addresses: [1.1.1.1]", "    eth2:\n      dhcp4: false\n      addresses: [192.168.10.1/24]"} {
		if !strings.Contains(np, want) {
			t.Errorf("netplan missing %q:\n%s", want, np)
		}
	}
	if strings.Contains(np, "eth3") {
		t.Error("a port left as it is must not be in netplan")
	}
	dm, err := DNSMasq(*n.LAN)
	if err != nil || !strings.Contains(dm, "dhcp-range=192.168.10.128,192.168.10.254,12h") || !strings.Contains(dm, "option:router,192.168.10.1") {
		t.Fatalf("dnsmasq %v:\n%s", err, dm)
	}
	if s, e, _ := dhcpRange("192.168.10.200/24"); s.String() != "192.168.10.2" || e.String() != "192.168.10.127" {
		t.Fatalf("range around the box: %s-%s", s, e)
	}
	peer := PPPoEPeer(Port{Iface: "eth0", PPPoEUser: "me@isp", PPPoEPassword: "pw"}, 100, "pppoe.so")
	if !strings.Contains(peer, "nic-eth0") || !strings.Contains(peer, `user "me@isp"`) || !strings.Contains(peer, "defaultroute-metric 100") {
		t.Fatal(peer)
	}
}

func TestJoinCode(t *testing.T) {
	fp := strings.Repeat("ab", 32)
	raw, _ := json.Marshal(map[string]string{"c": "https://connect.example:8443/", "t": "tok-0123456789", "f": strings.ToUpper(fp), "n": "kingston-hq"})
	j, err := ParseJoinCode("EXA1." + base64.RawURLEncoding.EncodeToString(raw) + "\n")
	if err != nil {
		t.Fatal(err)
	}
	if j.Controller != "https://connect.example:8443" || j.Fingerprint != fp || j.Name != "kingston-hq" {
		t.Fatalf("%+v", j)
	}
	for _, c := range []string{"", "hello", "EXA1.!!!", "EXA1." + base64.RawURLEncoding.EncodeToString([]byte(`{"c":"http://x","t":"tok-0123456789","f":"`+fp+`","n":"a"}`))} {
		if _, err := ParseJoinCode(c); err == nil {
			t.Errorf("accepted %q", c)
		}
	}
}

type fakeBackend struct {
	mu      sync.Mutex
	applied []Network
	fail    bool
}

func (f *fakeBackend) list() []Network {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]Network(nil), f.applied...)
}

func (f *fakeBackend) Caps() Caps                                   { return Caps{DHCPClient: true, DHCPServer: true} }
func (f *fakeBackend) Restore(ctx context.Context, n Network) error { return f.Apply(ctx, n) }
func (f *fakeBackend) Apply(_ context.Context, n Network) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail {
		return errors.New("boom")
	}
	f.applied = append(f.applied, n)
	return nil
}

func newTestServer(t *testing.T) (*Server, *httptest.Server, *http.Client, *fakeBackend) {
	t.Helper()
	fb := &fakeBackend{}
	var jmu sync.Mutex
	joined := ""
	s := &Server{Dir: t.TempDir(), Version: "test", Backend: fb, RevertAfter: 300 * time.Millisecond,
		Join: func(_ context.Context, j JoinInfo) error {
			if j.Token == "bad-token-000" {
				return errors.New(`POST /api/v1/enrol: 401 Unauthorized: {"detail":"This enrolment token is unknown, used or expired."}`)
			}
			jmu.Lock()
			joined = j.Name
			jmu.Unlock()
			return nil
		},
		Identity: func() (string, string, bool) {
			jmu.Lock()
			defer jmu.Unlock()
			return joined, "https://ctl:8443", joined != ""
		},
	}
	if err := s.Init(); err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewTLSServer(s.Handler())
	t.Cleanup(ts.Close)
	c := ts.Client()
	c.Jar, _ = cookiejar.New(nil)
	return s, ts, c, fb
}

func call(t *testing.T, c *http.Client, url, path string, body any) (int, map[string]any) {
	t.Helper()
	var req *http.Request
	if body == nil {
		req, _ = http.NewRequest(http.MethodGet, url+path, nil)
	} else {
		b, _ := json.Marshal(body)
		req, _ = http.NewRequest(http.MethodPost, url+path, bytes.NewReader(b))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Exa-Box", "1")
	}
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestConsoleFlow(t *testing.T) {
	s, ts, c, fb := newTestServer(t)
	if code, st := call(t, c, ts.URL, "/api/state", nil); code != 200 || st["stage"] != "claim" {
		t.Fatalf("state %d %v", code, st)
	}
	if code, _ := call(t, c, ts.URL, "/api/overview", nil); code != 401 {
		t.Fatalf("overview before sign-in answered %d", code)
	}
	// Writes need the console's header.
	resp, _ := c.Post(ts.URL+"/api/claim", "application/json", strings.NewReader(`{}`))
	if resp.StatusCode != 403 {
		t.Fatalf("write without header answered %d", resp.StatusCode)
	}
	resp.Body.Close()
	code, _ := SetupCode(s.Dir)
	if st, out := call(t, c, ts.URL, "/api/claim", map[string]string{"code": code, "password": "a good long password"}); st != 200 {
		t.Fatalf("claim %d %v", st, out)
	}
	if _, st := call(t, c, ts.URL, "/api/state", nil); st["stage"] != "ready" {
		t.Fatalf("after claim %v", st)
	}
	// Ports: the test host's own interfaces.
	ports, _ := Ports("")
	if len(ports) == 0 {
		t.Skip("no ports to set up in this sandbox")
	}
	n := map[string]any{"wan": []map[string]any{{"iface": ports[0].Name, "mode": "dhcp"}}}
	if st, out := call(t, c, ts.URL, "/api/network", n); st != 200 || out["pending"] == nil {
		t.Fatalf("network %d %v", st, out)
	}
	if st, _ := call(t, c, ts.URL, "/api/network/confirm", map[string]any{}); st != 200 {
		t.Fatal("confirm")
	}
	if len(fb.list()) != 1 {
		t.Fatalf("applied %d times", len(fb.list()))
	}
	// A change nobody confirms goes back by itself.
	n2 := map[string]any{"wan": []map[string]any{{"iface": ports[0].Name, "mode": "keep"}}}
	call(t, c, ts.URL, "/api/network", n2)
	time.Sleep(600 * time.Millisecond)
	if got := fb.list(); len(got) != 3 || got[2].WAN[0].Mode != "dhcp" {
		t.Fatalf("not reverted: %+v", got)
	}
	if info := s.BoxInfo().(BoxInfo); !info.Console || !info.AdminSet || len(info.WAN) != 1 || info.WAN[0].Mode != "dhcp" || info.LastChange == nil {
		t.Fatalf("box info %+v", info)
	}
	// Join: the controller's own reason comes back.
	if st, out := call(t, c, ts.URL, "/api/join", map[string]string{"controller": "https://ctl:8443", "token": "bad-token-000", "fingerprint": strings.Repeat("0", 64), "name": "box-1"}); st != 400 || !strings.Contains(out["detail"].(string), "unknown, used or expired") {
		t.Fatalf("join %d %v", st, out)
	}
	if st, out := call(t, c, ts.URL, "/api/join", map[string]string{"controller": "https://ctl:8443", "token": "good-token-000", "fingerprint": strings.Repeat("0", 64), "name": "box-1"}); st != 200 {
		t.Fatalf("join %d %v", st, out)
	}
	select {
	case <-s.Joined():
	default:
		t.Fatal("joined not signalled")
	}
	if st, _ := call(t, c, ts.URL, "/api/join", map[string]string{"code": "x"}); st != 409 {
		t.Fatal("joined twice")
	}
	if st, out := call(t, c, ts.URL, "/api/overview", nil); st != 200 || out["controller"].(map[string]any)["state"] != "starting" {
		t.Fatalf("overview %d %v", st, out)
	}
	// Sign out, then back in.
	call(t, c, ts.URL, "/api/logout", map[string]any{})
	if st, _ := call(t, c, ts.URL, "/api/overview", nil); st != 401 {
		t.Fatal("still signed in")
	}
	if st, _ := call(t, c, ts.URL, "/api/login", map[string]string{"user": "admin", "password": "a good long password"}); st != 200 {
		t.Fatal("sign in")
	}
	if st, out := call(t, c, ts.URL, "/api/diag", map[string]string{"tool": "ping", "target": "-f 1.1.1.1"}); st != 400 {
		t.Fatalf("diag accepted an option as a target: %v", out)
	}
	if st, _ := call(t, c, ts.URL, "/", nil); st != 200 {
		t.Fatal("index")
	}
}

func TestOnlyOfficeSide(t *testing.T) {
	s := &Server{}
	s.mu.Lock()
	s.mu.Unlock()
	req := httptest.NewRequest(http.MethodGet, "/api/state", nil)
	for addr, want := range map[string]bool{"192.168.1.20:5000": true, "10.1.2.3:1": true, "127.0.0.1:9": true, "[fe80::1]:9": true, "203.0.113.7:4000": false, "8.8.8.8:1": false} {
		req.RemoteAddr = addr
		if got := s.allowed(req); got != want {
			t.Errorf("%s: allowed=%v", addr, got)
		}
	}
	// A request that arrives on a WAN port is refused even from a private address.
	ifs, _ := net.Interfaces()
	for _, i := range ifs {
		pfs := ifacePrefixes(i.Name)
		if i.Flags&net.FlagLoopback != 0 || len(pfs) == 0 {
			continue
		}
		s.net = &Network{WAN: []Port{{Iface: i.Name}}}
		r := req.WithContext(context.WithValue(req.Context(), http.LocalAddrContextKey, &net.TCPAddr{IP: pfs[0].Addr().AsSlice(), Port: 443}))
		r.RemoteAddr = "192.168.1.20:5000"
		if s.allowed(r) {
			t.Errorf("request on WAN port %s allowed", i.Name)
		}
		break
	}
}
