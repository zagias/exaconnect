package steer

import (
	"encoding/json"
	"testing"
)

func f(v float64) *float64 { return &v }

func voiceSLA() *SLA { return &SLA{MaxLatencyMS: f(150), MaxJitterMS: f(30), MaxLossPct: f(1)} }

func TestSLASeverityAndBreach(t *testing.T) {
	s := voiceSLA()
	for _, tc := range []struct {
		lat, jit, loss float64
		replies        bool
		breach         bool
	}{
		{50, 5, 0, true, false},
		{150, 30, 1, true, false}, // at the limit is within it
		{151, 5, 0, true, true},
		{50, 31, 0, true, true},
		{50, 5, 1.2, true, true},
		{0, 0, 100, false, true}, // nothing came back
	} {
		if got := s.Breached(tc.lat, tc.jit, tc.loss, tc.replies); got != tc.breach {
			t.Errorf("%+v: breached = %v", tc, got)
		}
	}
	if got := s.Severity(75, 15, 2, true); got != 2 {
		t.Fatalf("severity = %v, want 2 (loss is twice its limit)", got)
	}
	var none *SLA
	if none.Breached(5000, 500, 50, false) {
		t.Fatal("a class without an SLA breached")
	}
	if (&SLA{MaxLossPct: f(5)}).Breached(900, 200, 1, true) {
		t.Fatal("a loss-only SLA checked latency")
	}
}

func TestSLAIsOptionalInTheMap(t *testing.T) {
	// A map from an older controller has no "sla": it still loads.
	var m Map
	if err := json.Unmarshal([]byte(`{"version":1,"paths":[{"name":"carrier-a","tunnel":"wg-a","table":101}],
		"classes":[{"name":"voice","mark":257,"dscp":[46],"ports":[],"subnets":[]}],
		"rules":[{"class":"voice","paths":["carrier-a"]}],"local_prefixes":[]}`), &m); err != nil {
		t.Fatal(err)
	}
	if err := m.Validate(); err != nil || m.Classes[0].SLA != nil {
		t.Fatalf("old map: %v %+v", err, m.Classes[0].SLA)
	}
	// And a new one round-trips its limits; unset limits stay unset.
	m.Classes[0].SLA = &SLA{MaxLatencyMS: f(250), MaxLossPct: f(2)}
	b, _ := json.Marshal(m)
	var back Map
	json.Unmarshal(b, &back)
	if s := back.Classes[0].SLA; s == nil || *s.MaxLatencyMS != 250 || s.MaxJitterMS != nil || *s.MaxLossPct != 2 {
		t.Fatalf("round trip: %s", b)
	}
	m.Classes[0].SLA.MaxLossPct = f(-1)
	if m.Validate() == nil {
		t.Fatal("negative limit accepted")
	}
}

func TestChooseHealthySkipsBreachingPaths(t *testing.T) {
	m := siteMap()
	up := map[string]bool{"carrier-a": true, "carrier-b": true, "sat": true}
	usable := func(p string) bool { return up[p] }
	bad := map[string]bool{}
	healthy := func(c, p string) bool { return !bad[c+"|"+p] }
	sev := map[string]float64{}
	severity := func(c, p string) float64 { return sev[c+"|"+p] }
	byClass := func(cs []Choice) map[string]Choice {
		out := map[string]Choice{}
		for _, c := range cs {
			out[c.Class] = c
		}
		return out
	}

	// Nothing breaching: the same as Choose.
	if got, want := ChooseHealthy(m, usable, healthy, severity), Choose(m, usable); len(got) != len(want) || got[0] != want[0] || got[2] != want[2] {
		t.Fatalf("got %+v want %+v", got, want)
	}

	// Voice breaches on carrier-b: it moves to carrier-a; business on carrier-a is unaffected.
	bad["voice|carrier-b"] = true
	got := byClass(ChooseHealthy(m, usable, healthy, severity))
	if got["voice"].Path != "carrier-a" || !got["voice"].Failover || got["business"].Path != "carrier-a" {
		t.Fatalf("voice off carrier-b: %+v", got)
	}

	// Every path breaches for voice: it keeps the least bad one, never strands.
	bad["voice|carrier-a"], bad["voice|sat"] = true, true
	sev["voice|carrier-b"], sev["voice|carrier-a"], sev["voice|sat"] = 1.4, 3, 8
	got = byClass(ChooseHealthy(m, usable, healthy, severity))
	if got["voice"].Path != "carrier-b" || got["voice"].Paused {
		t.Fatalf("all breaching: %+v", got["voice"])
	}
	sev["voice|carrier-a"] = 1.1
	if got = byClass(ChooseHealthy(m, usable, healthy, severity)); got["voice"].Path != "carrier-a" {
		t.Fatalf("least bad: %+v", got["voice"])
	}

	// A breaching path that is down is not a candidate at all.
	up["carrier-a"] = false
	if got = byClass(ChooseHealthy(m, usable, healthy, severity)); got["voice"].Path != "carrier-b" {
		t.Fatalf("down path kept: %+v", got["voice"])
	}

	// Bulk with only down paths still pauses as before.
	up["carrier-b"] = false
	if got = byClass(ChooseHealthy(m, usable, healthy, severity)); !got["bulk"].Paused || got["bulk"].Path != "" {
		t.Fatalf("bulk: %+v", got["bulk"])
	}
}
