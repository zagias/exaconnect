package render

import (
	"fmt"
	"net/netip"
	"sort"
	"strconv"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// InternetNFTable is the nftables table (family ip) for internet NAT and firewall.
const InternetNFTable = "exa_inet"

// Rule comments that name a counter in telemetry.
const (
	CommentRule    = "fw"      // fw<id>: a firewall rule
	CommentForward = "pf"      // pf<id>: a port forward
	CommentInbound = "inbound" // the drop of everything else arriving from the internet
	// The PoP's protection (ADR 0012), each a drop of new inbound traffic.
	CommentBlocked = "blocked" // from the block list
	CommentAuto    = "auto"    // from a source blocked automatically
	CommentFlood   = "flood"   // a source over its limit, which is then blocked
	CommentSYN     = "syn"     // TCP SYNs over the limit for all sources
)

// AutoBlockSet is the dynamic set of automatically blocked sources.
const AutoBlockSet = "auto_block"

// InternetNFT renders the `ip exa_inet` table for `nft -f`, replacing any
// earlier one atomically. The forward chain is filter_fwd: nft reserves
// "fwd" (the contract's name) as a keyword. A protected PoP also has the
// guard chain and its sets (ADR 0012). It is "" where the node has no such
// table: no internet block, or a site in pop or off mode.
func InternetNFT(s *desired.State) string {
	if !s.Firewalled() {
		return ""
	}
	in := s.Internet
	var ifs []string
	for _, u := range in.Uplinks {
		ifs = append(ifs, strconv.Quote(u.Interface))
	}
	uplinks := "{ " + strings.Join(ifs, ", ") + " }"
	var b strings.Builder
	fmt.Fprintf(&b, "table ip %s {}\ndelete table ip %s\n", InternetNFTable, InternetNFTable)
	fmt.Fprintf(&b, "table ip %s {\n", InternetNFTable)
	b.WriteString("\tset lan {\n\t\ttype ipv4_addr\n\t\tflags interval\n")
	if lan := nftPrefixes(in.LANPrefixes); len(lan) > 0 {
		fmt.Fprintf(&b, "\t\telements = { %s }\n", strings.Join(lan, ", "))
	}
	b.WriteString("\t}\n")
	if s.Protected() {
		guard(&b, ifs[0], in.PublicAddress, in.Protection)
	}
	if s.Role == desired.RolePoP && len(in.PortForwards) > 0 {
		b.WriteString("\tchain pre {\n\t\ttype nat hook prerouting priority dstnat;\n")
		for _, f := range in.PortForwards {
			fmt.Fprintf(&b, "\t\tiifname %s ip daddr %s %s dport %d dnat to %s:%d\n",
				ifs[0], in.PublicAddress, f.Protocol, f.Port, f.ToAddress, f.ToPort)
		}
		b.WriteString("\t}\n")
	}
	b.WriteString("\tchain post {\n\t\ttype nat hook postrouting priority srcnat;\n")
	if len(ifs) > 0 {
		fmt.Fprintf(&b, "\t\toifname %s ip saddr @lan masquerade\n", uplinks)
	}
	b.WriteString("\t}\n")
	b.WriteString("\tchain filter_fwd {\n\t\ttype filter hook forward priority filter; policy accept;\n")
	b.WriteString("\t\tct state established,related accept\n")
	if len(ifs) > 0 {
		if s.Role == desired.RolePoP {
			for _, f := range in.PortForwards {
				r := fmt.Sprintf("iifname %s ct status dnat ip daddr %s %s dport %d", uplinks, f.ToAddress, f.Protocol, f.ToPort)
				if from := nftPrefixes(f.AllowFrom); len(from) > 0 {
					r += " ip saddr { " + strings.Join(from, ", ") + " }"
				}
				fmt.Fprintf(&b, "\t\t%s counter accept comment \"%s%d\"\n", r, CommentForward, f.ID)
			}
		}
		fmt.Fprintf(&b, "\t\tiifname %s counter drop comment %q\n", uplinks, CommentInbound)
		for _, r := range in.Firewall {
			fmt.Fprintf(&b, "\t\t%s counter %s comment \"%s%d\"\n", firewallMatch(uplinks, r), verdict(r.Action), CommentRule, r.ID)
		}
	}
	b.WriteString("\t}\n}\n")
	return b.String()
}

// guard renders the PoP's protection sets and its filter chain, which sees
// traffic before NAT. Only new inbound traffic to the public address meets
// the limits: replies to outbound NAT are established.
func guard(b *strings.Builder, uplink, public string, p *desired.Protection) {
	b.WriteString("\tset blocklist {\n\t\ttype ipv4_addr\n\t\tflags interval\n")
	if list := nftPrefixes(p.Blocklist); len(list) > 0 {
		fmt.Fprintf(b, "\t\telements = { %s }\n", strings.Join(list, ", "))
	}
	b.WriteString("\t}\n")
	fmt.Fprintf(b, "\tset %s {\n\t\ttype ipv4_addr\n\t\tflags dynamic, timeout\n\t\ttimeout %dm\n\t\tsize 65536\n\t}\n", AutoBlockSet, p.BlockMinutes)
	b.WriteString("\tset rate {\n\t\ttype ipv4_addr\n\t\tflags dynamic, timeout\n\t\ttimeout 1m\n\t\tsize 65536\n\t}\n")
	b.WriteString("\tchain guard {\n\t\ttype filter hook prerouting priority -150; policy accept;\n")
	to := fmt.Sprintf("iifname %s ip daddr %s", uplink, public)
	fmt.Fprintf(b, "\t\t%s ip saddr @blocklist counter drop comment %q\n", to, CommentBlocked)
	fmt.Fprintf(b, "\t\t%s ip saddr @%s counter drop comment %q\n", to, AutoBlockSet, CommentAuto)
	fmt.Fprintf(b, "\t\t%s ct state new update @rate { ip saddr limit rate over %d/second burst %d packets } add @%s { ip saddr } counter drop comment %q\n",
		to, p.NewPerSource, 2*p.NewPerSource, AutoBlockSet, CommentFlood)
	fmt.Fprintf(b, "\t\t%s tcp flags & (syn|ack) == syn limit rate over %d/second burst %d packets counter drop comment %q\n",
		to, p.SynPerS, p.SynPerS, CommentSYN)
	b.WriteString("\t}\n")
}

func verdict(action string) string {
	if action == "allow" {
		return "accept"
	}
	return "drop"
}

func firewallMatch(uplinks string, r desired.FirewallRule) string {
	parts := []string{"oifname " + uplinks, "ip saddr @lan"}
	if src := nftPrefixes(r.Src); len(src) > 0 {
		parts = append(parts, "ip saddr { "+strings.Join(src, ", ")+" }")
	}
	if dst := nftPrefixes(r.Dst); len(dst) > 0 {
		parts = append(parts, "ip daddr { "+strings.Join(dst, ", ")+" }")
	}
	ports, _ := desired.ParsePorts(r.Ports)
	switch {
	case r.Protocol == "any":
	case len(ports) > 0:
		list := make([]string, len(ports))
		for i, p := range ports {
			list[i] = strconv.Itoa(p.From)
			if p.To != p.From {
				list[i] += "-" + strconv.Itoa(p.To)
			}
		}
		parts = append(parts, fmt.Sprintf("%s dport { %s }", r.Protocol, strings.Join(list, ", ")))
	default:
		parts = append(parts, "meta l4proto "+r.Protocol)
	}
	return strings.Join(parts, " ")
}

// nftPrefixes masks, sorts and de-overlaps IPv4 prefixes (or addresses) for
// an nft interval set, which rejects overlapping elements.
func nftPrefixes(list []string) []string {
	var ps []netip.Prefix
	for _, s := range list {
		if a, err := netip.ParseAddr(s); err == nil {
			ps = append(ps, netip.PrefixFrom(a, 32))
		} else if p, err := netip.ParsePrefix(s); err == nil {
			ps = append(ps, p.Masked())
		}
	}
	sort.Slice(ps, func(i, j int) bool {
		if ps[i].Bits() != ps[j].Bits() {
			return ps[i].Bits() < ps[j].Bits()
		}
		return ps[i].Addr().Less(ps[j].Addr())
	})
	var keep []netip.Prefix
next:
	for _, p := range ps {
		for _, k := range keep {
			if k.Contains(p.Addr()) {
				continue next
			}
		}
		keep = append(keep, p)
	}
	sort.Slice(keep, func(i, j int) bool { return keep[i].Addr().Less(keep[j].Addr()) })
	out := make([]string, len(keep))
	for i, p := range keep {
		if p.Bits() == 32 {
			out[i] = p.Addr().String()
		} else {
			out[i] = p.String()
		}
	}
	return out
}

// InternetRules renders the ip rules (without "rule add") that send a node's
// LAN traffic with no more specific route to the internet table: the main
// table without its default route first, then the internet table.
func InternetRules(in *desired.Internet, prefMain, prefInternet, table int) []string {
	if in == nil {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, s := range in.LANPrefixes {
		p, err := netip.ParsePrefix(s)
		if err != nil {
			continue
		}
		src := p.Masked().String()
		if seen[src] {
			continue
		}
		seen[src] = true
		out = append(out,
			fmt.Sprintf("pref %d from %s lookup main suppress_prefixlength 0", prefMain, src),
			fmt.Sprintf("pref %d from %s lookup %d", prefInternet, src, table))
	}
	return out
}
