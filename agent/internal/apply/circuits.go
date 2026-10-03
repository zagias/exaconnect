package apply

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/render"
	"github.com/zagias/exaconnect/agent/internal/system"
)

const (
	SwanctlDir     = "/etc/swanctl/conf.d"
	StrongSwanConf = "/etc/strongswan.d/exaconnect.conf"
	// Alias marks the interfaces the agent made for circuits, so it only
	// ever removes its own.
	Alias = "exaconnect"
	// LoopbackLabel marks the loopback address the agent put on lo.
	LoopbackLabel = "lo:exa"
	// XFRMMTU leaves room for ESP over a 1500-byte underlay.
	XFRMMTU = 1400
	// VXLANPort is the IANA VXLAN port.
	VXLANPort = 4789
)

var (
	staleVC = regexp.MustCompile(`^vc[0-9]+$`)
	staleVX = regexp.MustCompile(`^vx[0-9]+$`)
	staleBR = regexp.MustCompile(`^br[0-9]+$`)
)

// ipLink is one row of `ip -d -j link show`.
type ipLink struct {
	Name   string `json:"ifname"`
	Alias  string `json:"ifalias"`
	Master string `json:"master"`
	Link   string `json:"link"`
	Info   struct {
		Kind string                     `json:"info_kind"`
		Data map[string]json.RawMessage `json:"info_data"`
	} `json:"linkinfo"`
}

// num reads an info_data number that iproute2 may print as a number or as
// a hex string ("0x7", as it does for if_id).
func (l ipLink) num(key string) (uint64, bool) {
	raw, ok := l.Info.Data[key]
	if !ok {
		return 0, false
	}
	var n uint64
	if json.Unmarshal(raw, &n) == nil {
		return n, true
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		if n, err := strconv.ParseUint(s, 0, 64); err == nil {
			return n, true
		}
	}
	return 0, false
}

func (l ipLink) str(key string) string {
	var s string
	_ = json.Unmarshal(l.Info.Data[key], &s)
	return s
}

func (a *Applier) links(ctx context.Context) (map[string]ipLink, error) {
	out, err := a.Sys.Run(ctx, "ip", "-d", "-j", "link", "show")
	if err != nil {
		return nil, err
	}
	var rows []ipLink
	if len(out) > 0 {
		if err := json.Unmarshal(out, &rows); err != nil {
			return nil, fmt.Errorf("parse ip link: %w", err)
		}
	}
	m := make(map[string]ipLink, len(rows))
	for _, r := range rows {
		m[r.Name] = r
	}
	return m, nil
}

func (a *Applier) run(ctx context.Context, args ...string) error {
	_, err := a.Sys.Run(ctx, args[0], args[1:]...)
	return err
}

// loopback puts the node's loopback on lo, labelled so a changed address
// replaces the old one rather than adding to it.
func (a *Applier) loopback(ctx context.Context, s *desired.State) error {
	out, err := a.Sys.Run(ctx, "ip", "-j", "-4", "address", "show", "dev", "lo", "label", LoopbackLabel)
	if err != nil {
		return err
	}
	var rows []struct {
		AddrInfo []struct {
			Local     string `json:"local"`
			PrefixLen int    `json:"prefixlen"`
			Label     string `json:"label"`
		} `json:"addr_info"`
	}
	if len(out) > 0 {
		if err := json.Unmarshal(out, &rows); err != nil {
			return fmt.Errorf("parse ip address: %w", err)
		}
	}
	for _, r := range rows {
		for _, ai := range r.AddrInfo {
			p := ai.Local + "/" + strconv.Itoa(ai.PrefixLen)
			if ai.Label == LoopbackLabel && p != s.Loopback {
				if err := a.run(ctx, "ip", "address", "del", p, "dev", "lo"); err != nil {
					return err
				}
			}
		}
	}
	if s.Loopback == "" {
		return nil
	}
	return a.run(ctx, "ip", "address", "replace", s.Loopback, "dev", "lo", "label", LoopbackLabel)
}

