package console

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestBoxNFT(t *testing.T) {
	fresh := BoxNFT(nil)
	if strings.Contains(fresh, `comment "inbound"`) {
		t.Fatal("a fresh box drops nothing by port")
	}
	n := &Network{WAN: []Port{{Iface: "eth1", Path: "carrier-b"}, {Iface: "eth0", Path: "carrier-a"}}, LAN: &Port{Iface: "eth2"}}
	got := BoxNFT(n)
	for _, want := range []string{
		"policy drop;",
		`iifname "wg-*" accept`,
		`iifname { "eth0", "eth1" } counter drop comment "inbound"`,
		`iifname "ppp*" counter drop comment "inbound"`,
		"add @block4 { ip saddr }",
		`tcp dport 443 accept comment "box console"`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("box firewall has no %q:\n%s", want, got)
		}
	}
	if strings.Index(got, `comment "inbound"`) > strings.Index(got, `comment "box console"`) {
		t.Error("the console must come after the internet ports are dropped")
	}
	// nft checks the syntax where it can (it needs CAP_NET_ADMIN even with -c).
	if _, err := exec.LookPath("nft"); err == nil && os.Geteuid() == 0 {
		for _, body := range []string{fresh, got} {
			f := filepath.Join(t.TempDir(), "box.nft")
			if err := os.WriteFile(f, []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}
			if out, err := exec.Command("nft", "-c", "-f", f).CombinedOutput(); err != nil {
				if strings.Contains(string(out), "Operation not permitted") || strings.Contains(string(out), "Could not process rule: No such file") {
					t.Skip("nft cannot check here: " + strings.TrimSpace(string(out)))
				}
				t.Fatalf("nft -c: %v\n%s", err, out)
			}
		}
	}
}

func TestEmptyNetplan(t *testing.T) {
	if got := Netplan(Network{}); !strings.HasSuffix(got, "  ethernets: {}\n") {
		t.Fatalf("empty netplan:\n%s", got)
	}
}
