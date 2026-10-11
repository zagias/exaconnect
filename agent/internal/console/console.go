// Package console is the box console (ADR 0046): a small web app the agent
// serves on the office network, so someone at the branch can set a new box's
// ports, join it to Connect with the setup code from the portal, see its links,
// tunnels and routes, and run ping and traceroute. It has one local account,
// answers only on the office side, and keeps working while the controller is
// away. Everything it changes is reported to the controller.
package console

import (
	"context"
	"crypto/tls"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/zagias/exaconnect/agent/internal/agent"
	"github.com/zagias/exaconnect/agent/internal/frrstate"
	"github.com/zagias/exaconnect/agent/internal/system"
)

//go:embed static
var static embed.FS

const (
	cookieName  = "exabox"
	networkFile = "console-network.json"
)

// Server is the box console.
type Server struct {
	Dir     string // the agent's state directory
	Addr    string // listen address, e.g. :443
	Version string
	Log     *slog.Logger
	Sys     system.Runner
	Backend Backend
	// Join enrols the box with the controller (the agent's enrol).
	Join func(ctx context.Context, j JoinInfo) error
	// Identity is the enrolled name and controller, if the box has joined.
	Identity func() (name, controller string, ok bool)
	// RevertAfter undoes a port change nobody confirmed; default 2 minutes.
	RevertAfter time.Duration
	// Root prefixes /sys paths (tests).
	Root string

	auth       *auth
	mu         sync.Mutex
	agent      *agent.Agent
	pending    []pendingEvent
	net        *Network
	prev       *Network // the set-up to go back to while a change is unconfirmed
	revert     *time.Timer
	revertAt   time.Time
	gen        int // which change the revert timer is for
	lastChange *Change
	joined     chan struct{}
	joinOnce   sync.Once
	diagMu     sync.Mutex
	applyMu    sync.Mutex // one port change at a time, including an undo
	certFP     string
	started    time.Time
}

// Change is the last thing changed on the box itself.
type Change struct {
	At   time.Time `json:"at"`
	By   string    `json:"by"`
	What string    `json:"what"`
}

type pendingEvent struct {
	at     time.Time
	kind   string
	detail map[string]string
}

// Init loads what the console saved. Call it before Serve.
func (s *Server) Init() error {
	if s.Log == nil {
		s.Log = slog.Default()
	}
	if s.RevertAfter == 0 {
		s.RevertAfter = 2 * time.Minute
	}
	if s.Sys == nil {
		s.Sys = system.Host{}
	}
	if s.Backend == nil {
		s.Backend = HostBackend{Sys: s.Sys}
	}
	s.auth = newAuth(s.Dir)
	s.joined = make(chan struct{})
	s.started = time.Now()
	if _, _, ok := s.identity(); ok {
		s.joinOnce.Do(func() { close(s.joined) })
	}
	n, err := loadNetwork(filepath.Join(s.Dir, networkFile))
	if err != nil {
		return fmt.Errorf("box console network file: %w", err)
	}
	s.net = n
	if c, _ := s.auth.creds(); c == nil {
		if _, err := SetupCode(s.Dir); err != nil {
			return err
		}
	}
	return nil
}

func (s *Server) identity() (string, string, bool) {
	if s.Identity == nil {
		return "", "", false
	}
	return s.Identity()
}

// Restore puts back, after a restart, the saved port settings that do not
// survive one (see HostBackend.Restore).
func (s *Server) Restore(ctx context.Context) {
	s.mu.Lock()
	n := s.net
	s.mu.Unlock()
	if n == nil {
		return
	}
	if err := s.Backend.Restore(ctx, *n); err != nil {
		s.Log.Warn("putting back the saved ports failed", "err", err)
	}
}

// Joined is closed once the box has joined Connect.
func (s *Server) Joined() <-chan struct{} { return s.joined }

// SetAgent hands the running agent to the console, for its status and to
// report local changes.
func (s *Server) SetAgent(a *agent.Agent) {
	s.mu.Lock()
	s.agent = a
	ev := s.pending
	s.pending = nil
	s.mu.Unlock()
	for _, e := range ev {
		a.EventAt(e.at, e.kind, e.detail)
	}
}

func (s *Server) report(kind string, detail map[string]string) {
	s.mu.Lock()
	a := s.agent
	now := time.Now()
	if a == nil {
		s.pending = append(s.pending, pendingEvent{now, kind, detail})
	}
	s.mu.Unlock()
	if a != nil {
		a.EventAt(now, kind, detail)
	}
}

