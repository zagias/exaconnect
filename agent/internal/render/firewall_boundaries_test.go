package render

import (
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

func TestForwardAllowlistBoundToOriginalPublicPort(t *testing.T) {
	s := inetPoP()
	s.Internet.PortForwards = []desired.PortForward{
		{ID: 1, Protocol: "tcp", Port: 8080, ToAddress: "192.168.10.10", ToPort: 80, AllowFrom: []string{"203.0.113.1"}},
		{ID: 2, Protocol: "tcp", Port: 8081, ToAddress: "192.168.10.10", ToPort: 80, AllowFrom: []string{"198.51.100.1"}},
	}
	text := InternetNFT(s)
	for _, pair := range []struct{ port, source string }{{"8080", "203.0.113.1"}, {"8081", "198.51.100.1"}} {
		want := "ct original ip daddr 100.64.0.2 ct original proto-dst " + pair.port + " ip daddr 192.168.10.10 tcp dport 80 ip saddr { " + pair.source + " } counter accept"
		if !strings.Contains(text, want) {
			t.Fatalf("forward %s does not bind its own allowlist: %s", pair.port, text)
		}
	}
	if strings.Index(text, "ct state invalid drop") > strings.Index(text, "ct state established,related accept") {
		t.Fatal("invalid traffic must be discarded before established return traffic")
	}
}

func TestIPv6ForwardingCannotBypassIPv4InternetPolicy(t *testing.T) {
	for _, mode := range []string{desired.InternetPoP, desired.InternetLocal, desired.InternetOff, desired.InternetGateway} {
		s := &desired.State{Internet: &desired.Internet{Mode: mode}}
		text := InternetForwardGuard(s)
		if !strings.Contains(text, "meta nfproto ipv6 counter drop") || !strings.Contains(text, "hook forward") {
			t.Fatalf("%s has no IPv6 forwarding boundary: %s", mode, text)
		}
		if strings.Contains(text, "hook input") || strings.Contains(text, "hook output") || strings.Contains(text, "flush ruleset") {
			t.Fatalf("guard must leave management/host traffic and other tables alone: %s", text)
		}
	}
	if InternetForwardGuard(&desired.State{}) != "" {
		t.Fatal("an unmanaged internet policy must not install a forwarding guard")
	}
}

func TestInternetOffGuardIsIndependentOfRoutingAndConntrack(t *testing.T) {
	s := &desired.State{Internet: &desired.Internet{Mode: desired.InternetOff,
		LANPrefixes: []string{"192.168.10.0/24"}, CorporatePrefixes: []string{"10.254.0.0/24", "192.168.0.0/16"}}}
	text := InternetForwardGuard(s)
	for _, rule := range []string{
		"ip saddr { 192.168.10.0/24 } ip daddr != { 10.254.0.0/24, 192.168.0.0/16 } counter drop",
		"ip daddr { 192.168.10.0/24 } ip saddr != { 10.254.0.0/24, 192.168.0.0/16 } counter drop",
	} {
		if !strings.Contains(text, rule) {
			t.Fatalf("off policy must stop public forwarding in both directions: %s", text)
		}
	}
	if strings.Contains(text, "ct state") || strings.Contains(text, "meta mark") {
		t.Fatalf("off boundary must apply to existing and classified flows: %s", text)
	}
	s.Internet.CorporatePrefixes = nil
	text = InternetForwardGuard(s)
	if strings.Contains(text, "!=") || !strings.Contains(text, "ip saddr { 192.168.10.0/24 } counter drop") {
		t.Fatalf("missing corporate inventory must fail closed: %s", text)
	}
}
