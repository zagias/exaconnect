package console

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/zagias/exaconnect/agent/internal/system"
)

// Port is one network port as the box console sets it up (ADR 0046).
type Port struct {
	Iface string `json:"iface"`
	// Path is which Connect path a WAN port carries: carrier-a, carrier-b or sat.
	Path    string   `json:"path,omitempty"`
	Mode    string   `json:"mode"` // dhcp | static | pppoe | keep
	Address string   `json:"address,omitempty"`
	Gateway string   `json:"gateway,omitempty"`
	DNS     []string `json:"dns,omitempty"`
	// PPPoE sign-in from the internet provider. The password stays on the box
	// (0600) and never goes back out of the API.
	PPPoEUser     string `json:"pppoe_user,omitempty"`
	PPPoEPassword string `json:"pppoe_password,omitempty"`
}

// Network is the box's local port set-up.
type Network struct {
	WAN []Port `json:"wan"`
	LAN *Port  `json:"lan,omitempty"`
	// LANDHCP hands out addresses on the office network (dnsmasq).
	LANDHCP   bool      `json:"lan_dhcp,omitempty"`
	UpdatedAt time.Time `json:"updated_at,omitempty"`
	UpdatedBy string    `json:"updated_by,omitempty"`
}

// Caps says what this box can do, so the console offers only that.
type Caps struct {
	Netplan    bool `json:"netplan"`
	DHCPClient bool `json:"dhcp_client"`
	PPPoE      bool `json:"pppoe"`
	DHCPServer bool `json:"dhcp_server"`
}

var (
	ifaceRE = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,14}$`)
	paths   = map[string]string{"carrier-a": "Internet link 1", "carrier-b": "Internet link 2", "sat": "Satellite"}
	pppRE   = regexp.MustCompile(`^[^\s"\\]{1,128}$`)
)

// Validate checks a set-up against the ports this box has.
func (n *Network) Validate(have map[string]bool) error {
	if len(n.WAN) == 0 || len(n.WAN) > 3 {
		return errors.New("choose one to three internet (WAN) ports")
	}
	used := map[string]bool{}
	seenPath := map[string]bool{}
	for i := range n.WAN {
		p := &n.WAN[i]
		if p.Path == "" {
			p.Path = []string{"carrier-a", "carrier-b", "sat"}[i]
		}
		if _, ok := paths[p.Path]; !ok {
			return fmt.Errorf("unknown link %q", p.Path)
		}
		if seenPath[p.Path] {
			return fmt.Errorf("%s is set on two ports", paths[p.Path])
		}
		seenPath[p.Path] = true
		if err := checkPort(p, have, used, true); err != nil {
			return err
		}
	}
	if n.LAN != nil {
		n.LAN.Path = ""
		if n.LAN.Mode != "static" && n.LAN.Mode != "keep" {
			return errors.New("the office (LAN) port needs a fixed address")
		}
		if err := checkPort(n.LAN, have, used, false); err != nil {
			return err
		}
		if n.LANDHCP {
			if n.LAN.Mode != "static" {
				return errors.New("to hand out addresses, give the office port a fixed address")
			}
			if _, _, err := dhcpRange(n.LAN.Address); err != nil {
				return err
			}
		}
	} else if n.LANDHCP {
		return errors.New("choose the office (LAN) port to hand out addresses on it")
	}
	return nil
}