// BoxPort is a port as the controller hears about it.
type BoxPort struct {
	Iface   string `json:"iface"`
	Path    string `json:"path,omitempty"`
	Mode    string `json:"mode"`
	Address string `json:"address,omitempty"`
}

// BoxInfo goes up with telemetry, so the portal can show the box is also
// managed locally, and on which ports.
type BoxInfo struct {
	Console    bool      `json:"console"`
	URL        string    `json:"url,omitempty"`
	AdminSet   bool      `json:"admin_set"`
	WAN        []BoxPort `json:"wan,omitempty"`
	LAN        *BoxPort  `json:"lan,omitempty"`
	LANDHCP    bool      `json:"lan_dhcp,omitempty"`
	LastChange *Change   `json:"last_change,omitempty"`
}

// BoxInfo is the agent's Box hook.
func (s *Server) BoxInfo() any {
	c, _ := s.auth.creds()
	s.mu.Lock()
	defer s.mu.Unlock()
	b := BoxInfo{Console: true, AdminSet: c != nil, LastChange: s.lastChange}
	if s.net != nil {
		for _, p := range s.net.WAN {
			b.WAN = append(b.WAN, BoxPort{Iface: p.Iface, Path: p.Path, Mode: p.Mode})
		}
		if l := s.net.LAN; l != nil {
			b.LAN = &BoxPort{Iface: l.Iface, Mode: l.Mode, Address: l.Address}
			b.LANDHCP = s.net.LANDHCP
		}
	}
	b.URL = s.consoleURL()
	return b
}

// consoleURL is where people on the office network open the console. Caller holds s.mu.
func (s *Server) consoleURL() string {
	if s.net == nil || s.net.LAN == nil {
		return ""
	}
	addr := ""
	if s.net.LAN.Mode == "static" {
		if pf, err := netip.ParsePrefix(s.net.LAN.Address); err == nil {
			addr = pf.Addr().String()
		}
	} else if i, err := net.InterfaceByName(s.net.LAN.Iface); err == nil {
		if as, err := i.Addrs(); err == nil {
			for _, a := range as {
				if pf, err := netip.ParsePrefix(a.String()); err == nil && pf.Addr().Is4() {
					addr = pf.Addr().String()
					break
				}
			}
		}
	}
	if addr == "" {
		return ""
	}
	_, port, _ := net.SplitHostPort(s.Addr)
	if port != "" && port != "443" {
		return "https://" + net.JoinHostPort(addr, port)
	}
	return "https://" + addr
}

// Serve answers on Addr over TLS until ctx ends.
func (s *Server) Serve(ctx context.Context) error {
	host, _ := os.Hostname()
	cert, fp, err := consoleCert(s.Dir, host)
	if err != nil {
		return fmt.Errorf("box console certificate: %w", err)
	}
	s.certFP = fp
	ln, err := net.Listen("tcp", s.Addr)
	if err != nil {
		return err
	}
	srv := &http.Server{
		Handler:           s.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      90 * time.Second,
		IdleTimeout:       2 * time.Minute,
		TLSConfig:         &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12},
		ErrorLog:          slog.NewLogLogger(s.Log.Handler(), slog.LevelDebug),
	}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()
	s.Log.Info("box console listening", "addr", ln.Addr().String(), "cert_sha256", fp)
	if c, _ := s.auth.creds(); c == nil {
		s.Log.Info("box console has no admin password yet; run `exa-agent console-code` on the box for the setup code")
	}
	err = srv.ServeTLS(ln, "", "")
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// Handler is the console's routes behind its guards.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	sub, _ := fs.Sub(static, "static")
	files := http.FileServer(http.FS(sub))
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		b, _ := static.ReadFile("static/index.html")
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(b)
	})
	mux.Handle("GET /static/", http.StripPrefix("/static/", files))
	mux.HandleFunc("GET /api/state", s.handleState)
	mux.HandleFunc("POST /api/claim", s.handleClaim)
	mux.HandleFunc("POST /api/login", s.handleLogin)
	mux.HandleFunc("POST /api/logout", s.handleLogout)
	mux.HandleFunc("POST /api/password", s.signedIn(s.handlePassword))
	mux.HandleFunc("GET /api/overview", s.signedIn(s.handleOverview))
	mux.HandleFunc("GET /api/network", s.signedIn(s.handleGetNetwork))
	mux.HandleFunc("POST /api/network", s.signedIn(s.handleSetNetwork))
	mux.HandleFunc("POST /api/network/confirm", s.signedIn(s.handleConfirm))
	mux.HandleFunc("POST /api/join", s.signedIn(s.handleJoin))
	mux.HandleFunc("POST /api/diag", s.signedIn(s.handleDiag))
	return s.guard(mux)
}

