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

## Whitebox appliances: the box console

The easiest way is the ExaCarib box image (ADR 0047): download
`exacarib-box-<version>-amd64.img.xz` from the repository's Releases (tags `box-v…`),
write it to a USB stick or the box's disk with balenaEtcher, and boot the box from it.
Plug the internet link into the first port (eth0) and a laptop into the last port, and
open https://192.168.50.1. The box's screen shows the setup code. A box that already
runs Debian 12 or Ubuntu 22.04+ takes the package instead:
`sudo apt install ./exacarib-box_<version>_amd64.deb` (or `_arm64.deb`).

Without either, for a box set up by someone at the branch rather than over a root
shell (ADR 0046), install it once with `sudo bash install.sh --box`. It prints a setup code (keep it
with the box, or on its label). At the branch:

1. Plug the internet links and the office network into the box.
2. From a computer on the office network, open `https://<the box's address>/`, accept
   the box's own certificate, enter the setup code and choose the console password.
3. Mark each port (Internet link 1, Internet link 2, Satellite, Office network).
4. In the portal, Organisation › Locations › Connect this location, copy the **box
   setup code** and paste it into the console's Connect step.

The location turns green in the portal and shows a "Box console" tag. The console
keeps showing the box's links, tunnels and routes, and runs ping and traceroute, even
when ExaCarib cannot be reached. `exa-agent console-reset` on the box sets a new
password if it is lost.

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
