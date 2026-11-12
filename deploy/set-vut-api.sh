#!/bin/bash
# Asks for the VUT API client (https://id.vut.cz/my-account/client/), stores it in .env next to this script and tests it.
set -u
env="$(dirname "$(readlink -f "$0")")/../.env"
read -rp "VUT API client_id (Klient UID): " id
read -rsp "VUT API client_secret: " s; echo
[ -n "$id" ] && [ -n "$s" ] || exit 1
touch "$env"; chmod 600 "$env"
sed -i '/^VUT_API_CLIENT_ID=/d;/^VUT_API_SECRET=/d' "$env"
printf 'VUT_API_CLIENT_ID=%s\nVUT_API_SECRET=%s\n' "$id" "$s" >> "$env"
r=$(printf 'user = "%s:%s"\n' "$id" "$s" | curl -s -o /dev/null -w '%{http_code}' -K - -d grant_type=client_credentials https://id.vut.cz/auth/common/oauth2/token)  # the secret goes through stdin, not the process list
echo "uloženo. test tokenu: HTTP $r (200 = funguje; cokoli jiného = zkontroluj klienta)"
