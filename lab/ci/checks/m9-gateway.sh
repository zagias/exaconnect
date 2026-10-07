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
limited=0
for _ in $(seq 1 30); do
  [[ $(code "$base/api/v1/ca.pem") == 429 ]] && { limited=1; break; }
done
if [[ $limited == 1 ]]; then ok "repeated requests are rate limited"; else bad "no rate limit on the gateway"; fi
if curl -fsS --max-time 10 --cacert "$tmp/ca.pem" -o /dev/null "$base/api/v1/ca.pem" 2>/dev/null; then
  ok "$base reachable from this host over the internet"
else
  note "$base not reachable via its public address from this host (some clouds don't hairpin; check from outside)"
fi
# shellcheck disable=SC2031  # fail is only set by ok/bad in this shell
exit "$fail"
