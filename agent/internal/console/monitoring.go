package console

// Monitoring the box itself (ADR 0048): an SNMP agent (v2c and v3) that the
// customer's monitoring system polls from the office side or over Connect,
// SNMP traps for link changes and the agent's events, syslog to the
// customer's collector, and read-only API keys for the console's API. The
// box runs net-snmp's snmpd and rsyslog; the console writes their settings.

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base32"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/netip"
	"os"
	"os/user"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/zagias/exaconnect/agent/internal/agent"
)

const (
	monitoringFile = "console-monitoring.json"
	apiKeysFile    = "console-apikeys.json"
	snmpdConf      = "/etc/snmp/exacarib-snmpd.conf"
	rsyslogConf    = "/etc/rsyslog.d/90-exacarib-forward.conf"
	apiKeyPrefix   = "exabox_"
	maxAPIKeys     = 10
)

// Monitoring is what the console keeps for the box's own monitoring.
type Monitoring struct {
	SNMP     SNMPSettings  `json:"snmp"`
	Traps    []TrapTarget  `json:"traps,omitempty"`
	Syslog   *SyslogTarget `json:"syslog,omitempty"`
	EngineID string        `json:"engine_id"`
	Internal string        `json:"internal"` // snmpd's own user, for link and threshold traps
}

// SNMPSettings: v2c with a community, v3 with one user (SHA and AES), or both.
type SNMPSettings struct {
	V2c       bool     `json:"v2c"`
	Community string   `json:"community,omitempty"`
	V3        bool     `json:"v3"`
	V3User    string   `json:"v3_user,omitempty"`
	V3Auth    string   `json:"v3_auth,omitempty"`
	V3Priv    string   `json:"v3_priv,omitempty"`
	Allowed   []string `json:"allowed,omitempty"` // who may poll; empty: private addresses
	Location  string   `json:"location,omitempty"`
	Contact   string   `json:"contact,omitempty"`
}

// TrapTarget is where traps go. v3 traps use the v3 user.
type TrapTarget struct {
	Host      string `json:"host"`
	Port      int    `json:"port"`
	Version   string `json:"version"` // v2c | v3
	Community string `json:"community,omitempty"`
}

// SyslogTarget is the customer's syslog collector.
type SyslogTarget struct {
	Host  string `json:"host"`
	Port  int    `json:"port"`
	Proto string `json:"proto"` // udp | tcp
}

func (m *Monitoring) snmpOn() bool { return m.SNMP.V2c || m.SNMP.V3 || len(m.Traps) > 0 }

var (
	secretRE = regexp.MustCompile(`^[A-Za-z0-9!$%&()*+,\-./:;<=>?@\[\]^_{|}~]{8,64}$`)
	userRE   = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,31}$`)
	hostRE   = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$`)
	textRE   = regexp.MustCompile(`^[^"\\\x00-\x1f\x7f]{0,120}$`)
)

