#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# Jibsy Phone, the installable phone app, tested on this host as people use it
# (deploy/voice/uat/phone-app.mjs, ADR 0042): two test accounts in the demo business
# sign in at the app's own address, call each other with audio both ways, and go
# through contacts, recents, settings, offline and screen sizes. A third account has
# no extension. The accounts get fresh random passwords each run
# (deploy/voice/uat/live_users.py); the passwords pass through a private file that is
# deleted straight after and are never printed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" --profile voice)

echo "-- phone app"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
phone=$(. deploy/public/site.env && echo "${EXA_PHONE_HOST:-}")
[[ -n $phone ]] || { skip "no phone app address (EXA_PHONE_HOST)"; exit 0; }
if [[ -z $("${COMPOSE[@]}" ps -q --status running freeswitch 2>/dev/null) ]]; then
  skip "phone system not running on this host"
  exit 0
fi

# The Playwright image has the browsers; the test needs the matching package.
base=mcr.microsoft.com/playwright:v1.48.0-noble
image=exaconnect/phone-uat:1.48.0
if ! docker image inspect "$image" >/dev/null 2>&1; then
  if ! printf 'FROM %s\nWORKDIR /uat\nRUN npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.48.0\n' "$base" |
    docker build -q -t "$image" - >/dev/null 2>&1; then
    bad "could not build the phone app test image from $base"
    exit 1
  fi
fi

users=$("${COMPOSE[@]}" exec -T controller python - <"$REPO/deploy/voice/uat/live_users.py" 2>/dev/null | tail -1)
if ! jq -e '.A_EXT and .B_EXT and .A_PW' >/dev/null 2>&1 <<<"$users"; then
  bad "could not set up the phone app's test accounts"
  exit 1
fi
ok "test accounts ready (extensions $(jq -r '"\(.A_EXT) and \(.B_EXT)"' <<<"$users"))"
secrets=$(mktemp -d)
chmod 700 "$secrets"
jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$users" >"$secrets/env"
unset users
# While the test call is up, read the server's side every 3 s: host load, CPU steal (time the
# provider gave our virtual CPUs to someone else), FreeSWITCH's CPU and its own view of the audio.
fsc=$("${COMPOSE[@]}" ps -q freeswitch 2>/dev/null)
fspid=$(pgrep -x freeswitch | head -1)
watch_server() {
  local prev now hz
  hz=$(getconf CLK_TCK)
  prev="$(head -1 /proc/stat) $(awk '{print $14+$15}' "/proc/$fspid/stat" 2>/dev/null || echo 0)"
  while sleep 3; do
    now="$(head -1 /proc/stat) $(awk '{print $14+$15}' "/proc/$fspid/stat" 2>/dev/null || echo 0)"
    uuids=$(docker exec "$fsc" fs_cli -x "show channels" 2>/dev/null | grep -oE '^[0-9a-f-]{36}' | grep -c .)
    if ((uuids >= 2)); then
      # cpu user nice system idle iowait irq softirq steal ... then FreeSWITCH's ticks
      awk -v a="$prev" -v b="$now" -v hz="$hz" 'BEGIN { n = split(a, x, " "); split(b, y, " ");
        for (i = 2; i <= 9; i++) t += y[i] - x[i]; st = y[9] - x[9]; id = y[5] - x[5];
        printf "%s %.1f %.1f %.1f\n", "'"$(cut -d' ' -f1 /proc/loadavg)"'", 100 * st / t, 100 * (t - id) / t, 100 * (y[n] - x[n]) / (3 * hz) }' >>"$secrets/server"
      for u in $(docker exec "$fsc" fs_cli -x "show channels" 2>/dev/null | grep -oE '^[0-9a-f-]{36}'); do
        docker exec "$fsc" fs_cli -x "uuid_set_media_stats $u" >/dev/null 2>&1
        docker exec "$fsc" fs_cli -x "uuid_dump $u" 2>/dev/null |
          grep -oE '^variable_rtp_audio_in_(media_packet_count|skip_packet_count|jitter_max_variance|flaw_total|mos): [0-9.]+' |
          sed 's/variable_rtp_audio_in_//; s/: /=/' | tr '\n' ' '
        echo
      done >"$secrets/fs.tmp" && mv "$secrets/fs.tmp" "$secrets/fs"
    fi
    prev=$now
  done
}
[[ -n $fsc && -n $fspid ]] && { watch_server & watcher=$!; }
started=$(date +%s)
out=$(timeout 900 docker run --rm --network host --add-host "$phone:127.0.0.1" --ipc host \
  --env-file "$secrets/env" -e UAT_BASE="https://$phone" -e UAT_SHOTS=/tmp/shots \
  -v "$REPO/deploy/voice/uat/phone-app.mjs:/uat/phone-app.mjs:ro" -w /uat "$image" \
  node phone-app.mjs 2>&1)
