# Internet breakout, NAT gateway and firewall: contract (ADR 0010)

Step 3 of ExaConnect Fabric. Each site sends its internet traffic one of
three ways, set per site:

- `pop` (the default): through the overlay to the PoP, which is the
  customer's NAT gateway and firewall.
- `local`: straight out of the site's own carrier links, NAT and firewall
  on the site's agent, failing over between links.
- `off`: no internet from the site's LAN.

"Internet traffic" is traffic from a site's LAN to anything that is not
the customer's own network (other sites' LANs, loopbacks, overlay, cloud
circuit prefixes). The host's own traffic (agent to controller, WireGuard,
IKE) is never touched: everything below matches on source = LAN prefixes.

## Desired state (controller to agent)

Every node gets an `internet` block (absent = remove everything below):

```json
"internet": {
  "mode": "pop",                         // site: pop | local | off; PoP: "gateway"
  "lan_prefixes": ["192.168.10.0/24"],   // sources this applies to. Site: its LAN. PoP: the LANs
                                         // of every pop-mode site, plus its own LAN
  "tunnels": ["wg-a", "wg-b", "wg-sat"], // site, pop mode: tunnels to the PoP in preference order
  "uplinks": [                           // site, local mode: its carrier links in preference order.
    {"interface": "eth1", "gateway": "10.11.1.1", "path": "carrier-a", "tunnel": "wg-a"}
  ],                                     // PoP: one uplink, its internet interface (path/tunnel "")
  "public_address": "100.64.0.2",        // PoP only: the address port forwards listen on
  "firewall": [                          // local-mode site and PoP only, in order; first match wins
    {"id": 12, "action": "deny", "src": ["192.168.10.0/24"], "dst": ["198.51.100.0/24"],
     "protocol": "icmp", "ports": ""}    // protocol any | tcp | udp | icmp; ports as in traffic rules
  ],                                     // ("443", "8000-8100,8443"), only with tcp or udp
  "port_forwards": [                     // PoP only
    {"id": 3, "protocol": "tcp", "port": 8080, "to_address": "192.168.10.10", "to_port": 8080,
     "allow_from": ["0.0.0.0/0"]}
  ]
}
```

### What the agent does

Routing (all nodes, table 251, rules after the steering rules at 1000+):

```
ip rule add pref 31000 from <each lan prefix> lookup main suppress_prefixlength 0
ip rule add pref 31001 from <each lan prefix> lookup 251
```

so the customer's own destinations still follow BGP and steering, and only
what would otherwise use a default route goes to table 251, which holds:

- site, `pop`: `default dev <tunnel>`, the first tunnel in `tunnels` whose
  BFD session is up (the first one if none is up). Re-chosen on every BFD
  change, like local failover.
- site, `local`: `default via <gateway> dev <interface>`, the first uplink
  whose `tunnel` has BFD up (BFD down on a tunnel means that carrier is
  failing), else the first. Re-chosen on every BFD change.
- site, `off`: `unreachable default`.
- PoP: `default via <gateway> dev <interface>` of its uplink.

Firewall and NAT, one nftables table `ip exa_inet`, replaced atomically
(`nft -f`), only on a local-mode site and the PoP:

```
table ip exa_inet {
  set lan { type ipv4_addr; flags interval; elements = { <lan_prefixes> } }
  chain pre {                                    # PoP only, port forwards
    type nat hook prerouting priority dstnat;
    iifname "<uplink>" ip daddr <public_address> tcp dport 8080 dnat to 192.168.10.10:8080
  }
  chain post {
    type nat hook postrouting priority srcnat;
    oifname { <uplinks> } ip saddr @lan masquerade
  }
  chain filter_fwd {
    type filter hook forward priority filter; policy accept;
    ct state established,related accept
    # inbound: only port forwards, from allowed sources
    iifname { <uplinks> } ct status dnat ip daddr 192.168.10.10 tcp dport 8080 ip saddr { <allow_from> } counter accept comment "pf3"
    iifname { <uplinks> } counter drop comment "inbound"
    # outbound: the customer's rules, first match wins, then allow
    oifname { <uplinks> } ip saddr @lan ip saddr { <src> } ip daddr { <dst> } meta l4proto icmp counter drop comment "fw12"
  }
}
```

An empty `src` or `dst` means any; `allow_from` empty means any. The
controller only lists a port forward while its site is in `pop` mode (the
reply has to come back through the PoP to be translated). Only
traffic to or from the uplinks is filtered; overlay traffic is untouched.
A pop-mode or off site has no `exa_inet` table (delete it if present).

Steering (site, `local` mode): the steering map carries
`"corporate_prefixes": [...]` (see below); the classifier then returns
early for any destination outside them, so internet traffic is not marked
and is not pulled into a tunnel by a class's path table.

### Telemetry (agent to controller), every flush

```json
"internet": {
  "mode": "local",
  "via": "eth1",                         // the interface or tunnel table 251 points at; "" if none
  "counters": [                          // cumulative, from nft's counters
    {"kind": "rule", "id": 12, "packets": 40, "bytes": 3360},
    {"kind": "forward", "id": 3, "packets": 7, "bytes": 420},
    {"kind": "inbound", "id": 0, "packets": 2, "bytes": 120}
  ]
}
```

## Steering map (controller to agent)

A local-mode site's map adds:

```json
"corporate_prefixes": ["192.168.10.0/24", "192.168.20.0/24", "10.200.0.0/24",
                       "10.254.0.0/24", "100.64.0.0/16", "10.100.0.0/16"]
```

the customer's site LANs, the loopback range, the overlay, and the cloud
prefixes of its circuits. Absent = classify everything, as today.

## API (portal to controller), under /api/v1

Customers manage their own; admins any; carrier users none. Every write is
audited and refreshes desired state.

- `GET /customers/{cid}/internet`:
  ```json
  {"public_address": "100.64.0.2",
   "sites": [{"id", "name", "mode", "via", "via_label", "uplinks": ["Carrier A", ...], "updated_at"}],
   "rules": [{"id", "position", "site_id", "site", "action", "src", "dst", "protocol", "ports",
              "description", "enabled", "packets", "bytes"}],
   "forwards": [{"id", "description", "protocol", "port", "to_site_id", "to_site", "to_address",
                 "to_port", "allow_from", "enabled", "packets", "bytes"}],
   "inbound_dropped": 2}
  ```
  `via_label` is "Carrier A" for a local uplink, "ExaCarib PoP over Carrier A" for a tunnel.
- `PATCH /customers/{cid}/internet/sites/{site_id}` `{mode: "pop"|"local"|"off"}`.
- `POST /customers/{cid}/firewall/rules` `{site_id?, action: "allow"|"deny", src?, dst?, protocol?,
  ports?, description?, enabled?, position?}` (appended when no position) → 201 with the rule.
  `PATCH` and `DELETE /customers/{cid}/firewall/rules/{id}`.
  `POST /customers/{cid}/firewall/order` `{ids: [..]}` sets the order (all of the customer's rule ids).
- `POST /customers/{cid}/port-forwards` `{description?, protocol: "tcp"|"udp", port, to_site_id,
  to_address, to_port?, allow_from?, enabled?}` → 201. The public port is unique on the PoP
  across all customers (it shares one public address); `to_address` must be inside the site's LAN.
  `PATCH` and `DELETE /customers/{cid}/port-forwards/{id}`.
- Admin site form: `internet_interface` and `internet_gateway` (PoP); link form `underlay_gateway`.
- 400 with a plain-English `detail` on bad input.