func checkPort(p *Port, have, used map[string]bool, wan bool) error {
	if !ifaceRE.MatchString(p.Iface) || !have[p.Iface] {
		return fmt.Errorf("this box has no port called %q", p.Iface)
	}
	if used[p.Iface] {
		return fmt.Errorf("%s is chosen twice", p.Iface)
	}
	used[p.Iface] = true
	p.Address, p.Gateway = strings.TrimSpace(p.Address), strings.TrimSpace(p.Gateway)
	for _, d := range p.DNS {
		if _, err := netip.ParseAddr(d); err != nil {
			return fmt.Errorf("DNS server %q is not an IP address", d)
		}
	}
	switch p.Mode {
	case "keep", "dhcp":
		p.Address, p.Gateway, p.PPPoEUser, p.PPPoEPassword = "", "", "", ""
	case "static":
		pf, err := netip.ParsePrefix(p.Address)
		if err != nil || !pf.Addr().Is4() || pf.Bits() < 8 || pf.Bits() > 31 {
			return fmt.Errorf("%s: give the address with its size, like 192.168.1.2/24", p.Iface)
		}
		if pf.Addr() == pf.Masked().Addr() {
			return fmt.Errorf("%s: %s is the network itself, not an address in it", p.Iface, p.Address)
		}
		if p.Gateway != "" {
			gw, err := netip.ParseAddr(p.Gateway)
			if err != nil || !pf.Contains(gw) || gw == pf.Addr() {
				return fmt.Errorf("%s: the gateway must be another address in %s", p.Iface, pf.Masked())
			}
		} else if wan {
			return fmt.Errorf("%s: an internet port with a fixed address needs a gateway", p.Iface)
		}
		p.PPPoEUser, p.PPPoEPassword = "", ""
	case "pppoe":
		if !wan {
			return errors.New("PPPoE is for internet ports only")
		}
		if !pppRE.MatchString(p.PPPoEUser) || !pppRE.MatchString(p.PPPoEPassword) {
			return fmt.Errorf("%s: PPPoE needs the user name and password from your internet provider", p.Iface)
		}
		p.Address, p.Gateway = "", ""
	default:
		return fmt.Errorf("%s: choose automatic (DHCP), fixed, PPPoE or leave as it is", p.Iface)
	}
	return nil
}

// dhcpRange is the pool handed out on the office network: the upper half of
// the subnet, leaving the lower half for fixed addresses.
func dhcpRange(cidr string) (netip.Addr, netip.Addr, error) {
	pf, err := netip.ParsePrefix(cidr)
	if err != nil || !pf.Addr().Is4() || pf.Bits() > 28 || pf.Bits() < 16 {
		return netip.Addr{}, netip.Addr{}, errors.New("to hand out addresses the office network must be between /16 and /28")
	}
	base := pf.Masked().Addr().As4()
	n := uint32(base[0])<<24 | uint32(base[1])<<16 | uint32(base[2])<<8 | uint32(base[3])
	size := uint32(1) << (32 - pf.Bits())
	start, end := n+size/2, n+size-2
	box := pf.Addr().As4()
	b := uint32(box[0])<<24 | uint32(box[1])<<16 | uint32(box[2])<<8 | uint32(box[3])
	if b >= start && b <= end {
		start, end = n+2, n+size/2-1
		if b >= start && b <= end {
			start = b + 1
		}
	}
	return u32addr(start), u32addr(end), nil
}

func u32addr(v uint32) netip.Addr {
	return netip.AddrFrom4([4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)})
}

// Public is the set-up as the API shows it: PPPoE passwords left out.
func (n Network) Public() Network {
	out := n
	out.WAN = make([]Port, len(n.WAN))
	for i, p := range n.WAN {
		if p.PPPoEPassword != "" {
			p.PPPoEPassword = "set"
		}
		out.WAN[i] = p
	}
	return out
}

// keepPasswords carries saved PPPoE passwords into a new set-up whose form
// left them as "set" (the console never shows the real one).
func (n *Network) keepPasswords(old Network) {
	for i := range n.WAN {
		if n.WAN[i].PPPoEPassword != "set" {
			continue
		}
		n.WAN[i].PPPoEPassword = ""
		for _, o := range old.WAN {
			if o.Iface == n.WAN[i].Iface && o.Mode == "pppoe" {
				n.WAN[i].PPPoEPassword = o.PPPoEPassword
			}
		}
	}
}

