package agent

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/apply"
	"github.com/zagias/exaconnect/agent/internal/client"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/steer"
	"github.com/zagias/exaconnect/agent/internal/system"
)

// This opt-in test changes only a disposable network namespace. It uses the
// real nft/ip dataplane and agent recovery code, but deterministic FRR JSON;
// it does not measure BFD wire detection, WireGuard loss or full convergence.
func TestRoutingRecoveryInIsolatedKernel(t *testing.T) {
	if os.Getenv("EXA_ROUTING_KERNEL_TESTS") != "1" {
		t.Skip("set EXA_ROUTING_KERNEL_TESTS=1 in an isolated privileged Linux CI runner")
	}
	if runtime.GOOS != "linux" {
		t.Fatal("routing kernel tests require Linux; never run dataplane commands on macOS")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	if os.Getenv("EXA_ROUTING_KERNEL_CHILD") != "1" {
		for _, tool := range []string{"unshare", "ip", "nft"} {
			if _, err := exec.LookPath(tool); err != nil {
				t.Fatal("required kernel test tool unavailable:", tool)
			}
		}
		parent, err := os.Readlink("/proc/self/ns/net")
		if err != nil {
			t.Fatal(err)
		}
		exe, err := os.Executable()
		if err != nil {
			t.Fatal(err)
		}
		cmd := exec.CommandContext(ctx, "unshare", "--net", "--", exe,
			"-test.run=^TestRoutingRecoveryInIsolatedKernel$", "-test.v", "-test.timeout=40s")
		cmd.Env = append(os.Environ(), "EXA_ROUTING_KERNEL_CHILD=1", "EXA_ROUTING_KERNEL_PARENT="+parent)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("isolated kernel routing validation failed: %v\n%s", err, out)
		}
		t.Logf("%s", out)
		return
	}
	// Fail before any command if re-execution did not leave the parent netns.
	ns, err := os.Readlink("/proc/self/ns/net")
	parent := os.Getenv("EXA_ROUTING_KERNEL_PARENT")
	if err != nil || parent == "" || ns == parent {
		t.Fatalf("refusing to alter the parent network namespace: current=%q parent=%q err=%v", ns, parent, err)
	}

	sys := &kernelRecoverySys{bfd: map[string]string{"a": "up", "b": "up"}}
	run := func(name string, args ...string) string {
		t.Helper()
		out, err := sys.Host.Run(ctx, name, args...)
		if err != nil {
			t.Fatal(err)
		}
		return string(out)
	}
	run("ip", "link", "set", "lo", "up")
	for i, name := range []string{"wg-a", "wg-b", "wg-sat"} {
		run("ip", "link", "add", name, "type", "dummy")
		run("ip", "address", "add", "10.80."+strconv.Itoa(i+1)+".1/32", "dev", name)
		run("ip", "link", "set", name, "up")
	}
	// BGP's default intentionally uses a satellite excluded from the class.
	// The class must fail closed when both permitted terrestrial paths fail.
	run("ip", "route", "add", "default", "dev", "wg-sat")

	a := slaAgent(t)
	a.Sys = sys
	a.Steerer = &steer.Steerer{Sys: sys, StateDir: t.TempDir()}
	a.Applier = &apply.Applier{Sys: sys, StateDir: t.TempDir(), Log: a.Log}
	a.current.Tunnels = append(a.current.Tunnels, desired.Tunnel{Name: "wg-sat", Path: "sat"})
	m := a.steerMap
	m.Paths = append(m.Paths, steer.Path{Name: "sat", Tunnel: "wg-sat", Table: 103})
	m.Classes = m.Classes[:1]
	m.Rules = []steer.Rule{{Class: "voice", Paths: []string{"carrier-b", "carrier-a"}, PauseIfNone: true}}
	a.steerMap = nil
	body, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	a.Client = &client.Client{Base: "https://controller.invalid", HTTP: &http.Client{Transport: recoveryTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Query().Get("have") == "5" {
			return recoveryResponse(http.StatusNoContent, ""), nil
		}
		return recoveryResponse(http.StatusOK, string(body)), nil
	})}}
	assertRoute := func(want string) {
		t.Helper()
		out := run("ip", "-4", "route", "get", "198.51.100.10", "mark", "0x101")
		if !strings.Contains(out, "dev "+want) {
			t.Fatalf("kernel selected the wrong path, want %s: %s", want, out)
		}
	}
	updateBFD := func() {
		t.Helper()
		if !a.checkBFD(ctx) {
			t.Fatal("expected a changed authoritative BFD snapshot")
		}
		a.bfdChanged(ctx)
	}

	a.checkBFD(ctx)
	a.pollSteering(ctx)
	if a.steerErr != "" {
		t.Fatal(a.steerErr)
	}
	assertRoute("wg-b")

	// Fail after deleting an existing rule, as a non-atomic ip batch can do.
	// A 204 response on the next poll must repair the actual kernel state.
	sys.bfd["b"], sys.failRules = "down", true
	updateBFD()
	if a.steerErr == "" || a.steerAppliedVersion != 5 {
		t.Fatal("expected an unacknowledged failed steering attempt")
	}
	if out, err := sys.Host.Run(ctx, "ip", "-4", "route", "get", "198.51.100.10", "mark", "0x101"); err == nil {
		t.Fatalf("partial rule failure escaped onto an excluded BGP path: %s", out)
	}
	// Lose the steerer cache as a process restart would; guards must be read
	// back from the kernel, retained during repair, then removed on success.
	a.Steerer = &steer.Steerer{Sys: sys, StateDir: a.Steerer.StateDir}
	a.pollSteering(ctx)
	if a.steerErr != "" || a.pathOf("voice") != "carrier-a" {
		t.Fatalf("cached-map retry did not recover: %q", a.steerErr)
	}
	assertRoute("wg-a")

	// With both allowed paths down, no class rule may fall through to BGP/SAT.
	sys.bfd["a"] = "down"
	updateBFD()
	if out, err := sys.Host.Run(ctx, "ip", "-4", "route", "get", "198.51.100.10", "mark", "0x101"); err == nil {
		t.Fatalf("excluded satellite became reachable with both allowed paths down: %s", out)
	}
	if a.pathOf("voice") != "paused" {
		t.Fatal("agent did not acknowledge the class pause")
	}
	if rules := run("ip", "-4", "rule", "show"); !strings.Contains(rules, "blackhole") {
		t.Fatalf("kernel has no explicit fail-closed class rule: %s", rules)
	}

	// Recover on A, then replace the failed B session with a new healthy peer.
	sys.bfd["a"] = "up"
	updateBFD()
	assertRoute("wg-a")
	delete(sys.bfd, "b")
	sys.bfd["replacement-b"] = "up"
	updateBFD()
	assertRoute("wg-b")
	if _, exists := a.bfd["b"]; exists {
		t.Fatal("retired down BFD peer survived its replacement")
	}
	t.Log("real kernel routes recovered after partial apply, 204 retry, BFD down/up and peer replacement; excluded satellite stayed blocked")
}

type kernelRecoverySys struct {
	system.Host
	bfd       map[string]string
	failRules bool
}

func (s *kernelRecoverySys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	if name == "vtysh" {
		var peers []map[string]string
		for peer, status := range s.bfd {
			tunnel := "wg-b"
			if peer == "a" {
				tunnel = "wg-a"
			}
			peers = append(peers, map[string]string{"peer": peer, "interface": tunnel, "status": status})
		}
		return json.Marshal(peers)
	}
	if s.failRules && name == "ip" && len(args) == 2 && args[0] == "-batch" && strings.HasSuffix(args[1], "steer.rules") {
		s.failRules = false
		if _, err := s.Host.Run(ctx, "ip", "rule", "del", "pref", "1000"); err != nil {
			return nil, err
		}
		return nil, errors.New("injected partial ip-rule batch failure")
	}
	return s.Host.Run(ctx, name, args...)
}