// guard keeps the console on the office side, and sets the headers every
// answer needs. Writes need the console's own header, which a page on
// another site cannot send without a CORS preflight the console never allows.
func (s *Server) guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		if !s.allowed(r) {
			http.Error(w, "The box console answers only on the office network.", http.StatusForbidden)
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead && r.Header.Get("X-Exa-Box") != "1" {
			http.Error(w, "missing X-Exa-Box header", http.StatusForbidden)
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/") {
			h.Set("Cache-Control", "no-store")
		}
		next.ServeHTTP(w, r)
	})
}

// allowed refuses requests that arrive on an internet (WAN) port, and any
// from a public address outside the office network.
func (s *Server) allowed(r *http.Request) bool {
	remote := addrOf(r.RemoteAddr)
	if !remote.IsValid() {
		return false
	}
	local := netip.Addr{}
	if la, ok := r.Context().Value(http.LocalAddrContextKey).(net.Addr); ok {
		local = addrOf(la.String())
	}
	s.mu.Lock()
	var wan []string
	lan := ""
	if s.net != nil {
		for _, p := range s.net.WAN {
			wan = append(wan, p.Iface)
		}
		if s.net.LAN != nil {
			lan = s.net.LAN.Iface
		}
	}
	s.mu.Unlock()
	if local.IsValid() && !local.IsLoopback() {
		for _, name := range wan {
			for _, pf := range ifacePrefixes(name) {
				if pf.Addr() == local {
					return false
				}
			}
		}
	}
	if remote.IsLoopback() || remote.IsPrivate() || remote.IsLinkLocalUnicast() {
		return true
	}
	if lan != "" {
		for _, pf := range ifacePrefixes(lan) {
			if pf.Contains(remote) {
				return true
			}
		}
	}
	return false
}

func addrOf(hostport string) netip.Addr {
	host, _, err := net.SplitHostPort(hostport)
	if err != nil {
		host = hostport
	}
	a, err := netip.ParseAddr(host)
	if err != nil {
		return netip.Addr{}
	}
	return a.Unmap().WithZone("")
}

func ifacePrefixes(name string) []netip.Prefix {
	i, err := net.InterfaceByName(name)
	if err != nil {
		return nil
	}
	as, err := i.Addrs()
	if err != nil {
		return nil
	}
	var out []netip.Prefix
	for _, a := range as {
		if pf, err := netip.ParsePrefix(a.String()); err == nil {
			out = append(out, pf)
		}
	}
	return out
}

type userKey struct{}

func (s *Server) signedIn(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		c, err := r.Cookie(cookieName)
		if err != nil {
			fail(w, http.StatusUnauthorized, "Sign in first.")
			return
		}
		user, ok := s.auth.session(c.Value, time.Now())
		if !ok {
			fail(w, http.StatusUnauthorized, "Your session ended; sign in again.")
			return
		}
		h(w, r.WithContext(context.WithValue(r.Context(), userKey{}, user)))
	}
}

func userOf(r *http.Request) string { u, _ := r.Context().Value(userKey{}).(string); return u }

func setSession(w http.ResponseWriter, tok string) {
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: tok, Path: "/", HttpOnly: true, Secure: true,
		SameSite: http.SameSiteStrictMode, MaxAge: int(sessionMax / time.Second)})
}

func fail(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"detail": msg})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func readJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(io.LimitReader(r.Body, 64<<10))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return errors.New("the request is not valid JSON")
	}
	return nil
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	c, _ := s.auth.creds()
	stage := "claim"
	user := ""
	if c != nil {
		stage = "signin"
		if ck, err := r.Cookie(cookieName); err == nil {
			if u, ok := s.auth.session(ck.Value, time.Now()); ok {
				stage, user = "ready", u
			}
		}
	}
	host, _ := os.Hostname()
	name, _, joined := s.identity()
	writeJSON(w, http.StatusOK, map[string]any{"stage": stage, "user": user, "hostname": host, "name": name,
		"joined": joined, "version": s.Version, "cert_sha256": s.certFP})
}