func validHost(h string) bool {
	if a, err := netip.ParseAddr(h); err == nil {
		return !a.IsUnspecified() && !a.IsMulticast()
	}
	return len(h) <= 253 && hostRE.MatchString(h) && strings.ContainsAny(h, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
}

// Validate checks settings before anything is written; messages are for people.
func (m *Monitoring) Validate() error {
	s := m.SNMP
	if s.V2c && !secretRE.MatchString(s.Community) {
		return errors.New("The community needs 8 to 64 letters, digits or symbols, with no spaces or quotes.")
	}
	if s.V3 || hasV3Trap(m.Traps) {
		if !userRE.MatchString(s.V3User) {
			return errors.New("The SNMPv3 user name needs a letter first, then up to 31 letters, digits, dots, dashes or underscores.")
		}
		if !secretRE.MatchString(s.V3Auth) || !secretRE.MatchString(s.V3Priv) {
			return errors.New("The SNMPv3 passwords need 8 to 64 letters, digits or symbols, with no spaces or quotes.")
		}
		if s.V3Auth == s.V3Priv {
			return errors.New("Use different SNMPv3 authentication and privacy passwords.")
		}
	}
	if len(s.Allowed) > 16 {
		return errors.New("Allow at most 16 address ranges to poll the box.")
	}
	for _, a := range s.Allowed {
		if _, err := netip.ParsePrefix(a); err != nil {
			return fmt.Errorf("%q is not an address range such as 10.20.0.0/24.", a)
		}
	}
	if !textRE.MatchString(s.Location) || !textRE.MatchString(s.Contact) {
		return errors.New("Location and contact can have up to 120 characters, without quotes or backslashes.")
	}
	if len(m.Traps) > 4 {
		return errors.New("Send traps to at most 4 places.")
	}
	for _, t := range m.Traps {
		if !validHost(t.Host) {
			return fmt.Errorf("%q is not an address or host name.", t.Host)
		}
		if t.Port < 1 || t.Port > 65535 {
			return errors.New("A trap port is a number from 1 to 65535 (usually 162).")
		}
		switch t.Version {
		case "v2c":
			if !secretRE.MatchString(t.Community) {
				return errors.New("Each v2c trap destination needs a community of 8 to 64 characters, with no spaces or quotes.")
			}
		case "v3":
		default:
			return errors.New("A trap destination is v2c or v3.")
		}
	}
	if l := m.Syslog; l != nil {
		if !validHost(l.Host) {
			return fmt.Errorf("%q is not an address or host name.", l.Host)
		}
		if l.Port < 1 || l.Port > 65535 {
			return errors.New("The syslog port is a number from 1 to 65535 (usually 514).")
		}
		if l.Proto != "udp" && l.Proto != "tcp" {
			return errors.New("Syslog goes over UDP or TCP.")
		}
	}
	return nil
}

func hasV3Trap(ts []TrapTarget) bool {
	for _, t := range ts {
		if t.Version == "v3" {
			return true
		}
	}
	return false
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// SNMPDConf renders snmpd's settings. The engine ID is fixed per box, so a
// monitoring system can be told it once for v3 traps.
func SNMPDConf(m Monitoring, office []netip.Prefix) string {
	var b strings.Builder
	b.WriteString("# Written by the ExaCarib box console (ADR 0048). Changes here are replaced.\n")
	b.WriteString("agentAddress udp:161,udp6:161\n")
	fmt.Fprintf(&b, "exactEngineID 0x%s\n", m.EngineID)
	if m.SNMP.Location != "" {
		fmt.Fprintf(&b, "sysLocation %s\n", m.SNMP.Location)
	}
	if m.SNMP.Contact != "" {
		fmt.Fprintf(&b, "sysContact %s\n", m.SNMP.Contact)
	}
	b.WriteString("sysServices 76\nview all included .1\n")
	allowed := m.SNMP.Allowed
	if len(allowed) == 0 {
		allowed = []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"}
		for _, p := range office {
			if !p.Addr().IsPrivate() {
				allowed = append(allowed, p.Masked().String())
			}
		}
	}
	if m.SNMP.V2c {
		for _, a := range allowed {
			kw := "rocommunity"
			if strings.Contains(a, ":") {
				kw = "rocommunity6"
			}
			fmt.Fprintf(&b, "%s %s %s -V all\n", kw, m.SNMP.Community, a)
		}
	}
	if m.SNMP.V3 || hasV3Trap(m.Traps) {
		fmt.Fprintf(&b, "createUser %s SHA \"%s\" AES \"%s\"\n", m.SNMP.V3User, m.SNMP.V3Auth, m.SNMP.V3Priv)
	}
	if m.SNMP.V3 {
		fmt.Fprintf(&b, "rouser %s priv -V all\n", m.SNMP.V3User)
	}
	// snmpd's own link checks query it as this user. (defaultMonitors needs MIB
	// files Debian does not ship; disk and load are read by polling UCD-SNMP.)
	fmt.Fprintf(&b, "createUser exaInternal SHA \"%s\"\nrouser exaInternal auth -V all\niquerySecName exaInternal\n", m.Internal)
	b.WriteString("linkUpDownNotifications yes\nauthtrapenable 1\n")
	for _, t := range m.Traps {
		if t.Version == "v3" {
			fmt.Fprintf(&b, "trapsess -v 3 -u %s -l authPriv -a SHA -A \"%s\" -x AES -X \"%s\" %s\n", m.SNMP.V3User, m.SNMP.V3Auth, m.SNMP.V3Priv, trapAddr(t))
		} else {
			fmt.Fprintf(&b, "trap2sink %s %s\n", trapAddr(t), t.Community)
		}
	}
	return b.String()
}

func trapAddr(t TrapTarget) string {
	h := t.Host
	if strings.Contains(h, ":") {
		h = "udp6:[" + h + "]"
	}
	return h + ":" + strconv.Itoa(t.Port)
}

// RsyslogConf forwards the box's log to the collector, queued while it is away.
func RsyslogConf(l *SyslogTarget) string {
	return fmt.Sprintf("# Written by the ExaCarib box console (ADR 0048). Changes here are replaced.\n"+
		"*.info action(type=\"omfwd\" target=\"%s\" port=\"%d\" protocol=\"%s\" template=\"RSYSLOG_SyslogProtocol23Format\"\n"+
		"  queue.type=\"LinkedList\" queue.size=\"10000\" action.resumeRetryCount=\"-1\")\n", l.Host, l.Port, l.Proto)
}

// MonitoringCaps says what this box can do.
type MonitoringCaps struct {
	SNMP   bool `json:"snmp"`
	Syslog bool `json:"syslog"`
}

func (h HostBackend) monitoringCaps() MonitoringCaps {
	_, errS := os.Stat(h.file("/usr/sbin/snmpd"))
	_, errR := os.Stat(h.file("/usr/sbin/rsyslogd"))
	return MonitoringCaps{SNMP: errS == nil && h.has("systemctl"), Syslog: errR == nil && h.has("systemctl")}
}

// applyMonitoring writes snmpd's and rsyslog's settings and (re)starts them.
func (h HostBackend) applyMonitoring(ctx context.Context, m Monitoring, office []netip.Prefix) error {
	caps := h.monitoringCaps()
	if m.snmpOn() && !caps.SNMP {
		return errors.New("this box has no SNMP agent (the snmpd package)")
	}
	if m.Syslog != nil && !caps.Syslog {
		return errors.New("this box has no rsyslog")
	}
	if caps.SNMP {
		conf := h.file(snmpdConf)
		if m.snmpOn() {
			if err := os.MkdirAll(filepath.Dir(conf), 0o755); err != nil {
				return err
			}
			if err := h.Sys.WriteFile(conf, []byte(SNMPDConf(m, office)), 0o640); err != nil {
				return err
			}
			if g, err := user.LookupGroup("Debian-snmp"); err == nil {
				if gid, err := strconv.Atoi(g.Gid); err == nil {
					_ = os.Chown(conf, 0, gid)
				}
			}
			if _, err := h.Sys.Run(ctx, "systemctl", "enable", "snmpd"); err != nil {
				return fmt.Errorf("SNMP agent: %w", err)
			}
			if _, err := h.Sys.Run(ctx, "systemctl", "restart", "snmpd"); err != nil {
				return fmt.Errorf("SNMP agent did not start: %w", err)
			}
		} else {
			_, _ = h.Sys.Run(ctx, "systemctl", "disable", "--now", "snmpd")
			_ = os.Remove(conf)
		}
	}
	if caps.Syslog {
		conf := h.file(rsyslogConf)
		_, statErr := os.Stat(conf)
		if m.Syslog != nil {
			if err := h.Sys.WriteFile(conf, []byte(RsyslogConf(m.Syslog)), 0o644); err != nil {
				return err
			}
		} else if statErr == nil {
			_ = os.Remove(conf)
		} else {
			return nil
		}
		if _, err := h.Sys.Run(ctx, "systemctl", "restart", "rsyslog"); err != nil {
			return fmt.Errorf("syslog did not restart: %w", err)
		}
	}
	return nil
}

// monitoringBackend is what a backend needs for monitoring; HostBackend has it.
type monitoringBackend interface {
	monitoringCaps() MonitoringCaps
	applyMonitoring(ctx context.Context, m Monitoring, office []netip.Prefix) error
	trap(ctx context.Context, m Monitoring, kind string, detail map[string]string)
	logLine(ctx context.Context, msg string)
}

func (s *Server) monitor() (monitoringBackend, bool) {
	b, ok := s.Backend.(monitoringBackend)
	return b, ok
}

func loadMonitoring(path string) (*Monitoring, error) {
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var m Monitoring
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

func (s *Server) monitoring() Monitoring {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.mon != nil {
		return *s.mon
	}
	return Monitoring{}
}

// officePrefixes are the office port's networks, which may always poll.
func (s *Server) officePrefixes() []netip.Prefix {
	s.mu.Lock()
	n := s.net
	s.mu.Unlock()
	if n == nil || n.LAN == nil {
		return nil
	}
	return ifacePrefixes(n.LAN.Iface)
}

// The box's trap tree: 1.3.6.1.4.1.<PEN>.2 (the controller's is .1).
func boxOID() string {
	pen := os.Getenv("EXA_IANA_PEN")
	if _, err := strconv.Atoi(pen); err != nil || pen == "" {
		pen = "32473"
	}
	return "1.3.6.1.4.1." + pen + ".2"
}

var trapNo = map[string]int{"bfd_down": 1, "bfd_up": 2, "class_moved": 3, "controller_silent": 4, "controller_back": 5,
	"internet_exit_changed": 6, "local_change": 7, "steering_failed": 8, "cert_renew_failed": 9, "test": 99}

// trap sends one ExaCarib trap to every destination with net-snmp's snmptrap.
func (h HostBackend) trap(ctx context.Context, m Monitoring, kind string, detail map[string]string) {
	if len(m.Traps) == 0 || !h.has("snmptrap") {
		return
	}
	n, ok := trapNo[kind]
	if !ok {
		n = 50
	}
	keys := make([]string, 0, len(detail))
	for k := range detail {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		parts = append(parts, k+"="+detail[k])
	}
	b := boxOID()
	vars := []string{b + ".1.1", "s", kind, b + ".1.2", "s", strings.Join(parts, " "), b + ".1.3", "s", hostname()}
	for _, t := range m.Traps {
		args := []string{"-t", "3", "-r", "1"}
		if t.Version == "v3" {
			args = append(args, "-v", "3", "-e", "0x"+m.EngineID, "-u", m.SNMP.V3User, "-l", "authPriv", "-a", "SHA", "-A", m.SNMP.V3Auth, "-x", "AES", "-X", m.SNMP.V3Priv)
		} else {
			args = append(args, "-v", "2c", "-c", t.Community)
		}
		args = append(args, trapAddr(t), "", b+".0."+strconv.Itoa(n))
		args = append(args, vars...)
		cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		_, _ = h.Sys.Run(cctx, "snmptrap", args...)
		cancel()
	}
}

func (h HostBackend) logLine(ctx context.Context, msg string) {
	if h.has("logger") {
		_, _ = h.Sys.Run(ctx, "logger", "-t", "exacarib-box", "-p", "user.notice", msg)
	}
}

func writeFileAtomic(path string, b []byte, mode os.FileMode) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, mode); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func hostname() string { n, _ := os.Hostname(); return n }

// Notify is the agent's event hook: each event also goes out as a trap.
func (s *Server) Notify(e agent.Event) {
	mb, ok := s.monitor()
	if !ok {
		return
	}
	m := s.monitoring()
	if len(m.Traps) == 0 {
		return
	}
	mb.trap(context.Background(), m, e.Kind, e.Detail)
}

// ---- API ----

type snmpView struct {
	V2c          bool     `json:"v2c"`
	CommunitySet bool     `json:"community_set"`
	V3           bool     `json:"v3"`
	V3User       string   `json:"v3_user"`
	V3Set        bool     `json:"v3_passwords_set"`
	Allowed      []string `json:"allowed"`
	Location     string   `json:"location"`
	Contact      string   `json:"contact"`
}

type trapView struct {
	Host         string `json:"host"`
	Port         int    `json:"port"`
	Version      string `json:"version"`
	CommunitySet bool   `json:"community_set,omitempty"`
}

func (s *Server) monitoringView() map[string]any {
	m := s.monitoring()
	caps := MonitoringCaps{}
	if mb, ok := s.monitor(); ok {
		caps = mb.monitoringCaps()
	}
	sv := snmpView{V2c: m.SNMP.V2c, CommunitySet: m.SNMP.Community != "", V3: m.SNMP.V3, V3User: m.SNMP.V3User,
		V3Set: m.SNMP.V3Auth != "", Allowed: m.SNMP.Allowed, Location: m.SNMP.Location, Contact: m.SNMP.Contact}
	if sv.Allowed == nil {
		sv.Allowed = []string{}
	}
	traps := []trapView{}
	for _, t := range m.Traps {
		traps = append(traps, trapView{Host: t.Host, Port: t.Port, Version: t.Version, CommunitySet: t.Community != ""})
	}
	eng := m.EngineID
	if eng == "" {
		eng = s.engineID()
	}
	return map[string]any{"caps": caps, "snmp": sv, "traps": traps, "syslog": m.Syslog, "engine_id": "0x" + eng,
		"trap_oid": boxOID(), "api_keys": s.apiKeyList()}
}

func (s *Server) engineID() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.mon != nil && s.mon.EngineID != "" {
		return s.mon.EngineID
	}
	if s.pendingEngine == "" {
		// RFC 3411: enterprise 8072 (net-snmp), format 5 (octets), 8 random bytes.
		s.pendingEngine = "80001f8805" + randomHex(8)
	}
	return s.pendingEngine
}

func (s *Server) handleGetMonitoring(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.monitoringView())
}

