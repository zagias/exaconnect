# Desired state

The controller builds one desired-state document per node from inventory
(`controller/exaconnect_controller/desired.py`). The agent applies it
(`agent/internal/apply`). This page is the contract between the two.

## Versioning

* Every change to inventory that affects a node (a site, a link, an enrolment
  with a new WireGuard key) recomputes the documents for that customer.
* A new version is stored only when the document's content changes, so
  re-running the seed does not churn versions.
* Agents poll `GET /api/v1/agent/desired-state?have=<version>` every 10 s.
  The controller answers 204 when `have` is current, otherwise the document.
* After applying, the agent posts `POST /api/v1/agent/status` with the version
  and whether it worked. The Admin screen shows applied against desired.

## Document (schema 1)

```json
{
  "schema": 1,
  "version": 4,
  "node_id": "…", "node_name": "site-a", "customer_id": "…",
  "role": "site",                       // site | pop
  "asn": 65001,
  "router_id": "100.64.1.11",
  "lan_prefixes": ["192.168.10.0/24"],
  "bfd_profiles": [
    {"name": "terrestrial", "tx_ms": 200, "rx_ms": 200, "multiplier": 3},
    {"name": "satellite", "tx_ms": 1000, "rx_ms": 1000, "multiplier": 3}
  ],
  "tunnels": [{
    "name": "wg-a", "path": "carrier-a", "underlay_interface": "eth1",
    "address": "100.64.1.11/24", "listen_port": 0, "mtu": 1420,
    "peers": [{"name": "pop-miami", "public_key": "…", "endpoint": "10.11.0.2:51820",
               "allowed_ips": ["0.0.0.0/0"], "keepalive": 10}],
    "bgp_neighbors": [{"address": "100.64.1.1", "asn": 65000,
                       "bfd_profile": "terrestrial", "local_pref": 200}],
    "probe": {"target": "100.64.1.1:7000", "interval_ms": 1000}
  }],
  "reflector": null                     // the PoP has {"listen": ":7000"}
}
```

On the PoP each tunnel has a `listen_port`, one peer per site (allowed IPs are
the site's overlay /32 and its LAN prefixes) and one BGP neighbour per site.

Private keys never appear here. The agent generated its WireGuard key at
enrolment and fills it in locally when it renders the WireGuard config.

## How the agent applies it

1. Validate (interface names, addresses, keys, ports).
2. For each tunnel: write `/var/lib/exaconnect/wg/<name>.conf`, create the
   interface if missing, `wg syncconf`, `ip address replace`, set MTU and up.
3. Delete `wg-*` interfaces that are no longer in the document.
4. Render `frr.conf` (BFD profiles, eBGP per tunnel with BFD, local-pref
   route-maps, LAN networks) and reload FRR with `frr-reload.py`, falling back
   to `vtysh -f`.
5. On success save the document as `last-good.json`. On any failure re-apply
   `last-good.json` and report the error with the version that failed.

At start the agent re-applies `last-good.json` before it contacts the
controller, so a rebooted node forwards without the controller.

## Telemetry back

`POST /api/v1/agent/telemetry` every 10 s carries probe windows (sent,
received, loss, RTT avg/min/max, RFC 3550 jitter per path), interface byte
counters every 60 s, events (`config_applied`, `config_failed`,
`config_rolled_back`, `bfd_up`, `bfd_down`, `controller_silent`,
`controller_back`) and per-tunnel state (handshake age, BFD). While the
controller is unreachable the agent buffers up to 2000 items and sends them
when it is back.