func (s *Server) handleClaim(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Code     string `json:"code"`
		Password string `json:"password"`
	}
	if err := readJSON(r, &in); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	tok, err := s.auth.claim(addrOf(r.RemoteAddr).String(), in.Code, in.Password, time.Now())
	if err != nil {
		code := http.StatusBadRequest
		if errors.Is(err, errLocked) {
			code = http.StatusTooManyRequests
		}
		fail(w, code, err.Error())
		return
	}
	s.change(adminUser, "Set the box console's admin password")
	setSession(w, tok)
	writeJSON(w, http.StatusOK, map[string]string{"stage": "ready", "user": adminUser})
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var in struct {
		User     string `json:"user"`
		Password string `json:"password"`
	}
	if err := readJSON(r, &in); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	ip := addrOf(r.RemoteAddr).String()
	tok, err := s.auth.login(ip, strings.TrimSpace(in.User), in.Password, time.Now())
	if err != nil {
		code := http.StatusUnauthorized
		if errors.Is(err, errLocked) {
			code = http.StatusTooManyRequests
		}
		s.Log.Warn("box console sign-in refused", "from", ip)
		fail(w, code, err.Error())
		return
	}
	s.Log.Info("box console sign-in", "user", in.User, "from", ip)
	setSession(w, tok)
	writeJSON(w, http.StatusOK, map[string]string{"stage": "ready", "user": in.User})
}

func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	if c, err := r.Cookie(cookieName); err == nil {
		s.auth.close(c.Value)
	}
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: "", Path: "/", MaxAge: -1, HttpOnly: true, Secure: true, SameSite: http.SameSiteStrictMode})
	writeJSON(w, http.StatusOK, map[string]string{"stage": "signin"})
}

func (s *Server) handlePassword(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Current string `json:"current"`
		New     string `json:"new"`
	}
	if err := readJSON(r, &in); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.auth.changePassword(userOf(r), in.Current, in.New); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	s.change(userOf(r), "Changed the box console password")
	tok := s.auth.open(userOf(r), time.Now())
	setSession(w, tok)
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// change records something done on the box and reports it to the controller.
func (s *Server) change(by, what string) {
	c := &Change{At: time.Now().UTC(), By: by, What: what}
	s.mu.Lock()
	s.lastChange = c
	s.mu.Unlock()
	s.Log.Info("box console change", "by", by, "what", what)
	s.report("local_change", map[string]string{"by": "box console (" + by + ")", "what": what})
}

func (s *Server) ports() ([]Iface, map[string]bool) {
	ps, err := Ports(s.Root)
	if err != nil {
		s.Log.Warn("listing ports", "err", err)
	}
	have := map[string]bool{}
	s.mu.Lock()
	n := s.net
	s.mu.Unlock()
	for i := range ps {
		have[ps[i].Name] = true
		if n == nil {
			continue
		}
		for _, p := range n.WAN {
			if p.Iface == ps[i].Name {
				ps[i].Role, ps[i].Path = "wan", p.Path
			}
		}
		if n.LAN != nil && n.LAN.Iface == ps[i].Name {
			ps[i].Role = "lan"
		}
	}
	return ps, have
}

type bgpRow struct {
	Tunnel   string `json:"tunnel"`
	Address  string `json:"address"`
	State    string `json:"state"`
	Prefixes int    `json:"prefixes"`
}