// monitoringReq: secrets left empty keep what the box has.
type monitoringReq struct {
	SNMP struct {
		V2c       bool     `json:"v2c"`
		Community string   `json:"community"`
		V3        bool     `json:"v3"`
		V3User    string   `json:"v3_user"`
		V3Auth    string   `json:"v3_auth"`
		V3Priv    string   `json:"v3_priv"`
		Allowed   []string `json:"allowed"`
		Location  string   `json:"location"`
		Contact   string   `json:"contact"`
	} `json:"snmp"`
	Traps  []TrapTarget  `json:"traps"`
	Syslog *SyslogTarget `json:"syslog"`
}

func (s *Server) handleSetMonitoring(w http.ResponseWriter, r *http.Request) {
	mb, ok := s.monitor()
	if !ok {
		fail(w, http.StatusConflict, "This box cannot set up monitoring.")
		return
	}
	var q monitoringReq
	if err := readJSON(r, &q); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	old := s.monitoring()
	m := Monitoring{EngineID: old.EngineID, Internal: old.Internal}
	if m.EngineID == "" {
		m.EngineID = s.engineID()
	}
	if m.Internal == "" {
		m.Internal = randomHex(16)
	}
	m.SNMP = SNMPSettings{V2c: q.SNMP.V2c, Community: q.SNMP.Community, V3: q.SNMP.V3, V3User: strings.TrimSpace(q.SNMP.V3User),
		V3Auth: q.SNMP.V3Auth, V3Priv: q.SNMP.V3Priv, Location: strings.TrimSpace(q.SNMP.Location), Contact: strings.TrimSpace(q.SNMP.Contact)}
	if m.SNMP.Community == "" {
		m.SNMP.Community = old.SNMP.Community
	}
	if m.SNMP.V3Auth == "" && m.SNMP.V3User == old.SNMP.V3User {
		m.SNMP.V3Auth = old.SNMP.V3Auth
	}
	if m.SNMP.V3Priv == "" && m.SNMP.V3User == old.SNMP.V3User {
		m.SNMP.V3Priv = old.SNMP.V3Priv
	}
	for _, a := range q.SNMP.Allowed {
		if a = strings.TrimSpace(a); a != "" {
			if pf, err := netip.ParsePrefix(a); err == nil {
				a = pf.Masked().String()
			} else if ad, err := netip.ParseAddr(a); err == nil {
				a = netip.PrefixFrom(ad, ad.BitLen()).String()
			}
			m.SNMP.Allowed = append(m.SNMP.Allowed, a)
		}
	}
	for i, t := range q.Traps {
		t.Host = strings.TrimSpace(t.Host)
		if t.Port == 0 {
			t.Port = 162
		}
		if t.Version == "v2c" && t.Community == "" {
			// Keep the community this destination had.
			for _, o := range old.Traps {
				if o.Host == t.Host && o.Port == t.Port && o.Version == "v2c" {
					t.Community = o.Community
				}
			}
			if t.Community == "" && m.SNMP.Community != "" {
				t.Community = m.SNMP.Community
			}
		}
		q.Traps[i] = t
	}
	m.Traps = q.Traps
	if l := q.Syslog; l != nil && strings.TrimSpace(l.Host) != "" {
		l.Host = strings.TrimSpace(l.Host)
		if l.Port == 0 {
			l.Port = 514
		}
		if l.Proto == "" {
			l.Proto = "udp"
		}
		m.Syslog = l
	}
	if err := m.Validate(); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	s.monMu.Lock()
	defer s.monMu.Unlock()
	if err := mb.applyMonitoring(r.Context(), m, s.officePrefixes()); err != nil {
		// Put back what was there.
		if old.EngineID != "" {
			_ = mb.applyMonitoring(r.Context(), old, s.officePrefixes())
		}
		fail(w, http.StatusBadGateway, "Monitoring was not changed: "+err.Error())
		return
	}
	b, _ := json.MarshalIndent(m, "", "  ")
	if err := writeFileAtomic(filepath.Join(s.Dir, monitoringFile), b, 0o600); err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.mu.Lock()
	s.mon = &m
	s.mu.Unlock()
	what := []string{}
	if m.SNMP.V2c {
		what = append(what, "SNMP v2c")
	}
	if m.SNMP.V3 {
		what = append(what, "SNMPv3")
	}
	if len(m.Traps) > 0 {
		what = append(what, fmt.Sprintf("traps to %d place(s)", len(m.Traps)))
	}
	if m.Syslog != nil {
		what = append(what, "syslog to "+m.Syslog.Host)
	}
	summary := "Monitoring off"
	if len(what) > 0 {
		summary = "Monitoring: " + strings.Join(what, ", ")
	}
	s.change(userOf(r), summary)
	writeJSON(w, http.StatusOK, s.monitoringView())
}

