#!/usr/bin/env bash
# AI features (ADR 0006, 0008): live NHC, USGS, GDACS and tsunami.gov feeds,
# an example hurricane that warns site-a only, an example earthquake reported
# by three feeds that raises one alert for site-b only, insights from the faults earlier in the run, and one
# "Ask your network" question and one proposed change (applied, then undone) when an AI key is configured.
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

echo "-- disaster watch"
live=$("${COMPOSE[@]}" exec -T controller python -c '
from exaconnect_controller.ai import hazards
from exaconnect_controller.settings import get_settings
s = get_settings()
reports, failed = hazards.read_feeds(s.usgs_url, s.gdacs_url, [u for u in s.tsunami_urls.split(",") if u])
by = {}
for r in reports:
    by[r.source] = by.get(r.source, 0) + 1
events = hazards.cluster(hazards.current(reports, __import__("datetime").datetime.now(__import__("datetime").UTC), True))
print(" ".join(f"{k}={v}" for k, v in sorted(by.items())), f"events={len(events)}", "failed=" + ",".join(sorted(failed)),
      "; ".join(f"{k}: {v}" for k, v in hazards.last_errors.items()))' 2>&1 | tail -1)
if [[ $live == *events=* && $live =~ failed=\ ?$ ]]; then ok "all disaster feeds read from the lab host: $live"
elif [[ $live == *events=* ]]; then bad "a disaster feed could not be read: $live"
else bad "disaster feeds: $live"; fi
haz=$(api GET "/insights?kind=hazard" | jq -c '[.[] | select(.example)]')
if jq -e 'length == 1 and .[0].site == "site-b" and (.[0].data.sources | length) == 3' <<<"$haz" >/dev/null; then
  ok "example earthquake from USGS, GDACS and tsunami.gov raised one alert, for site-b only"
else bad "example earthquake alerts: $(jq -c '[.[] | {site, sources: .data.sources}]' <<<"$haz")"; fi
note "$(jq -r '.[0].detail' <<<"$haz")"
api POST "/ai/storm-watch/example?on=false" >/dev/null

echo "-- insights from this run"
api GET "/insights?include_resolved=true&limit=20" |
  jq -r '.[] | "      \(.kind) \(.severity): \(.title)"' | head -10

echo "-- ask your network"
# Why the controller answered an error: its message names the AI service's own
# status (for example 429 or 503) and never carries the key.
ask_why() { note "$(api_detail POST /ai/ask "$(jq -n --arg c "$(customer_id)" '{question: "ping", customer_id: $c}')")"; }
if [[ $(api GET /ai/status | jq -r .ask_enabled) == true ]]; then
  cid=$(customer_id)
  ans=$(api POST /ai/ask "$(jq -n --arg c "$cid" '{question: "In one sentence: which path is voice on at site-a now, and why did it last move?", customer_id: $c}')" | jq -r .answer)
  if [[ -n $ans && $ans != null ]]; then ok "answered: ${ans:0:300}"; else bad "no answer from the AI service"; ask_why; fi
  # Asked for a change, it proposes one; applying and undoing leave the network as it was.
  req="Add a traffic rule called lab assistant check that puts udp:7777 in bulk at site-b."
  out=$(api POST /ai/ask "$(jq -n --arg c "$cid" --arg q "$req" '{question: $q, customer_id: $c}')")
  pid=$(jq -r '.plan.id // empty' <<<"$out")
  if [[ -n $pid && $(jq '.plan.problems | length' <<<"$out") == 0 ]]; then
    ok "proposed: $(jq -r '.plan.summary | join(" ")' <<<"$out")"
    applied=$(api POST "/ai/plans/$pid/apply" | jq -r .status)
    has=$(api GET "/customers/$cid/rules" | jq '[.[] | select(.name | test("lab assistant check"; "i"))] | length')
    undone=$(api POST "/ai/plans/$pid/undo" | jq -r .status)
    left=$(api GET "/customers/$cid/rules" | jq '[.[] | select(.name | test("lab assistant check"; "i"))] | length')
    if [[ $applied == applied && $has == 1 && $undone == undone && $left == 0 ]]; then ok "applied, then undone"
    else bad "apply/undo: applied=$applied rule=$has undone=$undone left=$left"; fi
  else
    bad "no usable plan: $(jq -c '{answer, plan: (.plan | if . then {summary, problems} else null end)}' <<<"$out" | head -c 600)"
  fi
else
  skip "no AI key on this host (EXA_LLM_API_KEY)"
fi
exit $fail
