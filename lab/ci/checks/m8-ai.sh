#!/usr/bin/env bash
# AI features (ADR 0006): live NHC feed, an example hurricane that warns
# site-a only, insights from the faults earlier in the run, and one
# "Ask your network" question when an AI key is configured.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml" --env-file "$REPO/.env")

echo "-- hurricane watch"
live=$("${COMPOSE[@]}" exec -T controller python -c '
from exaconnect_controller.ai import storms
from exaconnect_controller.settings import get_settings
print(len(storms.parse(storms.fetch(get_settings().nhc_url))))' 2>&1 | tail -1)
if [[ $live =~ ^[0-9]+$ ]]; then ok "NHC feed read from the lab host: $live active storms"; else bad "NHC feed: $live"; fi
out=$(api POST "/ai/storm-watch/example")
if jq -e '.open_warnings >= 1' <<<"$out" >/dev/null; then ok "example hurricane raised a warning"; else bad "example hurricane: $out"; fi
sites=$(api GET "/insights?kind=storm_warning" | jq -r '[.[] | select(.example) | .site] | unique | join(",")')
if [[ $sites == site-a ]]; then ok "only site-a (Kingston) is warned"; else bad "warned sites: '${sites}'"; fi
note "$(api GET "/insights?kind=storm_warning" | jq -r '[.[] | select(.example)][0].detail')"
api POST "/ai/storm-watch/example?on=false" >/dev/null

echo "-- insights from this run"
api GET "/insights?include_resolved=true&limit=20" |
  jq -r '.[] | "      \(.kind) \(.severity): \(.title)"' | head -10

echo "-- ask your network"
if [[ $(api GET /ai/status | jq -r .ask_enabled) == true ]]; then
  cid=$(customer_id)
  ans=$(api POST /ai/ask "$(jq -n --arg c "$cid" '{question: "In one sentence: which path is voice on at site-a now, and why did it last move?", customer_id: $c}')" | jq -r .answer)
  if [[ -n $ans && $ans != null ]]; then ok "answered: ${ans:0:300}"; else bad "no answer from the AI service"; fi
else
  skip "no AI key on this host (EXA_LLM_API_KEY)"
fi
exit $fail
