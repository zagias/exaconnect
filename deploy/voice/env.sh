#!/usr/bin/env bash
# Completes .env for the public voice host before a release, so the controller
# and FreeSWITCH start with it. Existing values are kept, so a hand edit wins.
#   EXA_VOICE_PUBLIC_IP: the address carriers and browsers send call audio to,
#     the public host's own address (deploy/public/site.env EXA_PUBLIC_HOST).
#   EXA_VERTO_URL: the browser phone's sign-in, wss://<public host>/verto.
# Does nothing on a host without a public site.
set -euo pipefail
cd "$(dirname "$0")/../.."
host=$(grep -m1 '^EXA_PUBLIC_HOST=' deploy/public/site.env 2>/dev/null | cut -d= -f2- || true)
[[ -n $host ]] || exit 0
touch .env
add() { grep -q "^$1=." .env || { sed -i "/^$1=/d" .env && echo "$1=$2" >>.env; }; }
ip=$(getent ahostsv4 "$host" | awk 'NR==1{print $1}')
# Only a public address: a hosts-file entry pointing the name at the host itself is no use.
if [[ -n $ip && ! $ip =~ ^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
  add EXA_VOICE_PUBLIC_IP "$ip"
fi
add EXA_VERTO_URL "wss://$host/verto"
