#!/usr/bin/env bash
# The agent gateway (ADR 0024): real sites reach the controller on the public
# name's port 8443. The certificate names the public host and chains to the
# controller CA; enrolment is open but needs a valid token; every other agent
# endpoint needs a client certificate; nothing else answers.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "$EXA_PUBLIC_HOST")
base="https://$host:8443"
loc=(--resolve "$host:8443:127.0.0.1" --max-time 15)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if ss -Htln | awk '{print $4}' | grep -qE '^(0\.0\.0\.0|\*|\[::\]):8443$'; then
  ok "agent gateway listens on 8443"
else
  bad "nothing listens on 8443 on the host"
fi
if curl -fsS "${loc[@]}" -k -o "$tmp/ca.pem" "$base/api/v1/ca.pem"; then ok "CA served"; else bad "no CA from $base"; fi
want=$(jq -r .ca_fingerprint lab/.state/seed.json 2>/dev/null)
have=$(openssl x509 -in "$tmp/ca.pem" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 | tr -d : | tr 'A-F' 'a-f')
if [[ -n $want && $have == "$want" ]]; then
  ok "CA fingerprint matches the one given with enrolment tokens"
else
  note "fingerprints: served ${have:0:16}…, seed ${want:0:16}…"
  bad "CA fingerprint mismatch"
fi
# A strict TLS check against the controller CA, by the public name: what a real agent does.
if curl -fsS "${loc[@]}" --cacert "$tmp/ca.pem" -o /dev/null "$base/api/v1/ca.pem"; then
  ok "gateway certificate is valid for $host"
else
  bad "gateway certificate does not cover $host (delete data/tls to reissue if this persists)"
fi
code() { curl -s -o /dev/null -w '%{http_code}' "${loc[@]}" --cacert "$tmp/ca.pem" "$@"; }
c=$(code -X POST -H 'Content-Type: application/json' -d '{"token":"not-a-token","node_name":"x","csr":"x","wg_public_key":"x"}' "$base/api/v1/enrol")
if [[ $c == 401 || $c == 422 ]]; then ok "enrolment refuses a bad token ($c)"; else bad "enrolment with a bad token answered $c"; fi
c=$(code "$base/api/v1/agent/desired-state")
if [[ $c == 403 ]]; then ok "agent endpoints need a client certificate"; else bad "agent endpoint without a certificate answered $c"; fi
c=$(code "$base/api/v1/auth/me")
if [[ $c == 404 ]]; then ok "the portal API is not served on the gateway"; else bad "portal API on the gateway answered $c"; fi
if curl -fsS --max-time 10 --cacert "$tmp/ca.pem" -o /dev/null "$base/api/v1/ca.pem" 2>/dev/null; then
  ok "$base reachable from this host over the internet"
else
  note "$base not reachable via its public address from this host (some clouds don't hairpin; check from outside)"
fi
# A real agent enrols through the gateway from outside the lab network: a throwaway
# container that reaches the host's published 8443 by the public name, a site made
# for it, then its certificate revoked. The token and keys stay in the container.
echo "-- a real agent through the gateway"
cid=$(customer_id)
sql "DELETE FROM sites WHERE name = 'gw-probe'" >/dev/null
site=$(api POST /sites "{\"customer_id\": \"$cid\", \"name\": \"gw-probe\", \"asn\": 65099, \"overlay_host\": 249}" | jq -r '.id // empty')
tok=$([[ -n $site ]] && api POST /enrolment-tokens "{\"site_id\": \"$site\", \"ttl_hours\": 1}")
fp=$(jq -r '.ca_fingerprint // empty' <<<"$tok")
token=$(jq -r '.token // empty' <<<"$tok")
url=$(jq -r '.public_agent_url // empty' <<<"$tok")
if [[ $url == "$base" ]]; then ok "enrolment tokens give the public gateway ($url)"; else bad "enrolment token gave '${url:-nothing}' as the public gateway"; fi
# shellcheck disable=SC2031  # REPO is set by lib.sh in this shell
probe() {
  docker run --rm --network bridge --add-host "$host:host-gateway" --entrypoint sh \
    -e EXA_ENROL_TOKEN="$token" -v "$REPO/bin/lab:/opt/exa:ro" -v "$tmp:/state" exaconnect/node:dev -c "$1" 2>&1
}
if [[ -n $token ]] && out=$(probe "/opt/exa/exa-agent enrol --state-dir /state --controller $base --ca-fingerprint $fp --name gw-probe") &&
  [[ -s $tmp/client.crt ]]; then
  ok "an agent outside the lab enrolled through $base"
else
  bad "agent enrolment through the gateway failed: $(tail -n 2 <<<"${out:-no output}" | tr '\n' ' ' | cut -c1-200)"
fi
unset token
mtls="curl -s -o /dev/null -w '%{http_code}' --cacert /state/ca.crt --cert /state/client.crt --key /state/client.key $base/api/v1/agent/desired-state"
c=$(probe "$mtls" | tail -n 1)
if [[ $c == 200 || $c == 204 || $c == 404 ]]; then ok "it polls desired state over mutual TLS ($c)"; else bad "desired state over mutual TLS answered $c"; fi
node=$(sql "SELECT id FROM nodes WHERE name = 'gw-probe'")
[[ -n $node ]] && api POST "/nodes/$node/revoke" >/dev/null
c=$(probe "$mtls" | tail -n 1)
if [[ $c == 401 || $c == 403 ]]; then ok "after revoking, its certificate is refused ($c)"; else bad "a revoked certificate answered $c"; fi
sql "DELETE FROM sites WHERE name = 'gw-probe'" >/dev/null
rm -f "$tmp"/*.key
# Last: it trips the limiter for this host's address for a few minutes.
limited=0
for _ in $(seq 1 30); do
  [[ $(code "$base/api/v1/ca.pem") == 429 ]] && { limited=1; break; }
done
if [[ $limited == 1 ]]; then ok "repeated requests are rate limited"; else bad "no rate limit on the gateway"; fi
# shellcheck disable=SC2031  # fail is only set by ok/bad in this shell
exit "$fail"
