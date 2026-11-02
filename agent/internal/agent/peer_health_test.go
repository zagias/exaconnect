package agent

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

func popHealthAgent(t *testing.T) *Agent {
	t.Helper()
	a := slaAgent(t)
	a.current.Role = desired.RolePoP
	a.current.Tunnels = nil
	a.steerMap.Paths = nil
	for i, path := range []string{"carrier-a", "carrier-b", "sat"} {
		tunnel := []string{"wg-a", "wg-b", "wg-sat"}[i]
		a.steerMap.Paths = append(a.steerMap.Paths, steer.Path{Name: path, Tunnel: tunnel, Table: 101 + i})
		tun := desired.Tunnel{Name: tunnel, Address: fmt.Sprintf("100.64.%d.1/24", i+1)}
		for j, site := range []string{"site-a", "site-b"} {
			peer := fmt.Sprintf("100.64.%d.%d", i+1, j+11)
			tun.Peers = append(tun.Peers, desired.Peer{Name: site, AllowedIPs: []string{peer + "/32", fmt.Sprintf("192.168.%d.0/24", (j+1)*10)}})
			tun.Neighbors = append(tun.Neighbors, desired.Neighbor{Address: peer})
			a.bfd[peer] = bfdPeer{Tunnel: tunnel, Status: "up"}
		}
		a.current.Tunnels = append(a.current.Tunnels, tun)
	}
	a.steerMap.Rules = []steer.Rule{
		{Class: "voice", Dst: "192.168.10.0/24", Paths: []string{"carrier-a", "carrier-b", "sat"}},
		{Class: "bulk", Dst: "192.168.10.0/24", Paths: []string{"carrier-a", "carrier-b"}, PauseIfNone: true},
		{Class: "voice", Dst: "192.168.20.0/24", Paths: []string{"carrier-a", "carrier-b"}, PauseIfNone: true},
		{Class: "bulk", Dst: "192.168.20.0/24", Paths: []string{"carrier-a", "carrier-b"}, PauseIfNone: true},
	}
	return a
}

func TestPoPFailureAndRecoveryStayWithinPeerDestination(t *testing.T) {
	for _, silent := range []bool{false, true} {
		t.Run(fmt.Sprintf("controller_silent_%t", silent), func(t *testing.T) {
			a := popHealthAgent(t)
			a.silent = silent
			for _, peer := range []string{"100.64.1.11", "100.64.2.11"} {
				state := a.bfd[peer]
				state.Status = "down"
				a.bfd[peer] = state
			}
			a.steer(context.Background(), "bfd")
			if a.steerErr != "" || len(a.choices) != 4 {
				t.Fatalf("steering failed: %q %+v", a.steerErr, a.choices)
			}
			if a.choices[0].Path != "sat" || !a.choices[1].Paused || a.choices[2].Path != "carrier-a" || a.choices[3].Path != "carrier-a" {
				t.Fatalf("site-a failure affected another site or violated satellite policy: %+v", a.choices)
			}
			batch := a.Sys.(*fakeSys).files[filepath.Join(a.Steerer.StateDir, "steer.rules")]
			for _, rule := range []string{
				"rule add pref 1000 fwmark 0x101 to 192.168.10.0/24 lookup 103",
				"rule add pref 1010 fwmark 0x103 to 192.168.10.0/24 blackhole",
				"rule add pref 1020 fwmark 0x101 to 192.168.20.0/24 lookup 101",
				"rule add pref 1030 fwmark 0x103 to 192.168.20.0/24 lookup 101",
			} {
				if !strings.Contains(batch, rule) {
					t.Fatalf("kernel batch missing %q:\n%s", rule, batch)
				}
			}
			for _, peer := range []string{"100.64.1.11", "100.64.2.11"} {
				state := a.bfd[peer]
				state.Status = "up"
				a.bfd[peer] = state
			}
			a.steer(context.Background(), "bfd")
			for _, choice := range a.choices {
				if choice.Path != "carrier-a" || choice.Paused {
					t.Fatalf("recovery did not restore terrestrial forwarding: %+v", a.choices)
				}
			}
		})
	}
}

func TestPoPUnprovenOwnershipKeepsConservativeBFDDecision(t *testing.T) {
	for _, scenario := range []string{"unscoped", "unknown_destination", "spans_peers", "split_destination", "ambiguous_owner", "ambiguous_neighbour", "missing_session", "wrong_tunnel", "blank_status"} {
		for _, unrelatedDown := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s_unrelated_down_%t", scenario, unrelatedDown), func(t *testing.T) {
				a := popHealthAgent(t)
				if unrelatedDown {
					a.bfd["100.64.1.11"] = bfdPeer{Tunnel: "wg-a", Status: "down"}
				}
				rule := a.steerMap.Rules[2]
				switch scenario {
				case "unscoped":
					rule.Dst = ""
				case "unknown_destination":
					rule.Dst = "10.99.0.0/24"
				case "spans_peers":
					rule.Dst = "192.168.0.0/16"
				case "split_destination":
					rule.Dst = "192.168.0.0/16"
					a.current.Tunnels[0].Peers[0].AllowedIPs = append(a.current.Tunnels[0].Peers[0].AllowedIPs, rule.Dst)
				case "ambiguous_owner":
					a.current.Tunnels[0].Peers = append(a.current.Tunnels[0].Peers, a.current.Tunnels[0].Peers[1])
				case "ambiguous_neighbour":
					a.current.Tunnels[0].Peers[0].AllowedIPs = append(a.current.Tunnels[0].Peers[0].AllowedIPs, "100.64.1.12/32")
				case "missing_session":
					delete(a.bfd, "100.64.1.12")
				case "wrong_tunnel":
					a.bfd["100.64.1.12"] = bfdPeer{Tunnel: "wg-b", Status: "up"}
				case "blank_status":
					a.bfd["100.64.1.12"] = bfdPeer{Tunnel: "wg-a"}
				}
				want := scenario == "unscoped" && !unrelatedDown
				if got := a.usableForRule(a.steerMap, rule)("carrier-a"); got != want {
					t.Fatalf("unproven peer borrowed another destination's health or changed the unscoped policy: got %t want %t", got, want)
				}
			})
		}
	}
}

func TestPoPDestinationOwnershipUsesLongestAllowedPrefix(t *testing.T) {
	a := popHealthAgent(t)
	a.current.Tunnels[0].Peers[0].AllowedIPs = append(a.current.Tunnels[0].Peers[0].AllowedIPs, "192.168.0.0/16")
	a.bfd["100.64.1.11"] = bfdPeer{Tunnel: "wg-a", Status: "down"}
	if !a.usableForRule(a.steerMap, a.steerMap.Rules[2])("carrier-a") {
		t.Fatal("broader peer prefix shadowed the healthy destination owner")
	}
}

func TestPoPNeighbourOwnershipUsesLongestAllowedPrefix(t *testing.T) {
	a := popHealthAgent(t)
	a.current.Tunnels[0].Peers[1].AllowedIPs = []string{"100.64.1.0/24", "192.168.20.0/24"}
	a.bfd["100.64.1.11"] = bfdPeer{Tunnel: "wg-a", Status: "down"}
	if !a.usableForRule(a.steerMap, a.steerMap.Rules[2])("carrier-a") {
		t.Fatal("broader neighbour prefix borrowed the failed site's BFD state")
	}
}
