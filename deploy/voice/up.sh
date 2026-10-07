#!/usr/bin/env bash
# make voice-up: start the SBC (Kamailio in front of FreeSWITCH) next to the
# controller. No SIP or media port is published: carriers can't reach it until
# a carrier account, EXA_VOICE_PUBLIC_IP and a firewall rule for that carrier
# exist (deploy/kamailio/README.md). Safe to run again; existing files are kept.
set -euo pipefail
cd "$(dirname "$0")/../.."
COMPOSE=(docker compose -f deploy/docker-compose.yml --env-file .env)
public_ip=$(grep -m1 '^EXA_VOICE_PUBLIC_IP=' .env 2>/dev/null | cut -d= -f2 || true)

# Kamailio's host settings: the address it advertises (loopback until the voice
# host is public). Kept once written, so a hand edit survives.
local_cfg=deploy/kamailio/kamailio-local.cfg
if [[ ! -s $local_cfg ]]; then
  sed "s/203\.0\.113\.10/${public_ip:-127.0.0.1}/" deploy/kamailio/kamailio-local.cfg.example >"$local_cfg"
fi

# A stand-in certificate for port 5061 until the real one for the voice host
# name is put in deploy/kamailio/tls/ (never committed).
tls=deploy/kamailio/tls
if [[ ! -s $tls/server.crt || ! -s $tls/server.key ]]; then
  mkdir -p "$tls"
  (umask 077 && openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj /CN=voice.invalid \
    -keyout "$tls/server.key" -out "$tls/server.crt" 2>/dev/null)
fi

# The carrier lists Kamailio loads at start, rendered from the database.
"${COMPOSE[@]}" exec -T controller python - <<'PY' >/dev/null
from exaconnect_controller import db
from exaconnect_controller.commai.voice import carriers

with db.tx() as conn:
    carriers.render_kamailio(conn)
PY

"${COMPOSE[@]}" --profile voice up -d freeswitch kamailio
echo "SBC started: Kamailio and FreeSWITCH, no public SIP ports"