// circuits brings the cloud and layer 2 circuits to the desired state and
// removes the ones no longer listed.
func (a *Applier) circuits(ctx context.Context, s *desired.State) error {
	have, err := a.links(ctx)
	if err != nil {
		return err
	}
	cloud := s.ActiveCircuits()
	want := map[string]bool{}
	for _, c := range cloud {
		want[c.Name] = true
		if err := a.xfrm(ctx, c, have); err != nil {
			return fmt.Errorf("%s: %w", c.Name, err)
		}
	}
	for _, c := range s.L2Circuits {
		want[c.Name], want[c.Bridge()], want[c.VLANDev()] = true, true, true
		if err := a.l2(ctx, c, s.LoopbackAddr(), have); err != nil {
			return fmt.Errorf("%s: %w", c.Name, err)
		}
	}
	if err := a.removeStaleCircuits(ctx, have, want); err != nil {
		return err
	}
	return a.swanctl(ctx, cloud)
}

// recreate deletes dev if it exists but does not match; it reports whether
// dev has to be (re)created.
func (a *Applier) recreate(ctx context.Context, have map[string]ipLink, dev string, ok func(ipLink) bool) (bool, error) {
	l, exists := have[dev]
	if exists && ok(l) {
		return false, nil
	}
	if exists {
		if err := a.run(ctx, "ip", "link", "del", "dev", dev); err != nil {
			return false, err
		}
		delete(have, dev)
	}
	delete(a.shaped, dev) // a new interface has no qdisc yet
	return true, nil
}

func (a *Applier) xfrm(ctx context.Context, c desired.Circuit, have map[string]ipLink) error {
	create, err := a.recreate(ctx, have, c.Name, func(l ipLink) bool {
		id, _ := l.num("if_id")
		dev := l.str("link")
		return l.Info.Kind == "xfrm" && id == uint64(c.IfID) && (dev == "" || dev == c.UnderlayInterface)
	})
	if err != nil {
		return err
	}
	if create {
		if err := a.run(ctx, "ip", "link", "add", c.Name, "type", "xfrm", "dev", c.UnderlayInterface, "if_id", strconv.FormatUint(uint64(c.IfID), 10)); err != nil {
			return err
		}
	} else if err := a.dropOtherAddresses(ctx, c.Name, c.InsideAddress); err != nil {
		return err
	}
	for _, st := range [][]string{
		{"ip", "address", "replace", c.InsideAddress, "dev", c.Name},
		{"ip", "link", "set", "dev", c.Name, "mtu", strconv.Itoa(XFRMMTU), "alias", Alias, "up"},
	} {
		if err := a.run(ctx, st...); err != nil {
			return err
		}
	}
	a.shape(ctx, c.Name, c.ShapeKbit)
	return nil
}