func (s *Server) handleOverview(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	host, _ := os.Hostname()
	name, ctrl, joined := s.identity()
	ps, _ := s.ports()
	s.mu.Lock()
	a := s.agent
	n := s.net
	pend := s.pendingInfo()
	last := s.lastChange
	s.mu.Unlock()

	ctl := map[string]any{"joined": joined, "url": ctrl, "name": name, "state": "not joined"}
	out := map[string]any{"hostname": host, "version": s.Version, "uptime_s": uptime(s.Root),
		"setup": map[string]bool{"network": n != nil, "joined": joined}, "ports": ps,
		"tunnels": []agent.TunnelView{}, "bgp": []bgpRow{}, "steering": []any{}, "pending": pend, "last_change": last,
		"console_url": "", "cert_sha256": s.certFP}
	s.mu.Lock()
	out["console_url"] = s.consoleURL()
	s.mu.Unlock()
	if joined {
		ctl["state"] = "starting"
	}
	if a != nil {
		snap := a.Snapshot()
		since := time.Since(snap.LastContact)
		switch {
		case snap.Silent:
			ctl["state"] = "unreachable"
		case snap.AppliedVersion == 0 && since > 30*time.Second:
			ctl["state"] = "unreachable"
		case since < 45*time.Second:
			ctl["state"] = "connected"
		default:
			ctl["state"] = "unreachable"
		}
		ctl["last_contact"] = snap.LastContact
		ctl["applied_version"] = snap.AppliedVersion
		ctl["last_error"] = snap.LastError
		ctl["role"] = snap.Role
		for i := range snap.Tunnels {
			if age, err := frrstate.HandshakeAge(ctx, s.Sys, snap.Tunnels[i].Name, time.Now()); err == nil {
				snap.Tunnels[i].HandshakeAgeS = age
			}
		}
		if snap.Tunnels != nil {
			out["tunnels"] = snap.Tunnels
		}
		if snap.Steering != nil {
			out["steering"] = snap.Steering
		}
		var rows []bgpRow
		for _, nb := range snap.Neighbors {
			row := bgpRow{Tunnel: nb.Tunnel, Address: nb.Address, State: "unknown"}
			if st, err := frrstate.BGPNeighbor(ctx, s.Sys, nb.Address); err == nil && st.State != "" {
				row.State, row.Prefixes = st.State, st.PrefixesReceived
			}
			rows = append(rows, row)
		}
		if rows != nil {
			out["bgp"] = rows
		}
	}
	out["controller"] = ctl
	if b, err := s.Sys.Run(ctx, "ip", "-j", "route", "show"); err == nil {
		if rs, err := ParseRoutes(b); err == nil {
			out["routes"] = rs
		}
	}
	if _, ok := out["routes"]; !ok {
		out["routes"] = []Route{}
	}
	writeJSON(w, http.StatusOK, out)
}

func uptime(root string) int64 {
	b, err := os.ReadFile(filepath.Join(root, "/proc/uptime"))
	if err != nil {
		return 0
	}
	f, _ := strconv.ParseFloat(strings.Fields(string(b) + " 0")[0], 64)
	return int64(f)
}

// pendingInfo describes an unconfirmed port change. Caller holds s.mu.
func (s *Server) pendingInfo() any {
	if s.revert == nil {
		return nil
	}
	return map[string]any{"revert_at": s.revertAt}
}

func (s *Server) handleGetNetwork(w http.ResponseWriter, r *http.Request) {
	ps, _ := s.ports()
	s.mu.Lock()
	var n any
	if s.net != nil {
		n = s.net.Public()
	}
	pend := s.pendingInfo()
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"network": n, "caps": s.Backend.Caps(), "ports": ps, "pending": pend})
}

func (s *Server) handleSetNetwork(w http.ResponseWriter, r *http.Request) {
	var n Network
	if err := readJSON(r, &n); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	_, have := s.ports()
	s.mu.Lock()
	old := s.net
	if old != nil {
		n.keepPasswords(*old)
	}
	s.mu.Unlock()
	if err := n.Validate(have); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	n.UpdatedAt, n.UpdatedBy = time.Now().UTC(), userOf(r)
	s.applyMu.Lock()
	defer s.applyMu.Unlock()
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	if err := s.Backend.Apply(ctx, n); err != nil {
		s.Log.Warn("box console port change failed", "err", err)
		if old != nil {
			if rerr := s.Backend.Apply(context.Background(), *old); rerr != nil {
				s.Log.Error("putting the ports back failed", "err", rerr)
			}
		}
		fail(w, http.StatusBadRequest, "The ports were not changed: "+err.Error())
		return
	}
	if err := saveNetwork(filepath.Join(s.Dir, networkFile), &n); err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.mu.Lock()
	if s.revert == nil {
		// Keep the first unconfirmed change's starting point.
		s.prev = old
	} else {
		s.revert.Stop()
	}
	s.net = &n
	s.revertAt = time.Now().Add(s.RevertAfter)
	s.gen++
	gen := s.gen
	s.revert = time.AfterFunc(s.RevertAfter, func() { s.undo(gen) })
	pend := s.pendingInfo()
	url := s.consoleURL()
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"network": n.Public(), "pending": pend, "console_url": url})
}