func (s *Server) handleTestMonitoring(w http.ResponseWriter, r *http.Request) {
	mb, ok := s.monitor()
	if !ok {
		fail(w, http.StatusConflict, "This box cannot set up monitoring.")
		return
	}
	m := s.monitoring()
	if len(m.Traps) == 0 && m.Syslog == nil {
		fail(w, http.StatusBadRequest, "Add a trap destination or a syslog collector first.")
		return
	}
	msg := "ExaCarib box test message from " + hostname()
	mb.trap(r.Context(), m, "test", map[string]string{"by": userOf(r)})
	mb.logLine(r.Context(), msg)
	writeJSON(w, http.StatusOK, map[string]string{"detail": "Sent a test trap and a test syslog message."})
}

// ---- read-only API keys ----

type apiKey struct {
	ID       string    `json:"id"`
	Name     string    `json:"name"`
	Hash     string    `json:"hash"`
	Created  time.Time `json:"created"`
	LastUsed time.Time `json:"last_used,omitempty"`
}

func (s *Server) loadAPIKeys() []apiKey {
	b, err := os.ReadFile(filepath.Join(s.Dir, apiKeysFile))
	if err != nil {
		return nil
	}
	var ks []apiKey
	_ = json.Unmarshal(b, &ks)
	return ks
}

