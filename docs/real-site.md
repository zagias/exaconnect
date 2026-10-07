# Connecting a real site

The agent gateway (ADR 0024) lets a Linux box anywhere enrol with the controller at
`https://connect.exacarib.com:8443`. The site needs outbound HTTPS to that port; nothing
inbound.

1. **Site kit.** Download `exa-agent-linux-arm64` or `-amd64` from the latest CI run
   (Actions, the run, Artifacts), or run `make build-agent` in a checkout. It holds
   `exa-agent`, `install.sh` and `exa-agent.service`.
2. **Site in the portal.** Admin > Sites: add the site with its LAN prefixes and one
   link per underlay (carrier, type, commit). Then "Enrolment token". The token shows
   once, with the exact install command.
3. **Install**, as root on the box (Debian 12 or Ubuntu 22.04/24.04):

   ```
   EXA_ENROL_TOKEN=<token> bash install.sh --controller https://connect.exacarib.com:8443 \
     --ca-fingerprint <fingerprint> --name <site name>
   ```

   It installs FRR, WireGuard and nftables, enrols (the WireGuard private key never
   leaves the box), and starts `exa-agent` under systemd.
4. **Check.** The node appears under Admin > Agents with its applied version, and the
   site turns green once its tunnels to the PoP are up.

## Tunnels need a reachable PoP

Sites build one WireGuard tunnel per link to the PoP's address for that path. The
lab PoP lives inside the lab, so a real site enrols, reports and receives its
desired state, but its tunnels come up only against a PoP with a public address:
a site of kind `pop` on a cloud VM, installed with the same kit, whose links carry
its public IP and whose WireGuard ports (one UDP port per path, shown in the
desired state) are open. That is the Phase B test in CLAUDE.md, and the second
server it needs is Dudley's call.

## Removing a site

In Admin > Agents, "Revoke certificate" stops the agent at once (or delete the
whole site in Admin > Sites and links), then run `systemctl disable --now exa-agent`
on the box. Forwarding keeps running on the last
state until the box is cleaned up: `wg-quick` interfaces `wg-*`, the `exaconnect`
nftables table and the FRR config.
