package version

import (
	"strings"
	"testing"
)

func TestString(t *testing.T) {
	Version, Commit = "1.2.3", "abc123"
	s := String()
	for _, want := range []string{"exa-agent", "1.2.3", "abc123"} {
		if !strings.Contains(s, want) {
			t.Errorf("String() = %q, missing %q", s, want)
		}
	}
}
