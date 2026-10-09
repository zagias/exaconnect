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

# Where FreeSWITCH sends outside calls (deploy/kamailio/README.md): EXA_VOICE_EDGE=provider
# (the default: one SIP provider, FreeSWITCH registers to it) or kamailio (Kamailio holds
# the carriers). The gateway file has no secrets; FreeSWITCH reads them from its environment.
edge=$(grep -m1 '^EXA_VOICE_EDGE=' .env 2>/dev/null | cut -d= -f2 || true)
gw=deploy/freeswitch/sip_profiles/external/exacarib_sip.xml
case ${edge:-provider} in
  kamailio)
    cp deploy/freeswitch/sip_profiles/external/exacarib_sip.kamailio.xml.example "$gw"
    # Kamailio lets FreeSWITCH in by address: the compose network both run on (group 2).
    # Kept in .env so the controller's later renders keep it.
    if ! grep -q '^EXA_KAMAILIO_PBX_NETS=.' .env; then
      net=$(docker network inspect exaconnect_default --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' | awk '{print $1}')
      [[ -n $net ]] || { echo "No compose network yet: start the controller first (make controller-up)"; exit 1; }
      sed -i '/^EXA_KAMAILIO_PBX_NETS=/d' .env
      echo "EXA_KAMAILIO_PBX_NETS=$net" >>.env
      "${COMPOSE[@]}" up -d controller
    fi
    ;;
  provider)
    if grep -qs 'exa-edge: kamailio' "$gw"; then rm -f "$gw"; fi
    if [[ ! -e $gw ]] && grep -q '^EXA_SIP_USERNAME=.' .env; then
      cp deploy/freeswitch/sip_profiles/external/exacarib_sip.xml.example "$gw"
    fi
    ;;
  *)
    echo "EXA_VOICE_EDGE must be provider or kamailio"
    exit 1
    ;;
esac
grep -q '^EXA_PBX_SECRET=.' .env || echo "Note: EXA_PBX_SECRET is not set (lab/scripts/init-env.sh): every outside call will be refused"

# The carrier lists Kamailio loads at start, rendered from the database.
"${COMPOSE[@]}" exec -T controller python - <<'PY' >/dev/null
from exaconnect_controller import db
from exaconnect_controller.commai.voice import carriers, freeswitch
from exaconnect_controller.settings import get_settings

db.init(get_settings().database_url)
with db.tx() as conn:
    carriers.render_kamailio(conn)
    # Each business's dial plan again, so it has the current check before outside calls.
    for row in conn.execute("SELECT DISTINCT customer_id FROM voice_users").fetchall():
        freeswitch.render_business(conn, row["customer_id"])
db.close()
PY

# A test extension for ExaCarib's admin in the demo business (deploy/voice/test_phone.py).
"${COMPOSE[@]}" exec -T controller python - <deploy/voice/test_phone.py

# --no-deps: never recreate the controller here. This script runs without
# deploy/public/site.env, so a recreated controller would lose the public agent
# gateway (EXA_PUBLIC_HOST) and enrolment tokens would carry no install address.
"${COMPOSE[@]}" --profile voice up -d --no-deps freeswitch kamailio
# A FreeSWITCH that was already running reads the files rendered above.
for _ in $(seq 30); do
  "${COMPOSE[@]}" exec -T freeswitch fs_cli -x reloadxml >/dev/null 2>&1 && break
  sleep 2
done
echo "SBC started: Kamailio and FreeSWITCH, no public SIP ports"
