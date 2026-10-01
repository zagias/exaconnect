# One image for every lab node: sites, PoP and the underlay "carrier" routers.
# FRR 10.x (Alpine based) plus the tools the agent and fault scripts need.
FROM quay.io/frrouting/frr:10.2.1

RUN apk add --no-cache \
      bash iproute2 iproute2-tc nftables wireguard-tools \
      iperf3 tcpdump iputils curl jq ethtool

# bgpd and bfdd only; zebra, staticd and watchfrr start by default.
RUN sed -i -e 's/^bgpd=no/bgpd=yes/' -e 's/^bfdd=no/bfdd=yes/' /etc/frr/daemons

# exa-agent is bind-mounted from the host's bin/lab directory (lab/exaconnect.clab.yml).
RUN ln -s /opt/exaconnect/bin/exa-agent /usr/local/bin/exa-agent
