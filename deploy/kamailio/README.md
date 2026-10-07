# Kamailio: the voice edge proxy

ADR 0033. Kamailio stands in front of FreeSWITCH. Carriers reach Kamailio only;
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

## Outside calls: the controller decides, Kamailio carries

Before every outside call except an emergency call, FreeSWITCH's dial plan asks the controller
(`GET /api/v1/commai/internal/voice/authorise`, with mod_curl; `controller/.../voice/pbx.py`). The
controller runs the same checks as simulated calls: blocked prefixes, international on or off, the
daily spend cap, revenue share fraud rules and the emergency address rule for the calling number.
It answers in plain text:

- `OK 2,1 +18685550101`: go ahead. `2,1` is the carrier order as dispatcher set ids, which the dial
  plan puts in `X-Exa-Route` (`none`: the single provider, no header). The number is the caller id
  to show (`none`: unchanged).
- `NO <code>`, for example `NO daily_cap`: refused. The caller hears "cannot be completed as dialled"
  and the call ends with `CALL_REJECTED`. The refusal is kept as a blocked call.

**Fail closed.** If the controller cannot be reached, answers anything else or refuses the request,
the call is refused (`SERVICE_UNAVAILABLE`). Only emergency calls never ask: they bridge at once,
with `X-Exa-Route` listing every carrier switched on for the business when there are any.

The request is signed: `X-Exa-Pbx-Auth` is a digest of its fields with `EXA_PBX_SECRET`, which the
controller and FreeSWITCH both read from `.env` (`lab/scripts/init-env.sh` generates it). It is a
digest rather than the secret because mod_curl writes request headers to FreeSWITCH's debug log.
With no secret, every outside call is refused. The path is not served by the public proxy
(`deploy/public/Caddyfile`). Forwarding to an outside number goes back through the same check.

## Provider or Kamailio: `EXA_VOICE_EDGE`

FreeSWITCH sends outside calls to the gateway `exacarib_sip`. `make voice-up` installs it in
`deploy/freeswitch/sip_profiles/external/exacarib_sip.xml` (git-ignored, mounted into FreeSWITCH)
from the example for the edge set in `.env`:

| `EXA_VOICE_EDGE` | Gateway | Use |
| --- | --- | --- |
| `provider` (default) | `exacarib_sip.xml.example`: registers to `EXA_SIP_REALM` with `EXA_SIP_USERNAME` / `EXA_SIP_PASSWORD`. Installed once `EXA_SIP_USERNAME` is set. | One SIP provider (stage 4). Carriers in the controller are ignored on the wire. |
| `kamailio` | `exacarib_sip.kamailio.xml.example`: `kamailio:5060`, no registration. | Several carriers (ADR 0033). Kamailio holds the carrier list and follows `X-Exa-Route`. |

With `kamailio`, `make voice-up` also writes `EXA_KAMAILIO_PBX_NETS` to `.env` (the compose network
FreeSWITCH and Kamailio share) and restarts the controller once, so the rendered `address.list` lets
FreeSWITCH in (group 2). Switching back to `provider` removes the Kamailio gateway file.

## Files

| File | From |
| --- | --- |
| `kamailio.cfg`, `tls.cfg` | This folder (in git). |
| `kamailio-local.cfg` | Copy from `kamailio-local.cfg.example` on the host: public IP, FreeSWITCH address, call cap. Git-ignored. |
| `dispatcher.list`, `address.list` | Written by the controller from the carriers to `EXA_KAMAILIO_DIR` (`/data/kamailio` on the shared volume, `/exacarib/kamailio` inside Kamailio) each time `GET /api/v1/commai/voice-admin/kamailio` runs. |
| `tls/server.crt`, `tls/server.key` | The voice host's certificate, mounted from the host. Never committed. |

Start it with `make voice-up` (`deploy/voice/up.sh`). That writes `kamailio-local.cfg` from the
example if it is missing (advertising `EXA_VOICE_PUBLIC_IP` from `.env`, or 127.0.0.1), makes a
stand-in certificate in `tls/` until the real one is put there, renders the carrier lists and
starts Kamailio and FreeSWITCH. No SIP or media port is published, so nothing outside the host can
reach them. It is not started by the lab runner; it is started by hand once agreed.

`make voice-check` (`deploy/voice/check.sh`, run in CI) parses this config with the pinned image,
starts Kamailio with empty carrier lists, checks that it answers 403 to a caller that is not on the
allow-list, and checks that FreeSWITCH starts with ExaCarib's files and both SIP profiles run.
`lab/ci/checks/voice-sbc.sh` runs the same checks on a host where the SBC is started.

FreeSWITCH runs with three hardened stock files from `deploy/freeswitch/`: `vars.xml` (no STUN
lookup; the external address is `EXA_VOICE_PUBLIC_IP`, else the container address; `EXA_PBX_SECRET`
as the global `exa_pbx_secret`; values from the environment are read with `exec-set` and `echo`,
because the image's `env-set` does not read the environment and it has no `printenv`),
`event_socket.conf.xml` (loopback only) and `modules.conf.xml` (no `mod_signalwire`; `mod_curl` on
for the check above). The image's demo users 1000 to 1019 are hidden by an empty mount.
`make voice-check` also checks that mod_curl is loaded, that the secret reaches FreeSWITCH and that
the Kamailio gateway loads.

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

The image tag is pinned in `deploy/docker-compose.yml`. Check
that the tag has an arm64 build.
