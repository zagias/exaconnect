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
add EXA_ADMIN_EMAIL "admin@exacarib.local"
add EXA_ADMIN_PASSWORD "$(openssl rand -base64 18 | tr -d '/+=')"
echo ".env ready (admin: $(grep '^EXA_ADMIN_EMAIL=' .env | cut -d= -f2))"
