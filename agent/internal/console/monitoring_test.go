package console

import (
	"context"
	"encoding/json"
	"net/http"
	"net/netip"
	"strings"
	"sync"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/agent"
)

type fakeMonBackend struct {
	*fakeBackend
	mu      sync.Mutex
	applied []Monitoring
	traps   []string
	logs    []string
}

func (f *fakeMonBackend) monitoringCaps() MonitoringCaps {
	return MonitoringCaps{SNMP: true, Syslog: true}
}
func (f *fakeMonBackend) applyMonitoring(_ context.Context, m Monitoring, _ []netip.Prefix) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.applied = append(f.applied, m)
	return nil
}
func (f *fakeMonBackend) trap(_ context.Context, m Monitoring, kind string, _ map[string]string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, t := range m.Traps {
		f.traps = append(f.traps, kind+"@"+t.Host)
	}
}
func (f *fakeMonBackend) logLine(_ context.Context, msg string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.logs = append(f.logs, msg)
}

func TestMonitoringValidate(t *testing.T) {
	good := Monitoring{SNMP: SNMPSettings{V2c: true, Community: "office-ro-2026", V3: true, V3User: "nms", V3Auth: "auth-pass-1", V3Priv: "priv-pass-2",
		Allowed: []string{"10.20.0.0/24"}, Location: "Kingston branch"},
		Traps:  []TrapTarget{{Host: "10.20.0.5", Port: 162, Version: "v2c", Community: "trap-comm-1"}, {Host: "nms.example.com", Port: 162, Version: "v3"}},
		Syslog: &SyslogTarget{Host: "10.20.0.6", Port: 514, Proto: "udp"}}
	if err := good.Validate(); err != nil {
		t.Fatal(err)
	}
	bad := []func(m *Monitoring){
		func(m *Monitoring) { m.SNMP.Community = "short" },
		func(m *Monitoring) { m.SNMP.Community = `has "quote"` },
		func(m *Monitoring) { m.SNMP.V3Priv = m.SNMP.V3Auth },
		func(m *Monitoring) { m.SNMP.V3User = "1bad" },
		func(m *Monitoring) { m.SNMP.Allowed = []string{"10.20.0.0"} },
		func(m *Monitoring) { m.SNMP.Location = "line\nbreak" },
		func(m *Monitoring) { m.Traps[0].Host = "bad host" },
		func(m *Monitoring) { m.Traps[0].Port = 70000 },
		func(m *Monitoring) { m.Traps[0].Version = "v1" },
		func(m *Monitoring) { m.Syslog.Proto = "tls" },
		func(m *Monitoring) { m.Syslog.Host = "0.0.0.0" },
	}
	for i, f := range bad {
		m := good
		m.SNMP.Allowed = append([]string(nil), good.SNMP.Allowed...)
		m.Traps = append([]TrapTarget(nil), good.Traps...)
		sl := *good.Syslog
		m.Syslog = &sl
		f(&m)
		if m.Validate() == nil {
			t.Errorf("bad settings %d accepted", i)
		}
	}
}

func TestSNMPDConf(t *testing.T) {
	m := Monitoring{EngineID: "80001f88050102030405060708", Internal: "abcdef0123456789",
		SNMP:  SNMPSettings{V2c: true, Community: "office-ro-2026", V3: true, V3User: "nms", V3Auth: "auth-pass-1", V3Priv: "priv-pass-2", Location: "Kingston"},
		Traps: []TrapTarget{{Host: "10.20.0.5", Port: 162, Version: "v2c", Community: "trap-comm-1"}, {Host: "fd00::5", Port: 1162, Version: "v3"}}}
	c := SNMPDConf(m, nil)
	for _, want := range []string{
		"agentAddress udp:161,udp6:161\n",
		"exactEngineID 0x80001f88050102030405060708\n",
		"sysLocation Kingston\n",
		"rocommunity office-ro-2026 10.0.0.0/8 -V all\n",
		"rocommunity6 office-ro-2026 fc00::/7 -V all\n",
		`createUser nms SHA "auth-pass-1" AES "priv-pass-2"` + "\n",
		"rouser nms priv -V all\n",
		"iquerySecName exaInternal\n",
		"linkUpDownNotifications yes\n",
		"trap2sink 10.20.0.5:162 trap-comm-1\n",
		`trapsess -v 3 -u nms -l authPriv -a SHA -A "auth-pass-1" -x AES -X "priv-pass-2" udp6:[fd00::5]:1162` + "\n",
	} {
		if !strings.Contains(c, want) {
			t.Errorf("snmpd.conf lacks %q:\n%s", want, c)
		}
	}
	m.SNMP.V2c, m.SNMP.V3, m.Traps = false, false, nil
	c = SNMPDConf(m, nil)
	if strings.Contains(c, "rocommunity") || strings.Contains(c, "rouser nms") || strings.Contains(c, "createUser nms") {
		t.Errorf("v2c/v3 off still gives access:\n%s", c)
	}
	if r := RsyslogConf(&SyslogTarget{Host: "10.20.0.6", Port: 514, Proto: "tcp"}); !strings.Contains(r, `target="10.20.0.6" port="514" protocol="tcp"`) {
		t.Errorf("rsyslog: %s", r)
	}
}

