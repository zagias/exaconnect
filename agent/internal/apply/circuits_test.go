package apply

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

const psk = "s3cret-Pre-Shared-Key"

func popState(v int64, circuits ...desired.Circuit) *desired.State {
	return &desired.State{Schema: 1, Version: v, NodeName: "pop-miami", Role: desired.RolePoP, ASN: 65000,
		RouterID: "100.64.1.1", Loopback: "10.254.0.1/32", Circuits: circuits}
}

func vc7() desired.Circuit {
	return desired.Circuit{ID: 7, Name: "vc7", IfID: 7, UnderlayInterface: "eth5", LocalAddress: "100.64.0.2",
		RemoteAddress: "100.64.10.2", PSK: psk, IKEProposals: "aes256-sha256-modp2048", ESPProposals: "aes256-sha256-modp2048",
		InsideAddress: "169.254.100.2/30", PeerInside: "169.254.100.1", PeerASN: 64512,
		ImportPrefixes: []string{"10.100.0.0/16"}, MaxPrefixes: 100, ExportPrefixes: []string{"192.168.10.0/24"}, ShapeKbit: 50000}
}

func siteState(v int64, l2 ...desired.L2Circuit) *desired.State {
	return &desired.State{Schema: 1, Version: v, NodeName: "site-a", Role: desired.RoleSite, ASN: 65001,
		RouterID: "100.64.1.11", Loopback: "10.254.0.11/32", L2Circuits: l2}
}

func vx9(vlan int) desired.L2Circuit {
	return desired.L2Circuit{ID: 9, Name: "vx9", VNI: 10009, VLAN: vlan, Parent: "eth4", Remote: "10.254.0.12",
		ShapeKbit: 20000, MTU: 1370, Probe: &desired.Probe{Target: "10.254.0.12:7000", IntervalMS: 1000}}
}

// What `ip -d -j link show` prints once vc7 exists (iproute2 prints if_id in hex).
const vc7Links = `[{"ifindex":1,"ifname":"lo","mtu":65536},
{"ifindex":7,"ifname":"eth5","mtu":1500,"link_netnsid":0,"linkinfo":{"info_kind":"veth"}},
{"ifindex":3,"ifname":"br0","mtu":1500,"linkinfo":{"info_kind":"bridge","info_data":{"stp_state":0}}},
{"ifindex":20,"ifname":"vc7","mtu":1400,"ifalias":"exaconnect","linkinfo":{"info_kind":"xfrm","info_data":{"link":"eth5","if_id":"0x7"}}}]`

// ... and once vx9 exists on VLAN 100.
const vx9Links = `[{"ifindex":1,"ifname":"lo","mtu":65536},
{"ifindex":6,"ifname":"eth4","mtu":1500,"link_netnsid":0,"linkinfo":{"info_kind":"veth"}},
{"ifindex":21,"ifname":"vx9","mtu":1370,"master":"br9","ifalias":"exaconnect","linkinfo":{"info_kind":"vxlan","info_data":{"id":10009,"remote":"10.254.0.12","local":"10.254.0.11","ttl":0,"port":4789,"learning":false},"info_slave_kind":"bridge"}},
{"ifindex":22,"ifname":"br9","mtu":1370,"ifalias":"exaconnect","linkinfo":{"info_kind":"bridge","info_data":{"stp_state":0}}},
{"ifindex":23,"ifname":"eth4.100","link":"eth4","mtu":1370,"master":"br9","ifalias":"exaconnect","linkinfo":{"info_kind":"vlan","info_data":{"protocol":"802.1Q","id":100},"info_slave_kind":"bridge"}}]`

func circuitApplier(f *fake) (*Applier, *bytes.Buffer) {
	var buf bytes.Buffer
	a := applier(f)
	a.Log = slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
	a.SwanctlDir, a.StrongSwanConf = "/swanctl", "/strongswan.d/exaconnect.conf"
	a.Sleep = func(time.Duration) {}
	return a, &buf
}

func has(t *testing.T, cmds []string, want ...string) {
	t.Helper()
	joined := "\n" + strings.Join(cmds, "\n") + "\n"
	for _, w := range want {
		if !strings.Contains(joined, "\n"+w+"\n") {
			t.Errorf("missing command %q in:%s", w, joined)
		}
	}
}

func hasNone(t *testing.T, cmds []string, prefixes ...string) {
	t.Helper()
	for _, c := range cmds {
		for _, p := range prefixes {
			if strings.HasPrefix(c, p) {
				t.Errorf("unexpected command %q", c)
			}
		}
	}
}

func noPSK(t *testing.T, f *fake, logs *bytes.Buffer) {
	t.Helper()
	if strings.Contains(strings.Join(f.cmds, "\n"), psk) {
		t.Error("psk in a command line")
	}
	if strings.Contains(logs.String(), psk) {
		t.Errorf("psk in the log: %s", logs.String())
	}
	for p, d := range f.files {
		if strings.Contains(d, psk) && p != "/state/last-good.json" {
			t.Errorf("psk in clear in %s", p)
		}
	}
}

