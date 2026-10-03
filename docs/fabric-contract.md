# Virtual circuits: controller, agent and portal contract (ADR 0009)

Step 2 of ExaConnect Fabric. The controller owns circuits; the PoP agent
terminates cloud circuits; site agents carry layer 2 circuits.

## Desired state (controller to agent)

Every node:

```json
"loopback": "10.254.0.11/32"
```

The agent puts it on `lo` and advertises it in BGP (a `network` statement),
so every site can reach every other site's loopback through the PoP.

PoP only, when the PoP has `cloud_interface` and `cloud_address` set and the
customer has enabled cloud circuits (disabled and deleted circuits are left
out; anything not listed is torn down):

```json
"circuits": [{
  "id": 7,
  "name": "vc7",                       // xfrm interface name
  "if_id": 7,                          // XFRM if_id, also the swanctl connection name suffix
  "underlay_interface": "eth5",        // where IKE/ESP leave
  "local_address": "100.64.0.2",       // our public address on that interface
  "remote_address": "100.64.10.2",     // the cloud VPN gateway
  "psk": "...",                        // IKEv2 pre-shared key; write it 0600, never log it
  "ike_proposals": "aes256-sha256-modp2048",
  "esp_proposals": "aes256-sha256-modp2048",
  "inside_address": "169.254.100.2/30",// ours, on the xfrm interface
  "peer_inside": "169.254.100.1",      // the gateway's BGP address
  "peer_asn": 64512,
  "import_prefixes": ["10.100.0.0/16"],// accept only these (and longer, le 32); empty = accept any
  "max_prefixes": 100,
  "export_prefixes": ["192.168.10.0/24"],
  "export_circuits": [8],              // also export what we accept from these circuits (cloud router)
  "shape_kbit": 50000                  // egress shaping on the xfrm interface
}]
```

Sites only:

```json
"l2_circuits": [{
  "id": 9,
  "name": "vx9",                       // VXLAN interface; the bridge is br9, the VLAN subinterface <parent>.<vlan>
  "vni": 10009,
  "vlan": 100,
  "parent": "eth4",
  "remote": "10.254.0.12",             // the other end's loopback; local is this node's loopback
  "shape_kbit": 20000,
  "mtu": 1370,
  "probe": {"target": "10.254.0.12:7000", "interval_ms": 1000}
}],
"reflector": {"listen": "10.254.0.11:7000"}   // sites with l2 circuits reflect probes on their loopback
```

## Telemetry (agent to controller), every flush

```json
"circuits": [{
  "id": 7,
  "name": "vc7",
  "ike": "up",            // up | connecting | down | "" (l2)
  "bgp": "Established",   // FRR's state string, "" for l2
  "prefixes_received": 1,
  "routes": ["10.100.0.0/16"],   // up to 20 accepted prefixes
  "sent": 10, "received": 10, "rtt_ms": 3.2,   // probes since the last flush (l2); 0, 0, null for cloud
  "bytes_in": 123, "bytes_out": 456            // the interface's cumulative counters
}]
```

## API (portal to controller), under /api/v1

Customers see their own; admins any; carrier users none. The pre-shared key
is write-only: never returned, logged or audited (`has_psk` instead).

- `GET /circuits/providers`: `{aws: {name, asn, where}, azure, gcp, oracle, other}`
- `GET /customers/{cid}/circuits`: list of circuits, each:
  `{id, name, kind, a_site_id, a_site, b_site_id, b_site, a_vlan, b_vlan, provider, region,
   peer_address, peer_asn, inside_cidr, our_inside, cloud_inside, cloud_prefixes, a_prefixes,
   class_name, bandwidth_mbps, price_per_mbps_month, enabled, has_psk, status, ike, bgp,
   prefixes_received, routes, rtt_ms, loss_pct, mbps_in, mbps_out, month_to_date,
   created_by, created_at, updated_at}`
  where `status` is `provisioning | up | down | off`.
- `POST /customers/{cid}/circuits` with
  `{name, kind: "cloud"|"site", bandwidth_mbps, enabled?, a_site_id?, a_prefixes?, a_vlan?,
   b_site_id?, b_vlan?, provider?, region?, peer_address?, peer_asn?, inside_cidr?, psk?,
   cloud_prefixes?, class_name?}`: 201 with the circuit; 400 with a plain-English `detail`.
- `PATCH /customers/{cid}/circuits/{id}`: any of the same fields (psk to rotate the key);
  a bandwidth change takes effect within 10 s and is billed from then.
- `DELETE /customers/{cid}/circuits/{id}`: 204.
- `GET /customers/{cid}/circuits/{id}/charges?month=YYYY-MM`:
  `{price_per_mbps_month, segments: [{mbps, from, to, hours, amount}], total}`.
- `GET /customers/{cid}/circuits/{id}/metrics?minutes=60`:
  `[{time, sent, received, rtt_ms, mbps_in, mbps_out}]` per minute.
- `PATCH /customers/{cid}/settings` accepts `{cloud_to_cloud: bool}`; `GET` returns it.
- `GET /admin/inventory/...` and the site form carry `cloud_interface`, `cloud_address` (PoP)
  and `lan_interface`.
