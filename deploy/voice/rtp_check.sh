#!/usr/bin/env bash
# Ephemeral CI only: real SIP/PCMU media, no public ports or external calls.
set -euo pipefail
[[ ${EXA_VOICE_RTP_TESTS:-} == 1 && $(uname -s) == Linux ]] || {
  echo "Set EXA_VOICE_RTP_TESTS=1 on an ephemeral Linux CI runner." >&2
  exit 2
}
cd "$(dirname "$0")/.."
KAM_IMAGE=$(awk '/^  kamailio:/{k=1} k&&/image:/{print $2; exit}' docker-compose.yml)
FS_IMAGE=$(awk '/^  freeswitch:/{k=1} k&&/image:/{print $2; exit}' docker-compose.yml)
rtp_id="exa-rtp-$$-$(date +%s)"
rtp_net="$rtp_id-net"
rtp_kam="$rtp_id-kam"
rtp_fs="$rtp_id-fs"
rtp_tmp=$(mktemp -d)
cleanup() {
  local rtp_status=$?
  if ((rtp_status != 0)); then
    docker inspect "$rtp_kam" "$rtp_fs" --format '{{.Name}}: {{.State.Status}} {{.State.Error}}' 2>/dev/null || true
    docker logs --tail 60 "$rtp_kam" 2>&1 || true
    docker logs --tail 60 "$rtp_fs" 2>&1 || true
    docker exec "$rtp_fs" sh -c 'tail -n 100 /var/log/freeswitch/freeswitch.log' 2>&1 || true
  fi
  docker rm -f "$rtp_kam" "$rtp_fs" >/dev/null 2>&1 || true
  docker network rm "$rtp_net" >/dev/null 2>&1 || true
  rm -rf "$rtp_tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker network create --internal "$rtp_net" >/dev/null
mkdir -p "$rtp_tmp/exacarib/kamailio" "$rtp_tmp/exacarib/freeswitch" "$rtp_tmp/tls" "$rtp_tmp/external" "$rtp_tmp/start"
: >"$rtp_tmp/exacarib/kamailio/dispatcher.list"
: >"$rtp_tmp/exacarib/kamailio/address.list"
cp kamailio/kamailio-local.cfg.example "$rtp_tmp/local.cfg"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=rtp-ci.invalid \
  -keyout "$rtp_tmp/tls/server.key" -out "$rtp_tmp/tls/server.crt" 2>/dev/null
chmod 644 "$rtp_tmp/tls/server.key"
docker run -d --name "$rtp_kam" --network "$rtp_net" --network-alias kamailio --entrypoint /bin/sh \
  -v "$PWD/kamailio/kamailio.cfg:/etc/kamailio/kamailio.cfg:ro" \
  -v "$PWD/kamailio/tls.cfg:/etc/kamailio/tls.cfg:ro" \
  -v "$PWD/kamailio/start.sh:/exacarib-start.sh:ro" \
  -v "$rtp_tmp/local.cfg:/etc/kamailio/kamailio-local.cfg:ro" \
  -v "$rtp_tmp/tls:/etc/kamailio/tls:ro" -v "$rtp_tmp/exacarib:/exacarib:ro" \
  -v "$rtp_tmp/start:/ci:ro" "$KAM_IMAGE" \
  -c 'until [ -f /ci/ready ]; do sleep 1; done; exec /bin/sh /exacarib-start.sh' >/dev/null
rtp_kam_ip=$(docker inspect "$rtp_kam" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
sed "s/203.0.113.10/$rtp_kam_ip/g" kamailio/kamailio-local.cfg.example >"$rtp_tmp/local.cfg"
touch "$rtp_tmp/start/ready"
# Initialise the real startup watcher's legacy path before the media driver
# installs its local carrier lists. No synthetic htable override is used.
for _ in $(seq 15); do
  python3 voice/sip_probe.py "$rtp_kam_ip" 5060 >"$rtp_tmp/start/answer" || true
  grep -q "^SIP/2.0 403" "$rtp_tmp/start/answer" && break
  sleep 1
done
grep -q "^SIP/2.0 403" "$rtp_tmp/start/answer"
cp freeswitch/sip_profiles/external/exacarib_sip.kamailio.xml.example "$rtp_tmp/external/exacarib_sip.xml"
# The production config normally detects its local interface using the route
# table. This deliberately route-less network supplies that address explicitly.
PYTHONPATH="$PWD/voice" python3 - "$PWD/freeswitch/vars.xml" "$rtp_tmp/vars.xml" <<'PY'
import sys
from pathlib import Path
from rtp_check import isolated_vars
Path(sys.argv[2]).write_text(isolated_vars(Path(sys.argv[1]).read_text()))
PY
docker run -d --name "$rtp_fs" --network "$rtp_net" --network-alias freeswitch \
  --tmpfs /etc/freeswitch/directory/default --entrypoint /bin/sh \
  -v "$PWD/freeswitch/start.sh:/exacarib-start.sh:ro" \
  -v "$PWD/freeswitch/phone-runtime.sh:/exacarib-phone-runtime.sh:ro" -v "$rtp_tmp/exacarib:/exacarib:ro" \
  -v "$PWD/freeswitch/directory/exacarib.xml:/etc/freeswitch/directory/exacarib.xml:ro" \
  -v "$PWD/freeswitch/dialplan/exacarib.xml:/etc/freeswitch/dialplan/exacarib.xml:ro" \
  -v "$PWD/freeswitch/dialplan/public/exacarib.xml:/etc/freeswitch/dialplan/public/exacarib.xml:ro" \
  -v "$PWD/freeswitch/ivr_menus/exacarib.xml:/etc/freeswitch/ivr_menus/exacarib.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/callcenter.conf.xml:/etc/freeswitch/autoload_configs/callcenter.conf.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/verto.conf.xml:/etc/freeswitch/autoload_configs/verto.conf.xml:ro" \
  -v "$rtp_tmp/vars.xml:/etc/freeswitch/vars.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/event_socket.conf.xml:/etc/freeswitch/autoload_configs/event_socket.conf.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/modules.conf.xml:/etc/freeswitch/autoload_configs/modules.conf.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/switch.conf.xml:/etc/freeswitch/autoload_configs/switch.conf.xml:ro" \
  -v "$rtp_tmp/external:/etc/freeswitch/sip_profiles/external:ro" -e EXA_PBX_SECRET=rtp-ci-only \
  "$FS_IMAGE" -c 'EXA_VOICE_PUBLIC_IP=$(hostname -i); export EXA_VOICE_PUBLIC_IP; exec /exacarib-start.sh' >/dev/null
rtp_fs_ip=$(docker inspect "$rtp_fs" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
# Internal networks deliberately give containers no default gateway. Their
# bridge still has an IPAM address where this local-only SIP/RTP peer listens.
rtp_gateway=$(docker network inspect "$rtp_net" --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
echo "-- isolated RTP addresses: Kamailio $rtp_kam_ip, FreeSWITCH $rtp_fs_ip, endpoint $rtp_gateway"
python3 voice/rtp_check.py --kamailio "$rtp_kam" --freeswitch "$rtp_fs" \
  --kamailio-ip "$rtp_kam_ip" --freeswitch-ip "$rtp_fs_ip" --endpoint-ip "$rtp_gateway" \
  --directory "$rtp_tmp/exacarib/kamailio"
