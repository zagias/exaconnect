package counters

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRead(t *testing.T) {
	Root = t.TempDir()
	dir := filepath.Join(Root, "eth1", "statistics")
	os.MkdirAll(dir, 0o755)
	os.WriteFile(filepath.Join(dir, "rx_bytes"), []byte("1234\n"), 0o644)
	os.WriteFile(filepath.Join(dir, "tx_bytes"), []byte("99\n"), 0o644)
	got := Read([]string{"eth1", "missing"}, time.Unix(0, 0))
	if len(got) != 1 || got[0].RxBytes != 1234 || got[0].TxBytes != 99 {
		t.Fatalf("got %+v", got)
	}
}
