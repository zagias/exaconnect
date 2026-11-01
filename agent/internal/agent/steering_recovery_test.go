package agent

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/apply"
	"github.com/zagias/exaconnect/agent/internal/client"
)

// Fail the real steering-rule batch once, after earlier dataplane steps ran.
// A retry must reconcile the partially applied map, not just clear an error.
type steeringFailureSys struct {
	*fakeSys
	failRules int
	ruleRuns  int
	failWG    int
	failBFD   bool
}

func (s *steeringFailureSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	if name == "wg" && len(args) > 0 && args[0] == "syncconf" && s.failWG > 0 {
		s.failWG--
		return nil, errors.New("injected tunnel apply failure")
	}
	if name == "vtysh" && s.failBFD {
		return nil, errors.New("injected BFD read failure")
	}
	if name == "ip" && len(args) == 2 && args[0] == "-batch" && strings.HasSuffix(args[1], "steer.rules") {
		s.ruleRuns++
		if s.failRules > 0 {
			s.failRules--
			return nil, errors.New("injected policy-rule failure")
		}
	}
	return s.fakeSys.Run(ctx, name, args...)
}

type recoveryTransport func(*http.Request) (*http.Response, error)

func (f recoveryTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func recoveryResponse(status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Status: http.StatusText(status), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}
}

func TestSteeringRetriesCachedMapAfterTransientApplyFailure(t *testing.T) {
	for _, unchangedStatus := range []int{http.StatusNoContent, http.StatusServiceUnavailable} {
		t.Run(http.StatusText(unchangedStatus), func(t *testing.T) {
			a := slaAgent(t)
			sys := &steeringFailureSys{fakeSys: a.Sys.(*fakeSys), failRules: 1}
			a.Sys, a.Steerer.Sys = sys, sys
			a.Applier = &apply.Applier{StateDir: t.TempDir()}
			body, err := json.Marshal(a.steerMap)
			if err != nil {
				t.Fatal(err)
			}
			a.steerMap = nil
			var reports []Telemetry
			a.Client = &client.Client{Base: "https://controller.invalid", HTTP: &http.Client{Transport: recoveryTransport(func(r *http.Request) (*http.Response, error) {
				if r.Method == http.MethodPost {
					var report Telemetry
					if err := json.NewDecoder(r.Body).Decode(&report); err != nil {
						t.Fatal(err)
					}
					reports = append(reports, report)
					return recoveryResponse(http.StatusNoContent, ""), nil
				}
				if r.URL.Query().Get("have") == "5" {
					return recoveryResponse(unchangedStatus, ""), nil
				}
				return recoveryResponse(http.StatusOK, string(body)), nil
			})}}
			ctx := context.Background()
			a.pollSteering(ctx)
			if a.steerErr == "" || sys.ruleRuns != 1 {
				t.Fatalf("failure not injected: error=%q attempts=%d", a.steerErr, sys.ruleRuns)
			}
			a.flush(ctx)
			if reports[0].SteeringVersion != 0 || len(reports[0].Steering) != 0 {
				t.Fatalf("failed map acknowledged as applied: %+v", reports[0])
			}
			a.pollSteering(ctx)
			if a.steerErr != "" || sys.ruleRuns != 2 || a.pathOf("voice") != "carrier-b" {
				t.Fatalf("cached map did not recover: error=%q attempts=%d path=%s", a.steerErr, sys.ruleRuns, a.pathOf("voice"))
			}
			a.flush(ctx)
			if reports[1].SteeringVersion != 5 || len(reports[1].Steering) != 3 {
				t.Fatalf("successful retry not acknowledged: %+v", reports[1])
			}
		})
	}
}

func TestSLADemotionRetriesFailedDataplaneApply(t *testing.T) {
	a := slaAgent(t)
	sys := &steeringFailureSys{fakeSys: a.Sys.(*fakeSys)}
	a.Sys, a.Steerer.Sys = sys, sys
	a.silent = true
	ctx := context.Background()
	a.steer(ctx, "start")
	sys.failRules = 1
	a.tick(ctx, win("wg-a", 30, 2, 0), win("wg-b", 40, 3, 2.5))
	if a.steerErr == "" || a.pathOf("voice") != "carrier-b" {
		t.Fatal("expected failed SLA move to retain last acknowledged choices")
	}
	a.tick(ctx, win("wg-a", 30, 2, 0), win("wg-b", 40, 3, 2.5))
	if a.steerErr != "" || a.pathOf("voice") != "carrier-a" || sys.ruleRuns != 3 {
		t.Fatalf("unchanged SLA did not retry failed move: error=%q attempts=%d path=%s", a.steerErr, sys.ruleRuns, a.pathOf("voice"))
	}
}

