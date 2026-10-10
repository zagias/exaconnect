#!/usr/bin/env bash
# Checks the SBC configuration with the real images, without the controller:
# Kamailio parses its config, starts with empty carrier lists and refuses a
# caller that is not on the allow-list; FreeSWITCH starts with ExaCarib's files
# and both SIP profiles run. Used by CI. Needs Docker; publishes no ports.
set -euo pipefail
cd "$(dirname "$0")/.."
KAM_IMAGE=$(awk '/^  kamailio:/{k=1} k&&/image:/{print $2; exit}' docker-compose.yml)
FS_IMAGE=$(awk '/^  freeswitch:/{k=1} k&&/image:/{print $2; exit}' docker-compose.yml)
tmp=$(mktemp -d)
cleanup() { docker rm -f exa-sbc-kam exa-sbc-fs >/dev/null 2>&1 || true; rm -rf "$tmp"; }
trap cleanup EXIT

mkdir -p "$tmp/exacarib/kamailio" "$tmp/exacarib/freeswitch" "$tmp/tls"
: >"$tmp/exacarib/kamailio/dispatcher.list"
: >"$tmp/exacarib/kamailio/address.list"
cp kamailio/kamailio-local.cfg.example "$tmp/local.cfg"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=sbc.invalid \
  -keyout "$tmp/tls/server.key" -out "$tmp/tls/server.crt" 2>/dev/null
chmod 644 "$tmp/tls/server.key"

kam=(-v "$PWD/kamailio/kamailio.cfg:/etc/kamailio/kamailio.cfg:ro" -v "$PWD/kamailio/tls.cfg:/etc/kamailio/tls.cfg:ro"
  -v "$tmp/local.cfg:/etc/kamailio/kamailio-local.cfg:ro" -v "$tmp/tls:/etc/kamailio/tls:ro"
  -v "$tmp/exacarib:/exacarib:ro")
echo "-- kamailio -c"
docker run --rm --entrypoint kamailio "${kam[@]}" "$KAM_IMAGE" -c -f /etc/kamailio/kamailio.cfg 2>&1 |
  grep -E "config file ok|ERROR|CRITICAL"
docker run --rm --entrypoint kamailio "${kam[@]}" "$KAM_IMAGE" -c -f /etc/kamailio/kamailio.cfg >/dev/null 2>&1

echo "-- kamailio refuses an unlisted caller"
docker run -d --name exa-sbc-kam --entrypoint kamailio "${kam[@]}" "$KAM_IMAGE" -DD -E -f /etc/kamailio/kamailio.cfg >/dev/null
ip=$(docker inspect exa-sbc-kam --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
python3 voice/sip_probe.py "$ip" 5060 | tee "$tmp/answer"
grep -q "^SIP/2.0 403" "$tmp/answer"

echo "-- freeswitch starts with ExaCarib's files"
# The gateway as make voice-up installs it for EXA_VOICE_EDGE=kamailio.
mkdir -p "$tmp/external"
cp freeswitch/sip_profiles/external/*.example "$tmp/external/"
cp freeswitch/sip_profiles/external/exacarib_sip.kamailio.xml.example "$tmp/external/exacarib_sip.xml"
docker run -d --name exa-sbc-fs --tmpfs /etc/freeswitch/directory/default \
  --entrypoint /bin/sh -v "$PWD/freeswitch/start.sh:/exacarib-start.sh:ro" \
  -v "$tmp/exacarib:/exacarib:ro" \
  -v "$PWD/freeswitch/directory/exacarib.xml:/etc/freeswitch/directory/exacarib.xml:ro" \
  -v "$PWD/freeswitch/dialplan/exacarib.xml:/etc/freeswitch/dialplan/exacarib.xml:ro" \
  -v "$PWD/freeswitch/dialplan/public/exacarib.xml:/etc/freeswitch/dialplan/public/exacarib.xml:ro" \
  -v "$PWD/freeswitch/ivr_menus/exacarib.xml:/etc/freeswitch/ivr_menus/exacarib.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/callcenter.conf.xml:/etc/freeswitch/autoload_configs/callcenter.conf.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/verto.conf.xml:/etc/freeswitch/autoload_configs/verto.conf.xml:ro" \
  -v "$PWD/freeswitch/vars.xml:/etc/freeswitch/vars.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/event_socket.conf.xml:/etc/freeswitch/autoload_configs/event_socket.conf.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/modules.conf.xml:/etc/freeswitch/autoload_configs/modules.conf.xml:ro" \
  -v "$PWD/freeswitch/autoload_configs/switch.conf.xml:/etc/freeswitch/autoload_configs/switch.conf.xml:ro" \
  -v "$tmp/external:/etc/freeswitch/sip_profiles/external:ro" -e EXA_PBX_SECRET=check-only \
  "$FS_IMAGE" /exacarib-start.sh >/dev/null
for _ in $(seq 60); do
  docker exec exa-sbc-fs fs_cli -x status 2>/dev/null | grep -q '^UP' && break
  sleep 2
done
docker exec exa-sbc-fs fs_cli -x status | head -1
for _ in $(seq 30); do
  docker exec exa-sbc-fs fs_cli -x "sofia status" | grep -E "^[[:space:]]*(internal|external)[[:space:]]+profile" >"$tmp/sofia" || true
  [[ $(grep -c RUNNING "$tmp/sofia") == 2 ]] && break
  sleep 2
done
cat "$tmp/sofia"
[[ $(grep -c RUNNING "$tmp/sofia") == 2 ]]

echo "-- freeswitch can ask the controller before outside calls"
docker exec exa-sbc-fs fs_cli -x "module_exists mod_curl" | grep -qx true
docker exec exa-sbc-fs fs_cli -x "global_getvar exa_pbx_secret" | grep -qx check-only
docker exec exa-sbc-fs fs_cli -x "sofia status gateway exacarib_sip" | grep -E "^(Name|Proxy|Status)"
echo "-- call queues and spoken menu greetings are switched on"
docker exec exa-sbc-fs fs_cli -x "module_exists mod_callcenter" | grep -qx true
docker exec exa-sbc-fs fs_cli -x "module_exists mod_flite" | grep -qx true
echo "-- the watcher reloads newly rendered config"
docker exec exa-sbc-fs fs_cli -x "user_exists id 201 check.voice.exacarib.internal" | grep -qx false
mkdir -p "$tmp/exacarib/freeswitch/ccheck"
cat >"$tmp/exacarib/freeswitch/ccheck/directory.xml" <<'XML'
<include>
  <domain name="check.voice.exacarib.internal">
    <groups><group name="default"><users>
      <user id="201"><params><param name="password" value="check-only"/></params></user>
    </users></group></groups>
  </domain>
</include>
XML
for _ in $(seq 15); do
  docker exec exa-sbc-fs fs_cli -x "user_exists id 201 check.voice.exacarib.internal" | grep -qx true && break
  sleep 2
done
docker exec exa-sbc-fs fs_cli -x "user_exists id 201 check.voice.exacarib.internal" | grep -qx true
echo "-- the browser phone's sign-in listens on the compose network"
for _ in $(seq 15); do
  docker exec exa-sbc-fs fs_cli -x "verto status" >"$tmp/verto" 2>&1 || true
  grep -q "8081" "$tmp/verto" && break
  sleep 2
done
grep -E "exacarib-wss|8081" "$tmp/verto"
grep -q "8081" "$tmp/verto"
echo "SBC check passed"
