# Kamailio: the voice edge proxy

ADR 0027. Kamailio stands in front of FreeSWITCH. Carriers reach Kamailio only;
FreeSWITCH has no public SIP port.

- **Allow-list**: only carrier signalling addresses (`address.list` group 1) and
  FreeSWITCH (group 2) may send requests. Everything else gets 403.
- **Rate limits**: `pike` blocks a source that sends more than 30 requests in
  2 seconds, for 5 minutes. A counter caps new calls per source address per minute
  (`MAX_INVITES_PER_MIN`).
- **TLS** on 5061 (`tls.cfg`), with UDP and TCP on 5060 for carriers without TLS.
- **Carriers and failover**: there is one dispatcher set per carrier.
  FreeSWITCH sends the controller's carrier order in `X-Exa-Route` (set ids).
  When a carrier refuses or times out, `failure_route` tries the next one.
  Busy and wrong-number answers go back to the caller.
- **Trunk health**: the dispatcher sends SIP OPTIONS every 30 s. Three failures
  mark a destination inactive, and one answer brings it back.

## Files

| File | From |
| --- | --- |
| `kamailio.cfg`, `tls.cfg` | This folder (in git). |
| `kamailio-local.cfg` | Copy from `kamailio-local.cfg.example` on the host: public IP, FreeSWITCH address, call cap. Git-ignored. |
| `dispatcher.list`, `address.list` | Written by the controller from the carriers to `EXA_KAMAILIO_DIR` (`/data/kamailio` on the shared volume, `/exacarib/kamailio` inside Kamailio) each time `GET /api/v1/commai/voice-admin/kamailio` runs. |
| `tls/server.crt`, `tls/server.key` | The voice host's certificate, mounted from the host. Never committed. |

Run it with `docker compose --profile voice up -d kamailio`. It is not started by default.

## What Dudley must provide

1. **A SIP provider (carrier) account** for each carrier you add. You need its
   signalling host and port, its transport (TLS if offered), the IP addresses
   it sends from (for the allow-list), and its rate sheet. Credentials go in the
   environment only (`EXA_SIP_USERNAME`, `EXA_SIP_PASSWORD`, and
   `EXA_SIP_PROVIDER_URL`, `EXA_SIP_PROVIDER_KEY`, `EXA_SIP_PROVIDER_ACCOUNT` for
   its API), never in this repo.
2. **A voice host with a public IP**, with these ports open to the carriers'
   addresses only:
   - 5060/udp and 5060/tcp: SIP, if a carrier has no TLS.
   - 5061/tcp: SIP over TLS.
   - 16384–32768/udp: RTP media to FreeSWITCH. The other builder's FreeSWITCH
     profile sets the range; open the range it uses.
   - LiveKit, if AI calls are on: 7880/tcp (API, behind the proxy), 7881/tcp and
     50000–60000/udp (WebRTC media), 5062/udp and 5062/tcp (LiveKit SIP, from
     FreeSWITCH only, not public).
3. **A TLS certificate** for the voice host name (Let's Encrypt is fine).
4. **Its go-live record**: each carrier stays off in the go-live registry until
   its written criteria are met (contract, interconnect tested, rates checked,
   emergency calls tested).

Not run here: check it with `kamailio -c -f /etc/kamailio/kamailio.cfg` on the host
before first use. The image tag is pinned in `deploy/docker-compose.yml`. Check
that the tag has an arm64 build.