func TestMonitoringAndAPIKeys(t *testing.T) {
	s, ts, c, fb := newTestServer(t)
	mb := &fakeMonBackend{fakeBackend: fb}
	s.Backend = mb
	code, _ := SetupCode(s.Dir)
	if st, out := call(t, c, ts.URL, "/api/claim", map[string]string{"code": code, "password": "a good long password"}); st != 200 {
		t.Fatalf("claim %d %v", st, out)
	}
	body := map[string]any{"snmp": map[string]any{"v2c": true, "community": "office-ro-2026", "v3": true, "v3_user": "nms", "v3_auth": "auth-pass-1", "v3_priv": "priv-pass-2"},
		"traps": []map[string]any{{"host": "10.20.0.5", "version": "v2c", "community": "trap-comm-1"}}, "syslog": map[string]any{"host": "10.20.0.6"}}
	st, out := call(t, c, ts.URL, "/api/monitoring", body)
	if st != 200 {
		t.Fatalf("set monitoring %d %v", st, out)
	}
	if strings.Contains(strings.ToLower(jsonOf(out)), "office-ro-2026") || strings.Contains(jsonOf(out), "auth-pass-1") {
		t.Fatalf("secrets come back: %v", out)
	}
	if e, _ := out["engine_id"].(string); !strings.HasPrefix(e, "0x80001f8805") {
		t.Fatalf("engine id %v", out["engine_id"])
	}
	m := mb.applied[len(mb.applied)-1]
	if m.Traps[0].Port != 162 || m.Syslog.Port != 514 || m.Syslog.Proto != "udp" || m.Internal == "" {
		t.Fatalf("defaults not filled: %+v", m)
	}
	// Saving again without the secrets keeps them.
	body["snmp"] = map[string]any{"v2c": true, "v3": true, "v3_user": "nms", "location": "Kingston"}
	body["traps"] = []map[string]any{{"host": "10.20.0.5", "port": 162, "version": "v2c"}}
	if st, out := call(t, c, ts.URL, "/api/monitoring", body); st != 200 {
		t.Fatalf("resave %d %v", st, out)
	}
	m2 := mb.applied[len(mb.applied)-1]
	if m2.SNMP.Community != "office-ro-2026" || m2.SNMP.V3Auth != "auth-pass-1" || m2.Traps[0].Community != "trap-comm-1" || m2.EngineID != m.EngineID {
		t.Fatalf("secrets or engine id lost: %+v", m2)
	}
	if st, out := call(t, c, ts.URL, "/api/monitoring", map[string]any{"snmp": map[string]any{"v2c": true, "community": "bad one"}}); st != 400 {
		t.Fatalf("bad community answered %d %v", st, out)
	}
	if st, _ := call(t, c, ts.URL, "/api/monitoring/test", map[string]any{}); st != 200 || len(mb.traps) == 0 || len(mb.logs) == 0 {
		t.Fatalf("test message %d traps %v logs %v", st, mb.traps, mb.logs)
	}
	// Agent events become traps.
	n := len(mb.traps)
	s.Notify(agentEvent("bfd_down"))
	if len(mb.traps) != n+1 || mb.traps[n] != "bfd_down@10.20.0.5" {
		t.Fatalf("event trap %v", mb.traps)
	}

	// A read-only API key: shown once, reads, never writes.
	st, k := call(t, c, ts.URL, "/api/apikeys", map[string]string{"name": "Zabbix"})
	key, _ := k["key"].(string)
	if st != 200 || !strings.HasPrefix(key, apiKeyPrefix) {
		t.Fatalf("new key %d %v", st, k)
	}
	if _, v := call(t, c, ts.URL, "/api/monitoring", nil); strings.Contains(jsonOf(v), key) {
		t.Fatal("the key is listed")
	}
	bare := ts.Client()
	get := func(path, tok string, post bool) int {
		method := http.MethodGet
		if post {
			method = http.MethodPost
		}
		req, _ := http.NewRequest(method, ts.URL+path, strings.NewReader("{}"))
		req.Header.Set("Authorization", "Bearer "+tok)
		req.Header.Set("X-Exa-Box", "1")
		resp, err := bare.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if st := get("/api/overview", key, false); st != 200 {
		t.Fatalf("API key read overview: %d", st)
	}
	if st := get("/api/monitoring", key+"x", false); st != 401 {
		t.Fatalf("wrong key answered %d", st)
	}
	if st := get("/api/apikeys", key, true); st != 401 {
		t.Fatalf("API key wrote: %d", st)
	}
	if st, _ := call(t, c, ts.URL, "/api/apikeys/revoke", map[string]string{"id": k["id"].(string)}); st != 200 {
		t.Fatalf("revoke %d", st)
	}
	if st := get("/api/overview", key, false); st != 401 {
		t.Fatalf("revoked key answered %d", st)
	}
}

func jsonOf(v any) string { b, _ := json.Marshal(v); return string(b) }

func agentEvent(kind string) agent.Event { return agent.Event{Kind: kind} }