func TestApplyCloudCircuit(t *testing.T) {
	f := &fake{files: map[string]string{}, perms: map[string]os.FileMode{}}
	a, logs := circuitApplier(f)
	if err := a.Apply(context.Background(), popState(1, vc7())); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds,
		"ip address replace 10.254.0.1/32 dev lo label lo:exa",
		"ip link add vc7 type xfrm dev eth5 if_id 7",
		"ip address replace 169.254.100.2/30 dev vc7",
		"ip link set dev vc7 mtu 1400 alias exaconnect up",
		"tc qdisc replace dev vc7 root cake bandwidth 50000kbit diffserv4",
		"ipsec start",
		"swanctl --load-all --clear --noprompt",
	)
	conf := f.files["/swanctl/exa-vc7.conf"]
	if !strings.Contains(conf, "if_id_in = 7") || f.perms["/swanctl/exa-vc7.conf"] != 0o600 {
		t.Fatalf("swanctl conf %o:\n%s", f.perms["/swanctl/exa-vc7.conf"], conf)
	}
	if !strings.Contains(f.files["/strongswan.d/exaconnect.conf"], "install_routes = no") {
		t.Fatal("strongSwan settings not written")
	}
	if f.perms["/state/last-good.json"] != 0o600 {
		t.Fatal("last good must be 0600: it holds the key")
	}
	frr := f.files["/frr.conf"]
	for _, want := range []string{"network 10.254.0.1/32", "neighbor 169.254.100.1 remote-as 64512", "neighbor 169.254.100.1 prefix-list VC7-IN in"} {
		if !strings.Contains(frr, want) {
			t.Errorf("frr missing %q", want)
		}
	}
	noPSK(t, f, logs)

	// Again, with vc7 in place and charon running: nothing is re-created or restarted.
	f.cmds, f.ipLinks = nil, vc7Links
	if err := a.Apply(context.Background(), popState(2, vc7())); err != nil {
		t.Fatal(err)
	}
	hasNone(t, f.cmds, "ip link add", "ip link del", "ipsec start", "tc ", "swanctl --terminate", "swanctl --initiate")
	has(t, f.cmds, "swanctl --load-all --clear --noprompt")

	// A new key: reloaded and the SA re-established with it.
	f.cmds = nil
	c := vc7()
	c.PSK = "another-Pre-Shared-Key"
	if err := a.Apply(context.Background(), popState(3, c)); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds, "swanctl --terminate --ike exa-vc7 --force", "swanctl --initiate --child exa-vc7 --timeout 2")
	hasNone(t, f.cmds, "ip link add")
	noPSK(t, f, logs)
}

func TestApplyRemovesCloudCircuit(t *testing.T) {
	f := &fake{files: map[string]string{"/swanctl/exa-vc7.conf": "old", "/swanctl/other.conf": "theirs"}, ipLinks: vc7Links, charon: true}
	a, _ := circuitApplier(f)
	if err := a.Apply(context.Background(), popState(1)); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds, "ip link del dev vc7", "swanctl --load-all --clear --noprompt", "swanctl --terminate --ike exa-vc7 --force")
	hasNone(t, f.cmds, "ip link del dev br0", "ip link del dev eth5", "ipsec start")
	if _, ok := f.files["/swanctl/exa-vc7.conf"]; ok {
		t.Fatal("stale swanctl file kept")
	}
	if _, ok := f.files["/swanctl/other.conf"]; !ok {
		t.Fatal("a file the agent did not write was removed")
	}
	// Nothing left: strongSwan is not touched at all.
	f.cmds, f.ipLinks = nil, ""
	if err := a.Apply(context.Background(), popState(2)); err != nil {
		t.Fatal(err)
	}
	hasNone(t, f.cmds, "swanctl", "ipsec")
}

func TestApplyDisabledCircuitIsTornDown(t *testing.T) {
	f := &fake{files: map[string]string{"/swanctl/exa-vc7.conf": "old"}, ipLinks: vc7Links, charon: true}
	a, _ := circuitApplier(f)
	off := false
	c := vc7()
	c.Enabled = &off
	if err := a.Apply(context.Background(), popState(1, c)); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds, "ip link del dev vc7", "swanctl --terminate --ike exa-vc7 --force")
	if strings.Contains(f.files["/frr.conf"], "169.254.100.1") {
		t.Fatal("disabled circuit still in FRR")
	}
}

