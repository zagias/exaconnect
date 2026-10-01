package system

import (
	"context"
	"strings"
	"testing"
)

func TestRunReturnsStdoutOnly(t *testing.T) {
	out, err := Host{}.Run(context.Background(), "sh", "-c", `echo "% warning" >&2; echo '[]'`)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(out)); got != "[]" {
		t.Fatalf("stdout = %q, want []", got)
	}
	_, err = Host{}.Run(context.Background(), "sh", "-c", `echo boom >&2; exit 3`)
	if err == nil || !strings.Contains(err.Error(), "boom") {
		t.Fatalf("error should carry stderr, got %v", err)
	}
}
