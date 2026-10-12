#!/usr/bin/env bash
# Creates or completes .env with generated secrets. Existing values are kept.
# Nothing is printed; read the admin password on the host with:
#   grep EXA_ADMIN_PASSWORD .env
set -euo pipefail
cd "$(dirname "$0")/../.."
touch .env
chmod 600 .env
add() { grep -q "^$1=" .env || echo "$1=$2" >> .env; }
add POSTGRES_PASSWORD "$(openssl rand -hex 16)"
add EXA_PROXY_SECRET "$(openssl rand -hex 24)"
add EXA_PBX_SECRET "$(openssl rand -hex 24)"
# Jibsy Phone's Web Push key (ADR 0045): a P-256 key, base64 PKCS#8, only ever in this file.
add EXA_PUSH_VAPID_KEY "$(openssl ecparam -name prime256v1 -genkey -noout 2>/dev/null | openssl pkcs8 -topk8 -nocrypt -outform DER | base64 -w0)"
add EXA_ADMIN_EMAIL "admin@exacarib.local"
add EXA_ADMIN_PASSWORD "$(openssl rand -base64 18 | tr -d '/+=')"
echo ".env ready (admin: $(grep '^EXA_ADMIN_EMAIL=' .env | cut -d= -f2))"