[[ -n ${watcher:-} ]] && kill "$watcher" 2>/dev/null
if [[ -s $secrets/server ]]; then
  note "server during the test call (load, CPU steal %, CPU busy %, FreeSWITCH CPU %):" \
    "$(awk '{ if ($1 > l) l = $1; s += $2; b += $3; f += $4; n++ } END { printf "load up to %s, steal %.1f%%, busy %.0f%%, FreeSWITCH %.1f%% over %d readings", l, s / n, b / n, f / n, n }' "$secrets/server")"
fi
[[ -s $secrets/fs ]] && while read -r leg; do [[ -n $leg ]] && note "FreeSWITCH heard on one leg: $leg"; done <"$secrets/fs"
rm -rf "$secrets"
grep '^QUALITY ' <<<"$out" | while read -r _ who q; do note "call quality as $who hears it: $q"; done
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "app: ${line#PASS }" ;;
    "FAIL "*) bad "app: ${line#FAIL }" ;;
    *) ;;
  esac
done <<<"$out"
# Both phones send how the call sounded when it ends (portal quality.ts); real calls do the same.
reports=$(sql "SELECT count(*) FROM voice_call_quality q JOIN users u ON u.id::text = q.user_id
  WHERE u.email LIKE 'phone-app-test-%' AND q.created_at > to_timestamp($started)")
if [[ ${reports:-0} -ge 2 ]]; then ok "both phones reported the call's quality to the controller ($reports reports)"
else bad "call quality reports missing after the test call (${reports:-0} of 2)"; fi
note "real calls in the last 24 h, as people's phones heard them (test accounts left out):"
sql "SELECT count(*), round(avg(concealed_pct)::numeric, 2), round(max(concealed_pct)::numeric, 2),
    round(avg(100.0 * rx_lost / greatest(rx_packets + rx_lost, 1)), 2), round(avg(jitter_ms)), max(jitter_max_ms),
    round(avg(rtt_ms)), max(rtt_max_ms), round(avg(up_lost_pct)::numeric, 2), sum(bad_stretches)
  FROM voice_call_quality q WHERE created_at > now() - interval '24 hours' AND NOT EXISTS (SELECT 1 FROM users u
    WHERE u.id::text = q.user_id AND u.email ~ '(test|check)')" |
  awk -F'|' '{ printf "      %s call legs; made-up sound avg %s%% (worst %s%%); loss in %s%%, out %s%%; jitter avg %s ms (max %s); round trip avg %s ms (max %s); %s break-ups\n", $1, $2, $3, $4, $9, $5, $6, $7, $8, $10 }'
sql "SELECT to_char(created_at, 'HH24:MI'), extension, direction, secs, device, network, path, concealed_pct, bad_stretches,
    100.0 * rx_lost / greatest(rx_packets + rx_lost, 1), up_lost_pct, jitter_ms, jitter_max_ms, jitter_buffer_ms, rtt_ms, rtt_max_ms
  FROM voice_call_quality q WHERE created_at > now() - interval '24 hours' AND NOT EXISTS (SELECT 1 FROM users u
    WHERE u.id::text = q.user_id AND u.email ~ '(test|check)')
  ORDER BY created_at DESC LIMIT 20" |
  awk -F'|' '{ printf "      %s ext %s %s %ss %s %s via %s: made-up %s%%, %s break-ups, loss in %.1f%% out %s%%, jitter %s/%s ms, buffer %s ms, rtt %s/%s ms\n", $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16 }'
if ! grep -q '^PASS ' <<<"$out"; then
  bad "phone app test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
elif ! grep -qE '^[0-9]+ passed, 0 failed' <<<"$out"; then
  note "last lines: $(tail -6 <<<"$out" | grep -v '^PASS' | tr '\n' ' ' | cut -c1-400)"
  fail=1
fi
exit "$fail"
