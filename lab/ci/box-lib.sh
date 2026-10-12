# shellcheck shell=bash
# shellcheck disable=SC2154  # secrets is set by the check that sources this
# Shared by the box checks (box-console.sh, zz-box-image.sh): a test organisation
# with one connected location, the walk-through in a browser, and what the
# controller then knows. Source after lib.sh. Secrets go only into $secrets/env.

# box_location <tag> <lan prefix> <public host>: makes "Set-up check box <tag>" with
# "Branch box check <tag>" connected; sets org, place, site, name and writes
# JOIN_CODE (the portal's box setup code) to $secrets/env.
box_location() {
  local tag=$1 prefix=$2 host=$3 cid loc conn url
  org="Set-up check box $tag" place="Branch box check $tag"
  cid=$(api POST /admin/organisations "{\"name\": \"$org\", \"products\": [\"connect\"], \"owner_email\": \"box-$tag@setup-check.example\", \"country\": \"JM\"}" | jq -r '.id // empty')
  [[ -n $cid ]] || { bad "could not create the test organisation"; return 1; }
  loc=$(api POST "/orgs/$cid/locations" "{\"name\": \"$place\", \"city\": \"Kingston\", \"country\": \"JM\", \"timezone\": \"America/Jamaica\"}" | jq -r '.id // empty')
  conn=$([[ -n $loc ]] && api POST "/orgs/$cid/locations/$loc/connect" "{\"links\": [{\"carrier\": \"Flow\", \"underlay_type\": \"fibre\", \"commit_mbps\": 100}], \"lan_prefixes\": [\"$prefix\"]}")
  site=$(jq -r '.site_id // empty' <<<"$conn")
  name=$(jq -r '.name // empty' <<<"$conn")
  if [[ -z $site || -z $name ]]; then bad "could not connect the test location"; return 1; fi
  ok "test location connected as $name"
  url=$(jq -r '.install.public_agent_url // empty' <<<"$conn")
  [[ -n $url ]] || url="https://$host:8443"
  jq -r --arg u "$url" '.install | {c: $u, t: .token, f: .ca_fingerprint, n: .site} | @json' <<<"$conn" |
    tr -d '\n' | base64 -w0 | tr '+/' '-_' | tr -d '=' | sed 's/^/JOIN_CODE=EXA1./' >"$secrets/env"
  echo >>"$secrets/env"
  {
    echo "PORTAL_EMAIL=$(grep '^EXA_ADMIN_EMAIL=' "$REPO/.env" | cut -d= -f2-)"
    echo "PORTAL_PW=$(grep '^EXA_ADMIN_PASSWORD=' "$REPO/.env" | cut -d= -f2-)"
  } >>"$secrets/env"
}

# box_walkthrough <label> <docker run options...>: runs deploy/uat/box-console.mjs
# in the browser image with $secrets/env, then deletes that file.
box_walkthrough() {
  local label=$1 base=mcr.microsoft.com/playwright:v1.48.0-noble image=exaconnect/phone-uat:1.48.0 out line
  shift
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    if ! printf 'FROM %s\nWORKDIR /uat\nRUN npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.48.0\n' "$base" |
      docker build -q -t "$image" - >/dev/null 2>&1; then
      bad "could not build the browser test image from $base"
      rm -f "$secrets/env"
      return 1
    fi
  fi
  out=$(timeout 600 docker run --rm --ipc host --env-file "$secrets/env" "$@" \
    -e SITE_NAME="$name" -e SITE_ID="$site" -e ORG_NAME="$org" -e LOCATION_NAME="$place" \
    -v "$REPO/deploy/uat/box-console.mjs:/uat/box-console.mjs:ro" -w /uat "$image" node box-console.mjs 2>&1)
  rm -f "$secrets/env"
  while IFS= read -r line; do
    case $line in
      "PASS "*) ok "$label: ${line#PASS }" ;;
      "FAIL "*) bad "$label: ${line#FAIL }" ;;
      *) ;;
    esac
  done <<<"$out"
  grep -q '^PASS ' <<<"$out" || bad "$label test did not run: $(tail -4 <<<"$out" | tr '\n' ' ' | cut -c1-400)"
}

# box_controller_checks <wan iface>: what the controller knows about the box; sets node.
box_controller_checks() {
  local iface n v okv
  node=$(sql "SELECT id FROM nodes WHERE site_id = '$site'")
  # A box in an emulated machine (no KVM) is slow: give its first reports time to arrive.
  for _ in $(seq 1 36); do
    [[ -n $node && $(sql "SELECT box->>'console' = 'true' AND coalesce(applied_version, 0) >= 1 FROM nodes WHERE id = '$node'") == t ]] && break
    sleep 5
    node=$(sql "SELECT id FROM nodes WHERE site_id = '$site'")
  done
  if [[ -n $node && $(sql "SELECT box->>'console' FROM nodes WHERE id = '$node'") == true ]]; then
    ok "the controller has the box's console report"
  else
    bad "no box console report for $name"
  fi
  iface=$(sql "SELECT underlay_interface FROM links WHERE site_id = '$site' AND path = 'carrier-a'")
  if [[ $iface == "$1" ]]; then ok "the link follows the port chosen on the box ($1)"; else bad "link underlay is '$iface', not $1"; fi
  n=$(sql "SELECT count(*) FROM events WHERE node_id = '$node' AND kind = 'local_change'")
  if ((${n:-0} >= 1)); then ok "changes made on the box reach the controller ($n)"; else bad "no local_change events from the box"; fi
  v=$(sql "SELECT applied_version FROM nodes WHERE id = '$node'")
  okv=$(sql "SELECT coalesce(apply_ok::text, 'none') FROM nodes WHERE id = '$node'")
  if ((${v:-0} >= 1)) && [[ $okv == true ]]; then
    ok "the box applied its desired state (v$v)"
  else
    bad "box desired state: version ${v:-none}, ok $okv: $(sql "SELECT left(apply_error, 200) FROM nodes WHERE id = '$node'")"
  fi
}
