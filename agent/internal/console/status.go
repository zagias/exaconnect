package console

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Iface is one port on the box as the console shows it.
type Iface struct {
	Name    string   `json:"name"`
	MAC     string   `json:"mac,omitempty"`
	Up      bool     `json:"up"`
	Oper    string   `json:"oper"` // up, down, unknown (no cable is "down")
	SpeedMb int      `json:"speed_mbps,omitempty"`
	Addrs   []string `json:"addresses"`
	RxBytes uint64   `json:"rx_bytes"`
	TxBytes uint64   `json:"tx_bytes"`
	// Role is what the console set it up as: wan, lan or empty.
	Role string `json:"role,omitempty"`
	Path string `json:"path,omitempty"`
}

// skipIface leaves out the box's own tunnels and other virtual devices.
func skipIface(name string) bool {
	for _, p := range []string{"lo", "wg-", "vx", "veth", "docker", "br-", "exa", "ip6tnl", "sit", "tunl", "gre", "erspan", "ppp", "dummy", "ifb", "nlmon", "vrf"} {
		if name == p || strings.HasPrefix(name, p) && p != "lo" {
			return true
		}
	}
	return false
}

func sysNet(root, name, file string) string {
	b, err := os.ReadFile(filepath.Join(root, "/sys/class/net", name, file))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// Ports lists the box's ports with their addresses and counters.
func Ports(root string) ([]Iface, error) {
	ifs, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	out := []Iface{}
	for _, i := range ifs {
		if i.Flags&net.FlagLoopback != 0 || skipIface(i.Name) {
			continue
		}
		p := Iface{Name: i.Name, MAC: i.HardwareAddr.String(), Up: i.Flags&net.FlagUp != 0, Addrs: []string{}}
		p.Oper = sysNet(root, i.Name, "operstate")
		if p.Oper == "" {
			p.Oper = "unknown"
		}
		p.SpeedMb, _ = strconv.Atoi(sysNet(root, i.Name, "speed"))
		if p.SpeedMb < 0 {
			p.SpeedMb = 0
		}
		p.RxBytes, _ = strconv.ParseUint(sysNet(root, i.Name, "statistics/rx_bytes"), 10, 64)
		p.TxBytes, _ = strconv.ParseUint(sysNet(root, i.Name, "statistics/tx_bytes"), 10, 64)
		if addrs, err := i.Addrs(); err == nil {
			for _, a := range addrs {
				if pf, err := netip.ParsePrefix(a.String()); err == nil && !pf.Addr().IsLinkLocalUnicast() {
					p.Addrs = append(p.Addrs, pf.String())
				}
			}
		}
		out = append(out, p)
	}
	return out, nil
}

// Route is one row of the main routing table.
type Route struct {
	Dst      string `json:"dst"`
	Gateway  string `json:"gateway,omitempty"`
	Dev      string `json:"dev,omitempty"`
	Protocol string `json:"protocol,omitempty"`
	Metric   int    `json:"metric,omitempty"`
}

// ParseRoutes reads `ip -j route show` output.
func ParseRoutes(b []byte) ([]Route, error) {
	var rs []Route
	if len(bytes.TrimSpace(b)) == 0 {
		return []Route{}, nil
	}
	if err := json.Unmarshal(b, &rs); err != nil {
		return nil, err
	}
	return rs, nil
}

var targetRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$`)

// Diag runs one diagnostic: ping, traceroute, dns or controller.
func Diag(ctx context.Context, tool, target, iface, controller string) (string, error) {
	target = strings.TrimSpace(target)
	if tool == "controller" {
		if target == "" {
			target = controller
		}
		return checkController(ctx, target)
	}
	if !targetRE.MatchString(target) {
		return "", errors.New("give an address or a name, like 1.1.1.1 or exacarib.com")
	}
	if iface != "" {
		if _, err := net.InterfaceByName(iface); err != nil || !ifaceRE.MatchString(iface) {
			return "", errors.New("unknown port")
		}
	}
	ctx, cancel := context.WithTimeout(ctx, 40*time.Second)
	defer cancel()
	var cmd *exec.Cmd
	switch tool {
	case "ping":
		args := []string{"-c", "4", "-W", "2"}
		if iface != "" {
			args = append(args, "-I", iface)
		}
		cmd = exec.CommandContext(ctx, "ping", append(args, target)...)
	case "traceroute":
		if _, err := exec.LookPath("traceroute"); err == nil {
			args := []string{"-n", "-w", "2", "-q", "1", "-m", "20"}
			if iface != "" {
				args = append(args, "-i", iface)
			}
			cmd = exec.CommandContext(ctx, "traceroute", append(args, target)...)
		} else {
			cmd = exec.CommandContext(ctx, "tracepath", "-n", "-m", "20", target)
		}
	case "dns":
		return lookup(ctx, target)
	default:
		return "", errors.New("unknown tool")
	}
	out, err := cmd.CombinedOutput()
	if len(out) > 64<<10 {
		out = out[:64<<10]
	}
	if err != nil && len(out) == 0 {
		return "", err
	}
	return string(out), nil
}

func lookup(ctx context.Context, name string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	start := time.Now()
	addrs, err := net.DefaultResolver.LookupHost(ctx, name)
	if err != nil {
		return fmt.Sprintf("%s: no answer (%v)\n", name, err), nil
	}
	return fmt.Sprintf("%s has %s\n(answered in %d ms)\n", name, strings.Join(addrs, ", "), time.Since(start).Milliseconds()), nil
}

// checkController checks the controller answers on its agent address, the
// way the agent reaches it. It reads only the public CA.
func checkController(ctx context.Context, base string) (string, error) {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	if !strings.HasPrefix(base, "https://") || strings.ContainsAny(base, " \"'<>") {
		return "", errors.New("give the controller address, like https://connect.exacarib.com:8443")
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	hc := &http.Client{Transport: &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true, MinVersion: tls.VersionTLS12}, //nolint:gosec // reachability only
	}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/v1/ca.pem", nil)
	if err != nil {
		return "", err
	}
	start := time.Now()
	resp, err := hc.Do(req)
	if err != nil {
		return fmt.Sprintf("Cannot reach %s\n%v\n", base, err), nil
	}
	resp.Body.Close()
	return fmt.Sprintf("%s answered %s in %d ms\n", base, resp.Status, time.Since(start).Milliseconds()), nil
}

// JoinInfo is what a box needs to join Connect: the controller's agent
// address, the one-time enrolment token, the CA fingerprint and the site name.
type JoinInfo struct {
	Controller  string `json:"c"`
	Token       string `json:"t"`
	Fingerprint string `json:"f"`
	Name        string `json:"n"`
}

// The box setup code the portal shows: "EXA1." and the four values as
// base64url JSON, so one paste carries them all.
const joinPrefix = "EXA1."

var (
	nameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,30}$`)
	fpRE   = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// ParseJoinCode reads a box setup code from the portal.
