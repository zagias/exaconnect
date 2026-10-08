package render

import (
	"encoding/base64"
	"fmt"
	"strings"

	"github.com/zagias/exaconnect/agent/internal/desired"
)

// StrongSwanSettings goes in /etc/strongswan.d. Circuits are route-based: FRR
// routes over the XFRM interfaces, so charon must not add its own routes for
// the 0.0.0.0/0 traffic selectors (they would capture everything).
const StrongSwanSettings = `# Rendered by exa-agent. Do not edit.
charon {
  install_routes = no
  install_virtual_ip = no
}
`

// Swanctl renders one circuit's swanctl.conf fragment: an IKEv2 connection
// authenticated with a pre-shared key, route-based through the XFRM interface
// with the circuit's if_id, and the key itself. The key is written base64
// encoded (strongSwan's 0s prefix), so no character in it needs quoting.
func Swanctl(c desired.Circuit) string {
	var b strings.Builder
	conn := c.Conn()
	fmt.Fprintf(&b, "# Rendered by exa-agent for %s. Do not edit.\n", c)
	b.WriteString("connections {\n")
	fmt.Fprintf(&b, "  %s {\n", conn)
	b.WriteString("    version = 2\n")
	fmt.Fprintf(&b, "    local_addrs = %s\n", c.LocalAddress)
	fmt.Fprintf(&b, "    remote_addrs = %s\n", c.RemoteAddress)
	fmt.Fprintf(&b, "    proposals = %s\n", c.IKEProposals)
	b.WriteString("    mobike = no\n    dpd_delay = 10s\n")
	fmt.Fprintf(&b, "    local {\n      auth = psk\n      id = %s\n    }\n", c.LocalAddress)
	fmt.Fprintf(&b, "    remote {\n      auth = psk\n      id = %s\n    }\n", c.RemoteAddress)
	b.WriteString("    children {\n")
	fmt.Fprintf(&b, "      %s {\n", conn)
	b.WriteString("        local_ts = 0.0.0.0/0\n        remote_ts = 0.0.0.0/0\n")
	fmt.Fprintf(&b, "        esp_proposals = %s\n", c.ESPProposals)
	fmt.Fprintf(&b, "        if_id_in = %d\n        if_id_out = %d\n", c.IfID, c.IfID)
	b.WriteString("        start_action = start\n        dpd_action = restart\n        close_action = start\n")
	b.WriteString("      }\n    }\n  }\n}\n")
	b.WriteString("secrets {\n")
	fmt.Fprintf(&b, "  ike-%s {\n", conn)
	fmt.Fprintf(&b, "    id-local = %s\n    id-remote = %s\n", c.LocalAddress, c.RemoteAddress)
	fmt.Fprintf(&b, "    secret = 0s%s\n", base64.StdEncoding.EncodeToString([]byte(c.PSK)))
	b.WriteString("  }\n}\n")
	return b.String()
}
