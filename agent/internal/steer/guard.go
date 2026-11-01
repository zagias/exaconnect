package steer

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// guardMarks reads the kernel, including guards left by a crashed process.
// Preference999 is reserved solely for these exact-mark blackhole rules.
func (s *Steerer) guardMarks(ctx context.Context) (guards, oldMarks []int, err error) {
	out, err := s.Sys.Run(ctx, "ip", "-j", "rule", "show")
	if err != nil {
		return nil, nil, err
	}
	var rules []struct {
		Priority int             `json:"priority"`
		Mark     json.RawMessage `json:"fwmark"`
		Action   string          `json:"action"`
		Type     string          `json:"type"`
	}
	if len(strings.TrimSpace(string(out))) > 0 {
		if err := json.Unmarshal(out, &rules); err != nil {
			return nil, nil, fmt.Errorf("read steering guards: %w", err)
		}
	}
	for _, r := range rules {
		if r.Priority != GuardPref {
			if r.Priority >= RulePrefBase && r.Priority < RulePrefBase+RulePrefSpan && len(r.Mark) > 0 {
				mark, err := strconv.ParseUint(strings.Trim(string(r.Mark), `"`), 0, 16)
				if err != nil || mark == 0 {
					return nil, nil, fmt.Errorf("read class rule mark at preference %d", r.Priority)
				}
				oldMarks = append(oldMarks, int(mark))
			}
			continue
		}
		mark, err := strconv.ParseUint(strings.Trim(string(r.Mark), `"`), 0, 16)
		if err != nil || mark == 0 || (r.Action != "blackhole" && r.Type != "blackhole") {
			return nil, nil, fmt.Errorf("steering guard preference %d is occupied by an unrecognised rule", GuardPref)
		}
		guards = append(guards, int(mark))
	}
	return guards, oldMarks, nil
}

func (s *Steerer) protect(ctx context.Context, m *Map) error {
	marks, oldMarks, err := s.guardMarks(ctx)
	if err != nil {
		return err
	}
	seen := map[int]bool{}
	for _, mark := range marks {
		seen[mark] = true
	}
	s.guardActive = len(marks) > 0
	// Cover old marks too: a changed map may retire a class while its earlier
	// packets/rules still exist, including after a process restart.
	for _, c := range m.Classes {
		oldMarks = append(oldMarks, c.Mark)
	}
	for _, mark := range oldMarks {
		if seen[mark] {
			continue
		}
		if _, err := s.Sys.Run(ctx, "ip", "rule", "add", "pref", fmt.Sprint(GuardPref),
			"fwmark", fmt.Sprintf("0x%x", mark), "blackhole"); err != nil {
			return fmt.Errorf("protect class mark 0x%x: %w", mark, err)
		}
		s.guardActive = true
		seen[mark] = true
	}
	return nil
}

// Release only after every new classifier, route and class rule succeeded.
// On a deletion failure, retain the remaining guards for the next retry.
func (s *Steerer) releaseGuard(ctx context.Context) error {
	marks, _, err := s.guardMarks(ctx)
	if err != nil {
		return err
	}
	for _, mark := range marks {
		if _, err := s.Sys.Run(ctx, "ip", "rule", "del", "pref", fmt.Sprint(GuardPref),
			"fwmark", fmt.Sprintf("0x%x", mark), "blackhole"); err != nil {
			return fmt.Errorf("release steering guard: %w", err)
		}
	}
	s.guardActive = false
	return nil
}