func ParseJoinCode(code string) (JoinInfo, error) {
	code = strings.Join(strings.Fields(code), "")
	if !strings.HasPrefix(code, joinPrefix) {
		return JoinInfo{}, errors.New("that is not a box setup code; it starts with EXA1.")
	}
	b, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(code[len(joinPrefix):], "="))
	if err != nil {
		return JoinInfo{}, errors.New("the setup code is incomplete; copy it again from the portal")
	}
	var j JoinInfo
	if err := json.Unmarshal(b, &j); err != nil {
		return JoinInfo{}, errors.New("the setup code is incomplete; copy it again from the portal")
	}
	return j, j.Check()
}

// Check validates the four values.
func (j *JoinInfo) Check() error {
	j.Controller = strings.TrimRight(strings.TrimSpace(j.Controller), "/")
	j.Fingerprint = strings.ToLower(strings.ReplaceAll(strings.TrimSpace(j.Fingerprint), ":", ""))
	j.Token, j.Name = strings.TrimSpace(j.Token), strings.TrimSpace(j.Name)
	if !strings.HasPrefix(j.Controller, "https://") || len(j.Controller) > 200 || strings.ContainsAny(j.Controller, " \"'<>") {
		return errors.New("the controller address must start with https://")
	}
	if len(j.Token) < 10 || len(j.Token) > 200 {
		return errors.New("the enrolment token is missing or cut short")
	}
	if !fpRE.MatchString(j.Fingerprint) {
		return errors.New("the CA fingerprint should be 64 letters and digits")
	}
	if !nameRE.MatchString(j.Name) {
		return errors.New("the box name is lower-case letters, digits and dashes")
	}
	return nil
}