func (s *Server) saveAPIKeys(ks []apiKey) error {
	b, _ := json.MarshalIndent(ks, "", "  ")
	return writeFileAtomic(filepath.Join(s.Dir, apiKeysFile), b, 0o600)
}

func (s *Server) apiKeyList() []map[string]any {
	s.keyMu.Lock()
	defer s.keyMu.Unlock()
	out := []map[string]any{}
	for _, k := range s.loadAPIKeys() {
		out = append(out, map[string]any{"id": k.ID, "name": k.Name, "created": k.Created, "last_used": k.LastUsed})
	}
	return out
}

func keyHash(tok string) string { h := sha256.Sum256([]byte(tok)); return hex.EncodeToString(h[:]) }

// apiKeyUser checks a bearer key; read-only requests only.
func (s *Server) apiKeyUser(r *http.Request) (string, bool) {
	tok, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !ok || !strings.HasPrefix(tok, apiKeyPrefix) {
		return "", false
	}
	h := keyHash(strings.TrimSpace(tok))
	s.keyMu.Lock()
	defer s.keyMu.Unlock()
	ks := s.loadAPIKeys()
	for i, k := range ks {
		if subtle.ConstantTimeCompare([]byte(k.Hash), []byte(h)) == 1 {
			if time.Since(k.LastUsed) > time.Minute {
				ks[i].LastUsed = time.Now().UTC()
				_ = s.saveAPIKeys(ks)
			}
			return "API key " + k.Name, true
		}
	}
	return "", false
}

