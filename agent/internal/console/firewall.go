package console

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// The box's own firewall (ADR 0047): what may reach the box itself. Traffic
// through the box is the agent's (the exa_inet table, ADR 0012). Everything
// new arriving on an internet port is dropped; the tunnels, routing and
// probes run inside the wg- interfaces or answer what the box started. The
// console, DHCP and DNS answer on the other ports only. An address opening
// console connections too fast is dropped for a while, and TCP SYNs are
// limited overall, as on the PoP (ADR 0012); the kernel's SYN cookies are on.
const (
	BoxNFTable   = "exa_box"
	boxNFTFile   = "/run/exacarib-box.nft"
	floodPerSrc  = 50    // new connections a second from one address
	floodBlock   = "10m" // how long such an address is then dropped
	icmpPerSec   = 10
	synAllPerSec = 2000
)

// wanIfaces are the ports marked as internet links, quoted for nft.
func wanIfaces(n *Network) []string {
	if n == nil {
		return nil
	}
	var out []string
	for _, p := range n.WAN {
		out = append(out, strconv.Quote(p.Iface))
	}
	sort.Strings(out)
	return out
}

// BoxNFT renders the exa_box table for `nft -f`, replacing any earlier one.
// Before any port is marked as an internet link (a fresh box) the console
// answers on every port; it still refuses addresses that are not private.
func BoxNFT(n *Network) string {
	var b strings.Builder
	fmt.Fprintf(&b, "table inet %s {}\ndelete table inet %s\n", BoxNFTable, BoxNFTable)
	fmt.Fprintf(&b, "table inet %s {\n", BoxNFTable)
	for _, v := range []string{"4", "6"} {
		t := map[string]string{"4": "ipv4_addr", "6": "ipv6_addr"}[v]
		fmt.Fprintf(&b, "\tset block%s {\n\t\ttype %s\n\t\tflags dynamic, timeout\n\t\ttimeout %s\n\t\tsize 65536\n\t}\n", v, t, floodBlock)
		fmt.Fprintf(&b, "\tset rate%s {\n\t\ttype %s\n\t\tflags dynamic, timeout\n\t\ttimeout 1m\n\t\tsize 65536\n\t}\n", v, t)
	}
	b.WriteString("\tchain input {\n\t\ttype filter hook input priority filter; policy drop;\n")
	b.WriteString("\t\tiif \"lo\" accept\n")
	b.WriteString("\t\tct state invalid drop\n")
	b.WriteString("\t\tct state established,related accept\n")
	b.WriteString("\t\tiifname \"wg-*\" accept comment \"tunnels: BGP, BFD and probes\"\n")
	b.WriteString("\t\tip saddr @block4 counter drop comment \"flood\"\n")
	b.WriteString("\t\tip6 saddr @block6 counter drop comment \"flood\"\n")
	fmt.Fprintf(&b, "\t\ttcp dport 443 ct state new update @rate4 { ip saddr limit rate over %d/second burst %d packets } add @block4 { ip saddr } counter drop comment \"flood\"\n", floodPerSrc, 2*floodPerSrc)
	fmt.Fprintf(&b, "\t\ttcp dport 443 ct state new update @rate6 { ip6 saddr limit rate over %d/second burst %d packets } add @block6 { ip6 saddr } counter drop comment \"flood\"\n", floodPerSrc, 2*floodPerSrc)
	fmt.Fprintf(&b, "\t\ttcp flags & (syn|ack) == syn limit rate over %d/second burst %d packets counter drop comment \"syn\"\n", synAllPerSec, synAllPerSec)
	fmt.Fprintf(&b, "\t\ticmp type echo-request limit rate %d/second accept\n", icmpPerSec)
	b.WriteString("\t\ticmp type { destination-unreachable, time-exceeded, parameter-problem } accept\n")
	fmt.Fprintf(&b, "\t\ticmpv6 type echo-request limit rate %d/second accept\n", icmpPerSec)
	b.WriteString("\t\ticmpv6 type { destination-unreachable, packet-too-big, time-exceeded, parameter-problem, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept\n")
	b.WriteString("\t\tudp sport 67 udp dport 68 accept comment \"DHCP answers to the box\"\n")
	if wan := wanIfaces(n); len(wan) > 0 {
		set := wan[0]
		if len(wan) > 1 {
			set = "{ " + strings.Join(wan, ", ") + " }"
		}
		fmt.Fprintf(&b, "\t\tiifname %s counter drop comment \"inbound\"\n", set)
		b.WriteString("\t\tiifname \"ppp*\" counter drop comment \"inbound\"\n")
	}
	b.WriteString("\t\ttcp dport 443 accept comment \"box console\"\n")
	b.WriteString("\t\tudp dport 67 accept comment \"DHCP for the office\"\n")
	b.WriteString("\t\tudp dport 161 accept comment \"SNMP from the office\"\n")
	b.WriteString("\t\tmeta l4proto { tcp, udp } th dport 53 accept comment \"DNS for the office\"\n")
	b.WriteString("\t\tcounter comment \"other\"\n")
	b.WriteString("\t}\n}\n")
	return b.String()
}

// guard puts the box's own firewall in place, where the box has nftables.
func (h HostBackend) guard(ctx context.Context, n *Network) error {
	if !h.has("nft") {
		return nil
	}
	f := h.file(boxNFTFile)
	if err := h.Sys.WriteFile(f, []byte(BoxNFT(n)), 0o600); err != nil {
		return err
	}
	if _, err := h.Sys.Run(ctx, "nft", "-f", f); err != nil {
		return fmt.Errorf("box firewall: %w", err)
	}
	return nil
}