func TestApplyL2Circuit(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a, logs := circuitApplier(f)
	if err := a.Apply(context.Background(), siteState(1, vx9(100))); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds,
		"ip address replace 10.254.0.11/32 dev lo label lo:exa",
		"ip link add vx9 type vxlan id 10009 local 10.254.0.11 remote 10.254.0.12 dstport 4789 nolearning",
		"ip link add br9 type bridge",
		"ip link add link eth4 name eth4.100 type vlan id 100",
		"ip link set dev vx9 master br9",
		"ip link set dev eth4.100 master br9",
		"ip link set dev eth4 up",
		"ip link set dev vx9 mtu 1370 alias exaconnect up",
		"ip link set dev eth4.100 mtu 1370 alias exaconnect up",
		"ip link set dev br9 mtu 1370 alias exaconnect up",
		"tc qdisc replace dev vx9 root cake bandwidth 20000kbit diffserv4",
	)
	hasNone(t, f.cmds, "swanctl", "ipsec", "ip link del")
	if !strings.Contains(f.files["/frr.conf"], "network 10.254.0.11/32") {
		t.Fatal("loopback not advertised")
	}

	// Idempotent.
	f.cmds, f.ipLinks = nil, vx9Links
	if err := a.Apply(context.Background(), siteState(2, vx9(100))); err != nil {
		t.Fatal(err)
	}
	hasNone(t, f.cmds, "ip link add", "ip link del", "tc ")

	// The VLAN changes: the old subinterface goes, the new one joins the bridge.
	f.cmds = nil
	if err := a.Apply(context.Background(), siteState(3, vx9(200))); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds, "ip link del dev eth4.100", "ip link add link eth4 name eth4.200 type vlan id 200", "ip link set dev eth4.200 master br9")
	hasNone(t, f.cmds, "ip link add vx9", "ip link add br9", "ip link del dev vx9", "ip link del dev br9")

	// The remote changes: the VXLAN interface is re-created.
	f.cmds = nil
	c := vx9(100)
	c.Remote = "10.254.0.13"
	if err := a.Apply(context.Background(), siteState(4, c)); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds, "ip link del dev vx9", "ip link add vx9 type vxlan id 10009 local 10.254.0.11 remote 10.254.0.13 dstport 4789 nolearning")
	noPSK(t, f, logs)
}

func TestApplyRemovesL2Circuit(t *testing.T) {
	f := &fake{files: map[string]string{}, ipLinks: vx9Links, loAddrs: `[{"ifname":"lo","addr_info":[{"family":"inet","local":"10.254.0.9","prefixlen":32,"label":"lo:exa"}]}]`}
	a, _ := circuitApplier(f)
	if err := a.Apply(context.Background(), siteState(1)); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds, "ip address del 10.254.0.9/32 dev lo", "ip link del dev eth4.100", "ip link del dev vx9", "ip link del dev br9")
	hasNone(t, f.cmds, "ip link del dev lo")
	for _, c := range f.cmds {
		if c == "ip link del dev eth4" {
			t.Fatal("the parent interface was removed")
		}
	}
	// Members go before the bridge.
	idx := func(c string) int {
		for i, x := range f.cmds {
			if x == c {
				return i
			}
		}
		return -1
	}
	if idx("ip link del dev br9") < idx("ip link del dev vx9") {
		t.Fatalf("bridge removed before its members: %v", f.cmds)
	}
}

func TestShapingIsBestEffort(t *testing.T) {
	f := &fake{files: map[string]string{}, failOn: "root cake"}
	a, logs := circuitApplier(f)
	if err := a.Apply(context.Background(), siteState(1, vx9(100))); err != nil {
		t.Fatal(err)
	}
	has(t, f.cmds, "tc qdisc replace dev vx9 root tbf rate 20000kbit burst 25000 latency 50ms")

	f.failOn, f.cmds = "qdisc replace", nil // no qdisc works at all
	c := vx9(100)
	c.ShapeKbit = 30000
	for v := int64(2); v <= 3; v++ {
		if err := a.Apply(context.Background(), siteState(v, c)); err != nil {
			t.Fatalf("shaping must not fail the apply: %v", err)
		}
	}
	if n := strings.Count(logs.String(), "circuit shaping not applied"); n != 1 {
		t.Fatalf("logged %d times, want once:\n%s", n, logs.String())
	}
}

func TestCircuitFailureRollsBack(t *testing.T) {
	f := &fake{files: map[string]string{}, charon: true}
	a, logs := circuitApplier(f)
	if err := a.Apply(context.Background(), popState(1)); err != nil {
		t.Fatal(err)
	}
	f.failOn = "ip link add vc7"
	err := a.Apply(context.Background(), popState(2, vc7()))
	if !IsRolledBack(err) || strings.Contains(err.Error(), psk) {
		t.Fatalf("want a rollback without the key, got %v", err)
	}
	if lg := a.LastGood(); lg.Version != 1 {
		t.Fatalf("last good is %d", lg.Version)
	}
	if strings.Contains(f.files["/frr.conf"], "169.254.100.1") {
		t.Fatal("FRR still has the failed circuit")
	}
	noPSK(t, f, logs)
}

func TestCharonThatNeverStartsFailsTheApply(t *testing.T) {
	f := &fake{files: map[string]string{}, failOn: "ipsec start"}
	a, _ := circuitApplier(f)
	err := a.Apply(context.Background(), popState(1, vc7()))
	if err == nil || !strings.Contains(err.Error(), "strongSwan") {
		t.Fatalf("got %v", err)
	}
	has(t, f.cmds, "ipsec start")
}