func (s *Server) handleNewAPIKey(w http.ResponseWriter, r *http.Request) {
	var q struct {
		Name string `json:"name"`
	}
	if err := readJSON(r, &q); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	q.Name = strings.TrimSpace(q.Name)
	if q.Name == "" || !textRE.MatchString(q.Name) || len(q.Name) > 60 {
		fail(w, http.StatusBadRequest, "Give the key a name of up to 60 characters, such as the monitoring system it is for.")
		return
	}
	s.keyMu.Lock()
	ks := s.loadAPIKeys()
	if len(ks) >= maxAPIKeys {
		s.keyMu.Unlock()
		fail(w, http.StatusBadRequest, "This box has 10 API keys already; remove one first.")
		return
	}
	raw := make([]byte, 20)
	_, _ = rand.Read(raw)
	tok := apiKeyPrefix + strings.ToLower(base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(raw))
	k := apiKey{ID: randomHex(6), Name: q.Name, Hash: keyHash(tok), Created: time.Now().UTC()}
	err := s.saveAPIKeys(append(ks, k))
	s.keyMu.Unlock()
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.change(userOf(r), "Read-only API key added: "+q.Name)
	writeJSON(w, http.StatusOK, map[string]string{"id": k.ID, "name": k.Name, "key": tok})
}

func (s *Server) handleRevokeAPIKey(w http.ResponseWriter, r *http.Request) {
	var q struct {
		ID string `json:"id"`
	}
	if err := readJSON(r, &q); err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	s.keyMu.Lock()
	ks := s.loadAPIKeys()
	name := ""
	out := ks[:0]
	for _, k := range ks {
		if k.ID == q.ID {
			name = k.Name
			continue
		}
		out = append(out, k)
	}
	var err error
	if name != "" {
		err = s.saveAPIKeys(out)
	}
	s.keyMu.Unlock()
	if name == "" {
		fail(w, http.StatusNotFound, "There is no such key.")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.change(userOf(r), "Read-only API key removed: "+name)
	writeJSON(w, http.StatusOK, map[string]string{"detail": "Removed."})
}