func TestBFDReplacedPeerDoesNotRetainStaleFailure(t *testing.T) {
	a := slaAgent(t)
	a.bfd["100.64.2.1"] = bfdPeer{Tunnel: "wg-b", Status: "down"}
	sys := &bfdSys{fakeSys: a.Sys.(*fakeSys), bfd: map[string]string{"100.64.2.2": "up"}, tuns: map[string]string{"100.64.2.2": "wg-b"}}
	a.Sys = sys
	if !a.checkBFD(context.Background()) {
		t.Fatal("replacement peer did not change the snapshot")
	}
	if len(a.bfd) != 1 || tunnelBFD(a.bfd, "wg-b") != "up" || !a.usable(a.steerMap)("carrier-b") {
		t.Fatalf("old failed peer blocked the replacement: %+v", a.bfd)
	}
	sys.bfd = map[string]string{}
	if !a.checkBFD(context.Background()) || len(a.bfd) != 0 {
		t.Fatalf("removed peer survived an empty successful snapshot: %+v", a.bfd)
	}
}

func TestBFDKnownFailureAlwaysWinsOverInitialisingPeer(t *testing.T) {
	a := slaAgent(t)
	a.bfd = map[string]bfdPeer{
		"failed":  {Tunnel: "wg-b", Status: "down"},
		"new":     {Tunnel: "wg-b", Status: "init"},
		"healthy": {Tunnel: "wg-b", Status: "up"},
	}
	for i := 0; i < 100; i++ {
		if got := tunnelBFD(a.bfd, "wg-b"); got != "down" || a.usable(a.steerMap)("carrier-b") {
			t.Fatalf("known-down tunnel became usable: %q", got)
		}
	}
}

func TestBFDReadFailurePreservesTheLastKnownFailure(t *testing.T) {
	a := slaAgent(t)
	a.bfd["failed"] = bfdPeer{Tunnel: "wg-b", Status: "down"}
	a.Sys = &steeringFailureSys{fakeSys: a.Sys.(*fakeSys), failBFD: true}
	if a.checkBFD(context.Background()) || len(a.bfd) != 1 || a.usable(a.steerMap)("carrier-b") {
		t.Fatalf("failed FRR read discarded known failure: %+v", a.bfd)
	}
}

func TestFailedStartupRestoreIsNotAcknowledgedAndRetries(t *testing.T) {
	a := slaAgent(t)
	a.current, a.steerMap = nil, nil
	sys := &steeringFailureSys{fakeSys: a.Sys.(*fakeSys), failWG: 1}
	a.Sys, a.Steerer.Sys = sys, sys
	a.Applier = &apply.Applier{Sys: sys, StateDir: t.TempDir(), PrivateKey: "private", FRRConf: "/frr.conf", Log: a.Log}
	lastGood, err := json.Marshal(state(1))
	if err != nil {
		t.Fatal(err)
	}
	sys.files[filepath.Join(a.Applier.StateDir, "last-good.json")] = string(lastGood)
	var reports []client.Status
	a.Client = &client.Client{Base: "https://controller.invalid", HTTP: &http.Client{Transport: recoveryTransport(func(r *http.Request) (*http.Response, error) {
		if r.Method == http.MethodPost {
			var report client.Status
			if err := json.NewDecoder(r.Body).Decode(&report); err != nil {
				t.Fatal(err)
			}
			reports = append(reports, report)
		} else if r.URL.Query().Get("have") != "1" {
			t.Errorf("successful restore was not reflected in poll: %s", r.URL)
		}
		return recoveryResponse(http.StatusNoContent, ""), nil
	})}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.restoreLastGood(ctx)
	if a.have() != 0 || a.current != nil || a.applyErr == "" || a.unreported == nil || a.unreported.OK || a.unreported.AppliedVersion != 0 {
		t.Fatalf("failed restoration was treated as applied: have=%d status=%+v", a.have(), a.unreported)
	}
	a.poll(ctx)
	if a.have() != 1 || a.applyErr != "" || len(reports) != 1 || !reports[0].OK || reports[0].AppliedVersion != 1 {
		t.Fatalf("poll did not recover cached last-good state: have=%d error=%q reports=%+v", a.have(), a.applyErr, reports)
	}
}