// undo puts the ports back when nobody confirmed a change in time, which is
// what happens when a change cuts the console off.
func (s *Server) undo(gen int) {
	s.applyMu.Lock()
	defer s.applyMu.Unlock()
	s.mu.Lock()
	if s.revert == nil || s.gen != gen {
		s.mu.Unlock()
		return
	}
	prev := s.prev
	s.revert, s.prev = nil, nil
	s.net = prev
	s.mu.Unlock()
	p := filepath.Join(s.Dir, networkFile)
	if prev == nil {
		_ = os.Remove(p)
		s.Log.Warn("box console port change not confirmed; nothing earlier to go back to")
		s.change("box console", "Port change not confirmed in time; earlier ports kept on file")
		return
	}
	if err := s.Backend.Apply(context.Background(), *prev); err != nil {
		s.Log.Error("putting the ports back failed", "err", err)
	}
	_ = saveNetwork(p, prev)
	s.change("box console", "Port change not confirmed in time; went back to the earlier ports")
}

func (s *Server) handleConfirm(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	if s.revert == nil {
		s.mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{"confirmed": true})
		return
	}
	s.revert.Stop()
	s.revert, s.prev = nil, nil
	n := s.net
	s.mu.Unlock()
	s.change(userOf(r), describe(n))
	writeJSON(w, http.StatusOK, map[string]any{"confirmed": true})
}

func describe(n *Network) string {
	if n == nil {
		return "Cleared the port set-up"
	}
	var parts []string
	for _, p := range n.WAN {
		parts = append(parts, fmt.Sprintf("%s on %s (%s)", paths[p.Path], p.Iface, modeName(p.Mode)))
	}
	if n.LAN != nil {
		l := fmt.Sprintf("office network on %s", n.LAN.Iface)
		if n.LAN.Address != "" {
			l += " " + n.LAN.Address
		}
		if n.LANDHCP {
			l += " handing out addresses"
		}
		parts = append(parts, l)
	}
	return "Set the ports: " + strings.Join(parts, ", ")
}

func modeName(m string) string {
	switch m {
	case "dhcp":
		return "automatic"
	case "static":
		return "fixed address"
	case "pppoe":
		return "PPPoE"
	}
	return "as it was"
}

func (s *Server) handleJoin(w http.ResponseWriter, r *http.Request) {
	if _, _, ok := s.identity(); ok {
		fail(w, http.StatusConflict, "This box has already joined Connect.")
		return
	}
	var in struct {
		Code        string `json:"code"`
		Controller  string `json:"controller"`
		Token       string `json:"token"`
		Fingerprint string `json:"fingerprint"`
		Name        string `json:"name"`
	}
	if err := readJSON(r, &in); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	var j JoinInfo
	var err error
	if strings.TrimSpace(in.Code) != "" {
		j, err = ParseJoinCode(in.Code)
	} else {
		j = JoinInfo{Controller: in.Controller, Token: in.Token, Fingerprint: in.Fingerprint, Name: in.Name}
		err = j.Check()
	}
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	if s.Join == nil {
		fail(w, http.StatusInternalServerError, "joining is not available")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()
	if err := s.Join(ctx, j); err != nil {
		s.Log.Warn("box console join failed", "err", err)
		fail(w, http.StatusBadRequest, "Could not join: "+joinError(err))
		return
	}
	s.change(userOf(r), "Joined Connect as "+j.Name+" at "+j.Controller)
	s.joinOnce.Do(func() { close(s.joined) })
	writeJSON(w, http.StatusOK, map[string]any{"joined": true, "name": j.Name, "controller": j.Controller})
}

// joinError keeps the controller's own message, which says what was wrong.
func joinError(err error) string {
	msg := err.Error()
	if i := strings.Index(msg, `"detail":"`); i >= 0 {
		rest := msg[i+10:]
		if k := strings.Index(rest, `"`); k > 0 {
			return rest[:k]
		}
	}
	if len(msg) > 300 {
		msg = msg[:300]
	}
	return msg
}

func (s *Server) handleDiag(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Tool   string `json:"tool"`
		Target string `json:"target"`
		Iface  string `json:"iface"`
	}
	if err := readJSON(r, &in); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	if !s.diagMu.TryLock() {
		fail(w, http.StatusTooManyRequests, "Another test is running; try again when it finishes.")
		return
	}
	defer s.diagMu.Unlock()
	_, ctrl, _ := s.identity()
	out, err := Diag(r.Context(), in.Tool, in.Target, in.Iface, ctrl)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"output": out})
}
