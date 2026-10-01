#!/usr/bin/env bash
# The public portal (deploy/public): HTTPS on the public name, the portal and
# its API served, agent endpoints and the controller port not exposed.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "$EXA_PUBLIC_HOST")
loc=(--resolve "$host:443:127.0.0.1" --max-time 15)

if curl -fsS "${loc[@]}" -k "https://$host/healthz" | grep -q ok; then ok "https://$host/healthz answers on this host"; else bad "no HTTPS answer for $host"; fi
issuer=$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$host" 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null)
if [[ $issuer == *"Let's Encrypt"* ]]; then
  ok "certificate from Let's Encrypt ($issuer)"
else
  bad "no Let's Encrypt certificate yet (issuer: ${issuer:-none}). Inbound 80 and 443 must be open in the cloud firewall."
fi
if curl -fsS "${loc[@]}" -k "https://$host/" | grep -q '<div id="root">'; then ok "portal served"; else bad "portal not served"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' "${loc[@]}" -k -X POST "https://$host/api/v1/enrol")
if [[ $code == 404 ]]; then ok "agent endpoints not served publicly"; else bad "enrol answered $code publicly"; fi
if curl -s -o /dev/null -w '%{http_code}' "${loc[@]}" -k "https://$host/api/v1/auth/me" | grep -q 401; then ok "API answers and needs sign-in"; else bad "API through the public proxy"; fi
if curl -fsS --max-time 10 -o /dev/null "http://$host/" 2>/dev/null || curl -fsS --max-time 10 -o /dev/null "https://$host/healthz" 2>/dev/null; then
  ok "$host reachable from this host over the internet"
else
  note "$host not reachable via its public address from this host (some clouds don't hairpin; check from outside)"
fi
ports=$(ss -Htln | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|172\.)' | grep -oE '[0-9]+$' | sort -un | tr '\n' ' ')
note "listening on public addresses: $ports"
if grep -qE '(^| )(8000|5432|5173)( |$)' <<<"$ports"; then bad "controller, database or dev portal exposed"; else ok "controller, database and dev portal stay private"; fi
exit $fail
