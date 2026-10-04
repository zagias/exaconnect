# Resilient circuits, DDoS protection and encryption reporting: contract (ADR 0012)

Step 5 of ExaConnect Fabric.

- **Resilient circuits**: a cloud circuit can have a second tunnel to a
  second gateway address (AWS and Azure both give two). Both carry BGP;
  traffic moves to the other tunnel when one fails.
- **DDoS protection** on the PoP's public address: per-source limits on new
  inbound connections with automatic blocking, a SYN flood limit, and a
  block list kept by ExaCarib.
- **Encryption report**: which traffic is encrypted, how, and since when.

## 1. Resilient circuits

### Controller

New optional circuit fields: `secondary_peer_address` (IPv4) and
`secondary_inside_cidr` (a /30, allocated like `inside_cidr` when not
given). The pre-shared key is shared by both tunnels. A circuit with a
`secondary_peer_address` is *resilient*.

### Desired state (controller to PoP agent)

No new agent fields. The second tunnel is a second entry in `circuits`:

```json
{"id": 1000007, "name": "vc1000007", "if_id": 1000007,
 "remote_address": "<secondary_peer_address>",
 "inside_address": "<ours in secondary_inside_cidr>", "peer_inside": "<theirs>",
 ...everything else as the primary (psk, proposals, prefixes, asn, shape_kbit)...}
```

- `id` = 1,000,000 + the circuit id; the name is `vc<that id>`.
- Neither tunnel lists its sibling in `export_circuits`. Other circuits
  that export a resilient circuit's routes list both ids.
- BGP picks between the two; both learn the same prefixes, so failover is
  the BGP hold time (30 s, as AWS and Azure use) or IPsec dead-peer detection, whichever is first.

### Telemetry

Unchanged. A circuit entry with `id` >= 1,000,000 reports the secondary
tunnel of circuit `id - 1,000,000`.

### API

`GET /customers/{cid}/circuits` items add:

```json
"resilient": true,
"secondary_peer_address": "100.64.10.3", "secondary_inside_cidr": "169.254.100.4/30",
"tunnels": [
  {"which": "primary",   "peer_address": "100.64.10.2", "ike": "up", "bgp": "Established",
   "prefixes_received": 1, "status": "up"},
  {"which": "secondary", "peer_address": "100.64.10.3", "ike": "up", "bgp": "Established",
   "prefixes_received": 1, "status": "up"}
]
```

`tunnels` has one entry for a circuit that is not resilient. The circuit's
`status` is `up` when any tunnel is up. POST and PATCH accept the two new
fields; `secondary_peer_address: null` in a PATCH removes the second
tunnel. The second address must differ from the first.

## 2. DDoS protection at the PoP

### Controller

