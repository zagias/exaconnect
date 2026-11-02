package render

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// These are kernel-checked in a fresh user/network namespace, never in the
// host's network namespace. Sandboxes without user namespaces skip explicitly.
func TestInternetRulesetSyntaxInIsolatedNamespace(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("nftables kernel validation needs Linux")
	}
	for _, tool := range []string{"unshare", "nft"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skip(tool + " is unavailable")
		}
	}
	if out, err := exec.Command("unshare", "--user", "--map-root-user", "--net", "true").CombinedOutput(); err != nil {
		t.Skipf("isolated network namespace unavailable: %v: %s", err, out)
	}
	off := &desired.State{Internet: &desired.Internet{Mode: desired.InternetOff,
		LANPrefixes: []string{"192.168.10.0/24"}, CorporatePrefixes: []string{"192.168.0.0/16", "203.0.113.0/24"}}}
	for name, script := range map[string]string{
		"gateway": InternetNFT(inetPoP()), "local": InternetNFT(inetLocal()),
		"ipv6": InternetForwardGuard(inetLocal()), "off": InternetForwardGuard(off),
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "firewall.nft")
			if err := os.WriteFile(path, []byte(script), 0o600); err != nil {
				t.Fatal(err)
			}
			out, err := exec.Command("unshare", "--user", "--map-root-user", "--net", "nft", "--check", "--file", path).CombinedOutput()
			if err != nil {
				t.Fatalf("nft rejected generated %s rules: %v\n%s", name, err, out)
			}
		})
	}
}
