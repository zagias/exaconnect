#!/usr/bin/env bash
# M6: metering and the carrier view (demo step 6). Runs bulk traffic from
# site A for two full 5-minute buckets, then checks the samples, the 95th
# percentile and burst on screen (API), the carrier user's scope, and that
# the CSV matches the screen. Bulk may still be on carrier B after the
# earlier steps (it moves back only after 5 good minutes), so the check
# follows whichever link carries it.
# shellcheck disable=SC2329  # helpers are called through wait_for
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
lab/faults/restore.sh >/dev/null

echo "-- traffic: 20 Mbit/s bulk (CS1) from lan-a to lan-b for 11 minutes"
docker exec -d "$(node lan-b)" iperf3 -s -1 -p 5201
sleep 1
docker exec -d "$(node lan-a)" sh -c 'iperf3 -c 192.168.20.10 -p 5201 -u -b 20M -S 32 -t 660 > /tmp/iperf-bulk.txt 2>&1'
start=$(date -u +%FT%TZ)
bulk_path=$(path_of site-a 0x103 192.168.20.10)
note "bulk leaves site-a on ${bulk_path:-?}"
sample_ready() {
  (($(sql "SELECT count(*) FROM usage_5m u JOIN links l ON l.id = u.link_id JOIN sites s ON s.id = l.site_id
           WHERE s.name = 'site-a' AND u.bucket >= '$start'::timestamptz - interval '5 minutes'
             AND u.out_mbps > 5") >= 2))
}
if wait_for 900 sample_ready; then ok "5-minute samples with traffic for site-a"; else bad "no 5-minute samples with traffic after 15 minutes"; fi
note "iperf: $(docker exec "$(node lan-a)" sh -c 'tail -n 4 /tmp/iperf-bulk.txt' 2>&1 | tr '\n' ' ' | cut -c1-300)"

links=$(api GET "/metering/links?hours=1")
a=$(jq -c '[.links[] | select(.site == "site-a")] | max_by(.billable_mbps // 0)' <<<"$links")
note "site-a $(jq -r .path <<<"$a"): $(jq -r '"samples \(.samples), 95th in \(.p95_in_mbps) out \(.p95_out_mbps), billable \(.billable_mbps), commit \(.commit_mbps), burst \(.burst_mbps), total \(.total)"' <<<"$a")"
billable=$(jq -r .billable_mbps <<<"$a")
if awk -v b="$billable" 'BEGIN { exit !(b > 10 && b < 30) }'; then ok "billable rate ${billable} Mbps matches the ~20 Mbit/s offered"; else bad "billable rate ${billable}"; fi

echo "-- CSV matches the screen"
link_id=$(jq -r .id <<<"$a")
csv=$(api GET "/metering/settlement.csv?hours=1&link_id=$link_id")
csv_p95=$(awk -F, 'NR > 1 && $1 != "settlement" && NF == 9 { m = ($7 > $8) ? $7 : $8; print m }' <<<"$csv" | sort -g |
  awk '{ v[NR] = $1 } END { d = int(NR * 0.05); printf "%.3f", v[NR - d] }')
api_p95=$(printf '%.3f' "$billable")
if [[ $csv_p95 == "$api_p95" ]]; then ok "CSV 95th percentile $csv_p95 equals the screen"; else bad "CSV $csv_p95 vs screen $api_p95"; fi
if grep -q "total=$(jq -r .total <<<"$a")" <<<"$csv"; then ok "CSV settlement total equals the screen"; else bad "CSV total differs"; fi

echo "-- carrier view"
pw=$(openssl rand -hex 16)
docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env" exec -T -e PW="$pw" controller python - <<'PY' >/dev/null
import os
from exaconnect_controller import db
from exaconnect_controller.security import hash_password
from exaconnect_controller.settings import get_settings
db.init(get_settings().database_url)
with db.tx() as conn:
    cid = conn.execute("SELECT id FROM carriers WHERE name = 'Carrier A'").fetchone()["id"]
    conn.execute(
        """INSERT INTO users (email, password_hash, role, carrier_id) VALUES ('lab-noc@carrier-a.example', %s, 'carrier', %s)
           ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash""",
        (hash_password(os.environ["PW"]), cid),
    )
PY
tok=$(jq -n --arg p "$pw" '{email:"lab-noc@carrier-a.example", password:$p}' |
  curl -fsS -H 'Content-Type: application/json' -d @- http://127.0.0.1:8000/api/v1/auth/login | jq -r .token)
carriers=$(curl -fsS -H "Authorization: Bearer $tok" "http://127.0.0.1:8000/api/v1/metering/links?hours=1" |
  jq -r '[.links[].carrier] | unique | join(",")')
if [[ $carriers == "Carrier A" ]]; then ok "carrier user sees only Carrier A links"; else bad "carrier user sees: $carriers"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $tok" http://127.0.0.1:8000/api/v1/overview)
if [[ $code == 403 ]]; then ok "carrier user cannot open the customer overview"; else bad "carrier overview returned $code"; fi
exit $fail
