package steer

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"testing"
)

// Retain policy rules across Steerer instances to model failure and restart.
// Real nft/ip parsing and policy selection are checked by the Linux test.
type guardSys struct {
	*fakeSys
	guards      map[int]int
	owned       map[int]int
	failBatch   bool
	failRelease bool
	failAddMark int
	batches     int
}

func newGuardSys() *guardSys {
	return &guardSys{fakeSys: &fakeSys{files: map[string]string{}}, guards: map[int]int{}, owned: map[int]int{}}
}

func (s *guardSys) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	if _, err := s.fakeSys.Run(ctx, name, args...); err != nil {
		return nil, err
	}
	if name == "ip" && strings.Join(args, " ") == "-j rule show" {
		var rows []map[string]any
		for mark, n := range s.guards {
			for i := 0; i < n; i++ {
				rows = append(rows, map[string]any{"priority": GuardPref, "fwmark": fmt.Sprintf("0x%x", mark), "action": "blackhole"})
			}
		}
		for pref, mark := range s.owned {
			rows = append(rows, map[string]any{"priority": pref, "fwmark": mark})
		}
		return json.Marshal(rows)
	}
	if name == "ip" && len(args) == 7 && args[0] == "rule" && args[2] == "pref" && args[3] == fmt.Sprint(GuardPref) {
		mark, _ := strconv.ParseInt(args[5], 0, 32)
		if args[1] == "add" {
			if int(mark) == s.failAddMark {
				s.failAddMark = 0
				return nil, errors.New("injected guard install failure")
			}
			s.guards[int(mark)]++
		} else {
			if s.failRelease {
				s.failRelease = false
				return nil, errors.New("injected guard release failure")
			}
			s.guards[int(mark)]--
			if s.guards[int(mark)] == 0 {
				delete(s.guards, int(mark))
			}
		}
	}
	if name == "ip" && len(args) == 2 && args[0] == "-batch" && strings.HasSuffix(args[1], "steer.rules") {
		s.batches++
		for _, line := range strings.Split(s.files[args[1]], "\n") {
			fields := strings.Fields(line)
			if len(fields) < 4 {
				continue
			}
			pref, _ := strconv.Atoi(fields[3])
			if fields[1] == "del" {
				delete(s.owned, pref)
				if s.failBatch {
					s.failBatch = false
					return nil, errors.New("injected partial class rule batch")
				}
			} else {
				mark, _ := strconv.ParseInt(fields[5], 0, 32)
				s.owned[pref] = int(mark)
			}
		}
	}
	return nil, nil
}

func TestPartialRuleBatchStaysGuardedAcrossRestartUntilSuccessfulRepair(t *testing.T) {
	ctx := context.Background()
	sys := newGuardSys()
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	choices := Choose(m, func(string) bool { return true })
	if err := s.Apply(ctx, m, choices); err != nil || len(sys.guards) != 0 {
		t.Fatalf("initial apply: err=%v guards=%v", err, sys.guards)
	}
	sys.failBatch = true
	choices[0].Path = "carrier-a"
	if err := s.Apply(ctx, m, choices); err == nil {
		t.Fatal("partial failure was acknowledged")
	}
	if len(sys.guards) != len(m.Classes) || sys.owned[RulePrefBase] != 0 {
		t.Fatalf("a removed class rule was not fenced: guards=%v owned=%v", sys.guards, sys.owned)
	}
	// The OS guards remain even though a restarted process has no cache.
	s = &Steerer{Sys: sys, StateDir: "/st"}
	if err := s.Apply(ctx, m, choices); err != nil {
		t.Fatal(err)
	}
	if len(sys.guards) != 0 || len(sys.owned) != len(choices) {
		t.Fatalf("repair did not release guards after installing all rules: guards=%v owned=%v", sys.guards, sys.owned)
	}
}

func TestFailedGuardReleaseRetriesWithoutReapplyingRules(t *testing.T) {
	sys := newGuardSys()
	sys.failRelease = true
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	choices := Choose(m, func(string) bool { return true })
	if err := s.Apply(context.Background(), m, choices); err == nil || len(sys.guards) == 0 {
		t.Fatal("failed guard cleanup did not remain fail-closed")
	}
	batches := sys.batches
	if err := s.Apply(context.Background(), m, choices); err != nil || len(sys.guards) != 0 || sys.batches != batches {
		t.Fatalf("guard cleanup retry: err=%v guards=%v batches=%d/%d", err, sys.guards, sys.batches, batches)
	}
}

func TestPartialGuardInstallDoesNotChangeClassifierOrRoutes(t *testing.T) {
	sys := newGuardSys()
	sys.failAddMark = 0x102
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	choices := Choose(m, func(string) bool { return true })
	if err := s.Apply(context.Background(), m, choices); err == nil {
		t.Fatal("guard install failure was ignored")
	}
	if len(sys.guards) != 1 || len(sys.files) != 0 || sys.batches != 0 {
		t.Fatalf("dataplane changed before the full guard was ready: guards=%v files=%v", sys.guards, sys.files)
	}
	if err := s.Apply(context.Background(), m, choices); err != nil || len(sys.guards) != 0 {
		t.Fatalf("partial guard installation did not reconcile: %v %v", err, sys.guards)
	}
}

func TestRestartProtectsRetiredClassMarksBeforePolicyMutation(t *testing.T) {
	sys := newGuardSys()
	sys.owned[RulePrefBase] = 0x777 // a class from a map the new process never saw
	sys.failBatch = true
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	if err := s.Apply(context.Background(), m, Choose(m, func(string) bool { return true })); err == nil {
		t.Fatal("expected the injected partial policy change to fail")
	}
	if sys.guards[0x777] != 1 || len(sys.guards) != len(m.Classes)+1 {
		t.Fatalf("retired/current class marks not all fenced across restart: %v", sys.guards)
	}
}

func TestGuardPreferenceConflictDoesNotChangeDataplane(t *testing.T) {
	sys := &fakeSys{files: map[string]string{}, rules: `[{"priority":999,"fwmark":"0x101","table":101}]`}
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	if err := s.Apply(context.Background(), m, Choose(m, func(string) bool { return true })); err == nil || len(sys.files) != 0 {
		t.Fatalf("unrecognised guard preference was overwritten: err=%v files=%v", err, sys.files)
	}
}

func TestReconciliationRemovesEveryDuplicateOwnedRule(t *testing.T) {
	sys := &fakeSys{files: map[string]string{}, rules: `[{"priority":1000,"fwmark":"0x101"},{"priority":1000,"fwmark":"0x101"}]`}
	s := &Steerer{Sys: sys, StateDir: "/st"}
	m := siteMap()
	if err := s.Apply(context.Background(), m, Choose(m, func(string) bool { return true })); err != nil {
		t.Fatal(err)
	}
	if got := strings.Count(sys.files["/st/steer.rules"], "rule del pref 1000\n"); got != 2 {
		t.Fatalf("duplicate old route could precede the replacement: deleted %d rules", got)
	}
}