PoP settings (admin, on the PoP's site record):

| field | default | meaning |
| --- | --- | --- |
| `ddos_enabled` | true | protection on the public address |
| `ddos_new_per_source` | 50 | new inbound connections per second from one source before it is blocked |
| `ddos_syn_per_s` | 2000 | new TCP connections per second to the public address, all sources together |
| `ddos_block_minutes` | 10 | how long an automatically blocked source stays blocked |

Block list (admin only; it affects every customer on the PoP's shared
address): `blocked_sources (id, prefix cidr, reason, created_by,
created_at, expires_at null = until removed)`.

### Desired state (controller to PoP agent)

The PoP's `internet` block adds:

```json
"protection": {
  "enabled": true,
  "new_per_source": 50,
  "syn_per_s": 2000,
  "block_minutes": 10,
  "blocklist": ["203.0.113.66/32", "198.51.100.128/25"]
}
```

Absent or `enabled: false`: no protection chain.

### What the agent does

In table `ip exa_inet`, before NAT, a filter chain on the PoP's uplink for
traffic to `public_address`:

```
set blocklist { type ipv4_addr; flags interval; elements = { <blocklist> } }
set auto_block { type ipv4_addr; flags dynamic, timeout; timeout <block_minutes>m; size 65536; }
set rate { type ipv4_addr; flags dynamic, timeout; timeout 1m; size 65536; }
chain guard {
  type filter hook prerouting priority -150; policy accept;
  iifname "<uplink>" ip daddr <public_address> ip saddr @blocklist counter drop comment "blocked"
  iifname "<uplink>" ip daddr <public_address> ip saddr @auto_block counter drop comment "auto"
  iifname "<uplink>" ip daddr <public_address> ct state new update @rate { ip saddr limit rate over <new_per_source>/second burst <2 × new_per_source> packets } add @auto_block { ip saddr } counter drop comment "flood"
  iifname "<uplink>" ip daddr <public_address> tcp flags & (syn|ack) == syn limit rate over <syn_per_s>/second burst <syn_per_s> packets counter drop comment "syn"
}
```

Only new inbound traffic to the public address is affected: replies to
outbound NAT are `ct state established` and never reach the limits, and
customers' outbound traffic is untouched.

### Telemetry

The PoP's `internet` telemetry adds counter kinds `blocked`, `auto`,
`flood` and `syn` (id 0, cumulative), and:

```json
"auto_blocked": [{"address": "203.0.113.9", "expires_s": 540}]   // up to 100, from @auto_block
```

### API

- `GET /customers/{cid}/internet` adds
  `"protection": {"enabled", "new_per_source", "syn_per_s", "block_minutes",
   "dropped": {"blocked", "auto", "flood", "syn"} (packets), "auto_blocked": n, "updated_at"}`.
  Customers see counts only, never other customers' traffic.
- `GET /admin/protection` → `{settings, dropped, auto_blocked: [{address, expires_s}], blocklist: [...]}`.
- `PATCH /admin/protection` `{enabled?, new_per_source? (1 to 100000), syn_per_s? (10 to 1000000),
  block_minutes? (1 to 1440)}`.
- `POST /admin/protection/blocklist` `{prefix, reason?, hours? (null = until removed)}` → 201;
  `DELETE /admin/protection/blocklist/{id}`. Expired entries drop out of desired state.

RTBH (asking upstream carriers to drop traffic to an attacked address) needs
BGP with a carrier that honours the BLACKHOLE community; it is a seam for
later (ADR 0012), not built.

## 3. Encryption report

### Telemetry (PoP agent)

Circuit telemetry adds, for cloud circuits (from `swanctl --list-sas`):

```json
"ike_cipher": "AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048",
"esp_cipher": "AES_CBC-256/HMAC_SHA2_256_128",
"established_s": 120          // seconds since the IKE SA was established; -1 unknown
```

Empty strings when there is no SA. WireGuard needs nothing new: tunnels
report `handshake_age_s` today.

### API

`GET /customers/{cid}/encryption`:

```json
{"summary": {"encrypted": 7, "total": 7},
 "paths": [{"site": "site-a", "path": "carrier-a", "label": "Carrier A", "protocol": "WireGuard",
            "cipher": "ChaCha20-Poly1305, Curve25519 key exchange",
            "handshake_age_s": 45, "status": "encrypted" | "idle" | "down"}],
 "circuits": [{"id": 7, "name": "AWS us-east-1", "kind": "cloud", "tunnel": "primary",
               "protocol": "IPsec (IKEv2)", "ike_cipher": "...", "esp_cipher": "...",
               "established_s": 120, "status": "encrypted" | "down",
               "notes": []}],      // e.g. "Uses SHA-1" or "Weak key exchange" if negotiated
 "layer2": [{"id": 9, "name": "...", "protocol": "VXLAN inside WireGuard", "status": "encrypted"}],
 "control": {"protocol": "TLS with client certificates (mutual TLS)"},
 "internet": "Internet traffic is encrypted between your sites and the PoP. Beyond the PoP it travels as your applications send it."}
```

- A path is `encrypted` while its handshake is under 3 minutes old, `idle`
  when older but BFD is up (WireGuard only re-keys when there is traffic),
  `down` otherwise.
- A circuit tunnel is `encrypted` when IKE is up, else `down`; `notes`
  flag SHA-1, MD5, DES/3DES or a DH group below 2048-bit MODP.
- `summary` counts paths and circuit tunnels.