// dropOtherAddresses removes IPv4 addresses on dev other than keep, so a
// changed inside address does not leave the old one behind.
func (a *Applier) dropOtherAddresses(ctx context.Context, dev, keep string) error {
	out, err := a.Sys.Run(ctx, "ip", "-j", "-4", "address", "show", "dev", dev)
	if err != nil {
		return err
	}
	var rows []struct {
		AddrInfo []struct {
			Local     string `json:"local"`
			PrefixLen int    `json:"prefixlen"`
		} `json:"addr_info"`
	}
	if len(out) > 0 && json.Unmarshal(out, &rows) != nil {
		return nil // nothing we can act on; replace below still sets ours
	}
	for _, r := range rows {
		for _, ai := range r.AddrInfo {
			if p := ai.Local + "/" + strconv.Itoa(ai.PrefixLen); p != keep {
				if err := a.run(ctx, "ip", "address", "del", p, "dev", dev); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

func (a *Applier) l2(ctx context.Context, c desired.L2Circuit, local string, have map[string]ipLink) error {
	vx, br, vl := c.Name, c.Bridge(), c.VLANDev()
	create, err := a.recreate(ctx, have, vx, func(l ipLink) bool {
		id, _ := l.num("id")
		port, _ := l.num("port")
		return l.Info.Kind == "vxlan" && id == uint64(c.VNI) && l.str("local") == local &&
			l.str("remote") == c.Remote && port == VXLANPort
	})
	if err != nil {
		return err
	}
	if create {
		if err := a.run(ctx, "ip", "link", "add", vx, "type", "vxlan", "id", strconv.Itoa(c.VNI),
			"local", local, "remote", c.Remote, "dstport", strconv.Itoa(VXLANPort), "nolearning"); err != nil {
			return err
		}
	}
	if create, err = a.recreate(ctx, have, br, func(l ipLink) bool { return l.Info.Kind == "bridge" }); err != nil {
		return err
	} else if create {
		if err := a.run(ctx, "ip", "link", "add", br, "type", "bridge"); err != nil {
			return err
		}
	}
	create, err = a.recreate(ctx, have, vl, func(l ipLink) bool {
		id, _ := l.num("id")
		return l.Info.Kind == "vlan" && id == uint64(c.VLAN) && (l.Link == "" || l.Link == c.Parent)
	})
	if err != nil {
		return err
	} else if create {
		if err := a.run(ctx, "ip", "link", "add", "link", c.Parent, "name", vl, "type", "vlan", "id", strconv.Itoa(c.VLAN)); err != nil {
			return err
		}
	}
	// Anything else on the bridge (an old VLAN after the VLAN changed) goes.
	for _, l := range have {
		if l.Master != br || l.Name == vx || l.Name == vl {
			continue
		}
		cmd := []string{"ip", "link", "set", "dev", l.Name, "nomaster"}
		if l.Alias == Alias {
			cmd = []string{"ip", "link", "del", "dev", l.Name}
		}
		if err := a.run(ctx, cmd...); err != nil {
			return err
		}
		if l.Alias == Alias {
			delete(have, l.Name)
		}
	}
	steps := [][]string{
		{"ip", "link", "set", "dev", vx, "master", br},
		{"ip", "link", "set", "dev", vl, "master", br},
		{"ip", "link", "set", "dev", c.Parent, "up"},
	}
	for _, dev := range []string{vx, vl, br} {
		st := []string{"ip", "link", "set", "dev", dev}
		if c.MTU > 0 {
			st = append(st, "mtu", strconv.Itoa(c.MTU))
		}
		steps = append(steps, append(st, "alias", Alias, "up"))
	}
	for _, st := range steps {
		if err := a.run(ctx, st...); err != nil {
			return err
		}
	}
	a.shape(ctx, vx, c.ShapeKbit)
	return nil
}

// removeStaleCircuits deletes circuit interfaces no longer wanted: XFRM and
// VXLAN interfaces by their names, bridges and VLAN subinterfaces only when
// the agent marked them as its own.
func (a *Applier) removeStaleCircuits(ctx context.Context, have map[string]ipLink, want map[string]bool) error {
	ours := func(l ipLink) bool {
		switch l.Info.Kind {
		case "xfrm":
			return staleVC.MatchString(l.Name)
		case "vxlan":
			return staleVX.MatchString(l.Name)
		case "bridge":
			return staleBR.MatchString(l.Name) && l.Alias == Alias
		case "vlan":
			return l.Alias == Alias
		}
		return false
	}
	// Members before bridges, in a stable order.
	for _, kinds := range [][]string{{"xfrm", "vxlan", "vlan"}, {"bridge"}} {
		for _, name := range sortedNames(have) {
			l := have[name]
			if want[name] || !ours(l) || !contains(kinds, l.Info.Kind) {
				continue
			}
			if err := a.run(ctx, "ip", "link", "del", "dev", name); err != nil {
				return err
			}
			delete(have, name)
			delete(a.shaped, name)
		}
	}
	return nil
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func sortedNames(m map[string]ipLink) []string {
	out := make([]string, 0, len(m))
	for n := range m {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// shape puts egress shaping on dev: CAKE when the kernel has it, else a token
// bucket. It is best effort, like the tunnels' QoS: a failure is logged once
// and does not fail the apply.
func (a *Applier) shape(ctx context.Context, dev string, kbit int) {
	if a.shaped == nil {
		a.shaped, a.shapeErr = map[string]string{}, map[string]string{}
	}
	key := strconv.Itoa(kbit)
	if a.shaped[dev] == key {
		return
	}
	if kbit <= 0 {
		_, _ = a.Sys.Run(ctx, "tc", "qdisc", "del", "dev", dev, "root") // none there is fine
		a.shaped[dev] = key
		return
	}
	cake := []string{"qdisc", "replace", "dev", dev, "root", "cake", "bandwidth", fmt.Sprintf("%dkbit", kbit), "diffserv4"}
	_, err := a.Sys.Run(ctx, "tc", cake...)
	if err != nil {
		burst := kbit * 1000 / 8 / 100 // 10 ms at the rate
		if burst < 3000 {
			burst = 3000
		}
		tbf := []string{"qdisc", "replace", "dev", dev, "root", "tbf", "rate", fmt.Sprintf("%dkbit", kbit),
			"burst", strconv.Itoa(burst), "latency", "50ms"}
		if _, err2 := a.Sys.Run(ctx, "tc", tbf...); err2 != nil {
			if msg := err2.Error(); a.shapeErr[dev] != msg {
				a.shapeErr[dev] = msg
				a.Log.Warn("circuit shaping not applied; the circuit runs unshaped", "dev", dev, "err", err2)
			}
			return // not remembered: the next apply tries again
		}
	}
	delete(a.shapeErr, dev)
	a.shaped[dev] = key
}

// swanctl writes one strongSwan file per cloud circuit, removes the files of
// circuits gone, and loads the result into charon (starting it if needed).
func (a *Applier) swanctl(ctx context.Context, circuits []desired.Circuit) error {
	dir := a.SwanctlDir
	if dir == "" {
		dir = SwanctlDir
	}
	files, _ := a.Sys.(system.Files)
	var stale, changed []string
	if files != nil {
		existing, err := files.Glob(filepath.Join(dir, "exa-*.conf"))
		if err != nil {
			return err
		}
		want := map[string]bool{}
		for _, c := range circuits {
			want[filepath.Join(dir, c.Conn()+".conf")] = true
		}
		for _, f := range existing {
			if !want[f] {
				if err := files.Remove(f); err != nil {
					return err
				}
				stale = append(stale, strings.TrimSuffix(filepath.Base(f), ".conf"))
			}
		}
	}
	if len(circuits) == 0 && len(stale) == 0 {
		return nil // no IPsec here: leave strongSwan alone
	}
	settings := a.StrongSwanConf
	if settings == "" {
		settings = StrongSwanConf
	}
	reload := false
	if len(circuits) > 0 {
		if old, err := a.Sys.ReadFile(settings); err != nil || string(old) != render.StrongSwanSettings {
			if err := a.Sys.WriteFile(settings, []byte(render.StrongSwanSettings), 0o644); err != nil {
				return err
			}
			reload = true // charon may be running with the old settings
		}
	}
	for _, c := range circuits {
		path := filepath.Join(dir, c.Conn()+".conf")
		conf := render.Swanctl(c)
		old, err := a.Sys.ReadFile(path)
		if err == nil && string(old) == conf {
			continue
		}
		if err == nil {
			changed = append(changed, c.Conn()) // an established SA would keep the old settings
		}
		if err := a.Sys.WriteFile(path, []byte(conf), 0o600); err != nil {
			return err
		}
	}
	running := a.charonRunning(ctx)
	if !running {
		if len(circuits) == 0 {
			return nil // nothing loaded to unload
		}
		if err := a.startCharon(ctx); err != nil {
			return err
		}
	} else if reload {
		_, _ = a.Sys.Run(ctx, "swanctl", "--reload-settings")
	}
	if err := a.run(ctx, "swanctl", "--load-all", "--clear", "--noprompt"); err != nil {
		return err
	}
	// Unloading a connection leaves its SAs up; end them. Errors mean there was none.
	for _, conn := range stale {
		_, _ = a.Sys.Run(ctx, "swanctl", "--terminate", "--ike", conn, "--force")
	}
	for _, conn := range changed {
		_, _ = a.Sys.Run(ctx, "swanctl", "--terminate", "--ike", conn, "--force")
		_, _ = a.Sys.Run(ctx, "swanctl", "--initiate", "--child", conn, "--timeout", "2")
	}
	return nil
}

func (a *Applier) charonRunning(ctx context.Context) bool {
	_, err := a.Sys.Run(ctx, "swanctl", "--stats")
	return err == nil
}

// charonPaths are where distributions put the IKE daemon, for when the
// `ipsec` starter script is not installed.
const charonPaths = "/usr/libexec/ipsec/charon /usr/lib/ipsec/charon /usr/lib/strongswan/charon /usr/libexec/strongswan/charon"

func (a *Applier) startCharon(ctx context.Context) error {
	if _, err := a.Sys.Run(ctx, "ipsec", "start"); err != nil {
		a.Log.Info("ipsec start failed; starting charon directly", "err", err)
		script := "for c in " + charonPaths + `; do if [ -x "$c" ]; then "$c" >/dev/null 2>&1 & exit 0; fi; done; exit 1`
		if _, err := a.Sys.Run(ctx, "sh", "-c", script); err != nil {
			return fmt.Errorf("cannot start strongSwan: %w", err)
		}
	}
	sleep := a.Sleep
	if sleep == nil {
		sleep = time.Sleep
	}
	for i := 0; i < 20; i++ {
		if a.charonRunning(ctx) {
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		sleep(250 * time.Millisecond)
	}
	return fmt.Errorf("strongSwan (charon) did not start within 5 s")
}
