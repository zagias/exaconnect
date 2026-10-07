# ADR 0024: The agent gateway on the internet

Date: 2026-10-07 · Status: accepted

## Context

ADR 0003 put agent mTLS on an nginx proxy at 8443, reachable only on the lab's
management network. No real site could reach the controller, so nothing outside
the lab could enrol, poll desired state or report telemetry. The public portal
(Caddy on 443) deliberately refuses every agent endpoint.

## Options

1. **Publish the existing proxy on the host's 8443** (chosen). The mTLS path is
   the one the lab agents already prove on every run; one more port.
2. Route agents through Caddy on 443 with client-certificate auth. One port, but
   Caddy's certificate comes from Let's Encrypt, so agents would trust public CAs
   as well as the pinned controller CA, and browsers on the portal would be asked
   for client certificates.
3. A separate host name for agents. Needs a DNS record and changes nothing else.

## Decision

* The proxy publishes `8443:8443` on the host. `make public-up` opens 8443/tcp in
  ufw with 80 and 443. The controller and database stay private.
* `EXA_PUBLIC_HOST` (deploy/public/site.env) becomes `EXA_AGENT_PUBLIC_HOST` in the
  controller. That name joins the server certificate, which is reissued by the same
  CA when a name is missing, so enrolled agents keep working with no change.
* Enrolment tokens show the public URL (`https://<host>:8443`) and a ready
  install command. Agents pin the CA by fingerprint, as before.
* The gateway answers only `/api/v1/ca.pem`, `/api/v1/enrol` (rate limited per
  address, 10 a minute with a small burst) and `/api/v1/agent/*` (client
  certificate required). Everything else is 404.
* A site kit (binary, `deploy/agent/install.sh`, `exa-agent.service`) is built for
  arm64 and amd64 on every CI run. The installer sets up FRR, WireGuard and
  nftables, enrols with the token from the environment and enables the service.

## Consequences

* Sites need outbound TCP 8443 only. Tunnels need the PoP's WireGuard ports
  (one UDP port per path) reachable; a real PoP is a site of kind `pop` whose link
  `underlay_ip` is its public address, installed with the same kit.
* Port 8443 is open on the lab host. Lab check `m9-gateway.sh` proves the
  certificate covers the public name, a bad token is refused, agent endpoints
  need a certificate, the portal API is absent and the rate limit holds.
* Revocation stays "delete or re-enrol the node" (ADR 0003).