func loadNetwork(path string) (*Network, error) {
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var n Network
	return &n, json.Unmarshal(b, &n)
}

func saveNetwork(path string, n *Network) error {
	b, _ := json.MarshalIndent(n, "", "  ")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Backend puts a set-up on the box's ports.
type Backend interface {
	Caps() Caps
	Apply(ctx context.Context, n Network) error
	// Restore brings back, after a restart, what does not survive one.
	Restore(ctx context.Context, n Network) error
}

// HostBackend uses netplan where the box has it (Ubuntu), and ip commands
// otherwise. PPPoE uses pppd and handing out addresses uses dnsmasq.
type HostBackend struct {
	Sys system.Runner
	// Root prefixes every file path (tests).
	Root string
	// Look finds a program; nil uses exec.LookPath.
	Look func(string) bool
}

func (h HostBackend) has(prog string) bool {
	if h.Look != nil {
		return h.Look(prog)
	}
	_, err := exec.LookPath(prog)
	return err == nil
}

func (h HostBackend) file(p string) string { return filepath.Join(h.Root, p) }

func (h HostBackend) Caps() Caps {
	_, err := os.Stat(h.file("/etc/netplan"))
	np := err == nil && h.has("netplan")
	return Caps{
		Netplan:    np,
		DHCPClient: np || h.has("udhcpc") || h.has("dhclient") || h.has("dhcpcd"),
		PPPoE:      h.has("pppd"),
		DHCPServer: h.has("dnsmasq"),
	}
}

const (
	netplanFile = "/etc/netplan/90-exacarib.yaml"
	dnsmasqConf = "/etc/dnsmasq.d/exacarib-lan.conf"
	dnsmasqPid  = "/run/exacarib-dnsmasq.pid"
)

func (h HostBackend) Apply(ctx context.Context, n Network) error {
	caps := h.Caps()
	ports := append([]Port(nil), n.WAN...)
	if n.LAN != nil {
		ports = append(ports, *n.LAN)
	}
	for _, p := range ports {
		if p.Mode == "pppoe" && !caps.PPPoE {
			return errors.New("PPPoE needs the ppp package on this box (apt install ppp)")
		}
		if p.Mode == "dhcp" && !caps.DHCPClient {
			return errors.New("this box has no DHCP client")
		}
	}
	if n.LANDHCP && !caps.DHCPServer {
		return errors.New("handing out addresses needs dnsmasq on this box (apt install dnsmasq)")
	}
	var err error
	if caps.Netplan {
		err = h.applyNetplan(ctx, n)
	} else {
		err = h.applyIP(ctx, n)
	}
	if err != nil {
		return err
	}
	for i, p := range n.WAN {
		if p.Mode == "pppoe" {
			if err := h.startPPPoE(ctx, p, 100*(i+1)); err != nil {
				return err
			}
		}
	}
	if err := h.applyDHCPServer(ctx, n); err != nil {
		return err
	}
	return h.guard(ctx, &n)
}

// Restore re-applies everything on boxes without netplan, where ip settings
// do not survive a reboot. With netplan the ports come back by themselves;
// PPPoE sessions and the office DHCP pool are started again.
func (h HostBackend) Restore(ctx context.Context, n Network) error {
	if !h.Caps().Netplan {
		return h.Apply(ctx, n)
	}
	if err := h.guard(ctx, &n); err != nil {
		return err
	}
	for i, p := range n.WAN {
		if p.Mode == "pppoe" {
			if err := h.startPPPoE(ctx, p, 100*(i+1)); err != nil {
				return err
			}
		}
	}
	return h.applyDHCPServer(ctx, n)
}

// Netplan renders the box's netplan file. Ports left "as they are" are not in it.
func Netplan(n Network) string {
	var b strings.Builder
	b.WriteString("# Written by the ExaCarib box console (ADR 0046). Change it there, not here.\n")
	b.WriteString("network:\n  version: 2\n")
	head := b.Len()
	b.WriteString("  ethernets:\n")
	ports := append([]Port(nil), n.WAN...)
	metric := map[string]int{}
	for i, p := range n.WAN {
		metric[p.Iface] = 100 * (i + 1)
	}
	if n.LAN != nil {
		ports = append(ports, *n.LAN)
	}
	sort.SliceStable(ports, func(i, j int) bool { return ports[i].Iface < ports[j].Iface })
	for _, p := range ports {
		if p.Mode == "keep" {
			continue
		}
		fmt.Fprintf(&b, "    %s:\n", p.Iface)
		switch p.Mode {
		case "dhcp":
			fmt.Fprintf(&b, "      dhcp4: true\n      dhcp4-overrides:\n        route-metric: %d\n", metric[p.Iface])
		case "pppoe":
			b.WriteString("      dhcp4: false\n")
		case "static":
			fmt.Fprintf(&b, "      dhcp4: false\n      addresses: [%s]\n", p.Address)
			if p.Gateway != "" {
				fmt.Fprintf(&b, "      routes:\n        - to: default\n          via: %s\n          metric: %d\n", p.Gateway, metric[p.Iface])
			}
		}
		if len(p.DNS) > 0 {
			fmt.Fprintf(&b, "      nameservers:\n        addresses: [%s]\n", strings.Join(p.DNS, ", "))
		}
	}
	if b.Len() == head+len("  ethernets:\n") {
		return b.String()[:head] + "  ethernets: {}\n"
	}
	return b.String()
}

func (h HostBackend) applyNetplan(ctx context.Context, n Network) error {
	if err := h.Sys.WriteFile(h.file(netplanFile), []byte(Netplan(n)), 0o600); err != nil {
		return err
	}
	if _, err := h.Sys.Run(ctx, "netplan", "generate"); err != nil {
		return err
	}
	_, err := h.Sys.Run(ctx, "netplan", "apply")
	return err
}

// applyIP sets the ports with ip commands, for boxes without netplan.
func (h HostBackend) applyIP(ctx context.Context, n Network) error {
	run := func(args ...string) error { _, err := h.Sys.Run(ctx, "ip", args...); return err }
	var dns []string
	ports := append([]Port(nil), n.WAN...)
	if n.LAN != nil {
		ports = append(ports, *n.LAN)
	}
	for i, p := range ports {
		metric := fmt.Sprint(100 * (i + 1))
		if p.Mode == "keep" {
			continue
		}
		if err := run("link", "set", p.Iface, "up"); err != nil {
			return err
		}
		dns = append(dns, p.DNS...)
		switch p.Mode {
		case "static":
			if err := run("-4", "addr", "flush", "dev", p.Iface, "scope", "global"); err != nil {
				return err
			}
			if err := run("addr", "add", p.Address, "dev", p.Iface); err != nil {
				return err
			}
			if p.Gateway != "" {
				if err := run("route", "replace", "default", "via", p.Gateway, "dev", p.Iface, "metric", metric); err != nil {
					return err
				}
			}
		case "dhcp":
			var err error
			switch {
			case h.has("udhcpc"):
				_, err = h.Sys.Run(ctx, "udhcpc", "-i", p.Iface, "-n", "-q", "-t", "5")
			case h.has("dhclient"):
				_, err = h.Sys.Run(ctx, "dhclient", "-1", p.Iface)
			default:
				_, err = h.Sys.Run(ctx, "dhcpcd", "-1", p.Iface)
			}
			if err != nil {
				return fmt.Errorf("%s: no address from DHCP: %w", p.Iface, err)
			}
		}
	}
	if len(dns) > 0 {
		var b strings.Builder
		b.WriteString("# Written by the ExaCarib box console.\n")
		for _, d := range dns {
			fmt.Fprintf(&b, "nameserver %s\n", d)
		}
		if err := h.Sys.WriteFile(h.file("/etc/resolv.conf"), []byte(b.String()), 0o644); err != nil {
			return err
		}
	}
	return nil
}

// PPPoEPeer renders a pppd peer file for one internet port.
func PPPoEPeer(p Port, metric int, plugin string) string {
	return fmt.Sprintf(`# Written by the ExaCarib box console (ADR 0046).
plugin %s
nic-%s
user "%s"
password "%s"
noauth
defaultroute
defaultroute-metric %d
replacedefaultroute
usepeerdns
persist
maxfail 0
holdoff 5
noipdefault
hide-password
mtu 1492
mru 1492
`, plugin, p.Iface, p.PPPoEUser, p.PPPoEPassword, metric)
}

func (h HostBackend) startPPPoE(ctx context.Context, p Port, metric int) error {
	plugin := "rp-pppoe.so"
	if m, _ := filepath.Glob(h.file("/usr/lib/pppd/*/pppoe.so")); len(m) > 0 {
		plugin = "pppoe.so"
	}
	name := "exacarib-" + p.Iface
	if err := h.Sys.WriteFile(h.file("/etc/ppp/peers/"+name), []byte(PPPoEPeer(p, metric, plugin)), 0o600); err != nil {
		return err
	}
	_, _ = h.Sys.Run(ctx, "pkill", "-f", "pppd call "+name)
	_, err := h.Sys.Run(ctx, "pppd", "call", name)
	return err
}

// DNSMasq renders the office network's DHCP pool.
func DNSMasq(lan Port) (string, error) {
	start, end, err := dhcpRange(lan.Address)
	if err != nil {
		return "", err
	}
	pf, _ := netip.ParsePrefix(lan.Address)
	return fmt.Sprintf(`# Written by the ExaCarib box console (ADR 0046).
interface=%s
bind-interfaces
dhcp-range=%s,%s,12h
dhcp-option=option:router,%s
dhcp-option=option:dns-server,%s
`, lan.Iface, start, end, pf.Addr(), pf.Addr()), nil
}

func (h HostBackend) applyDHCPServer(ctx context.Context, n Network) error {
	conf := h.file(dnsmasqConf)
	if !n.LANDHCP || n.LAN == nil {
		// Also stops the office DHCP a fresh ExaCarib image runs until set-up (same pid file).
		_, errConf := os.Stat(conf)
		_, errPid := os.Stat(h.file(dnsmasqPid))
		if errConf == nil || errPid == nil {
			_ = os.Remove(conf)
			h.restartDNSMasq(ctx, conf, false)
		}
		return nil
	}
	body, err := DNSMasq(*n.LAN)
	if err != nil {
		return err
	}
	if err := h.Sys.WriteFile(conf, []byte(body), 0o644); err != nil {
		return err
	}
	return h.restartDNSMasq(ctx, conf, true)
}

func (h HostBackend) restartDNSMasq(ctx context.Context, conf string, on bool) error {
	// The full dnsmasq package runs as a service and reads /etc/dnsmasq.d;
	// dnsmasq-base (what install.sh --box adds) is run here with our file only.
	for _, u := range []string{"/lib/systemd/system/dnsmasq.service", "/usr/lib/systemd/system/dnsmasq.service", "/etc/systemd/system/dnsmasq.service"} {
		if _, err := os.Stat(h.file(u)); err == nil && h.has("systemctl") {
			_, err := h.Sys.Run(ctx, "systemctl", "restart", "dnsmasq")
			return err
		}
	}
	pid := h.file(dnsmasqPid)
	if b, err := os.ReadFile(pid); err == nil {
		_, _ = h.Sys.Run(ctx, "kill", strings.TrimSpace(string(b)))
	}
	if !on {
		return nil
	}
	_, err := h.Sys.Run(ctx, "dnsmasq", "--conf-file="+conf, "--pid-file="+pid)
	return err
}
