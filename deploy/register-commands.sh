#!/bin/bash
# Register the bot's slash commands (run once, again after changing commands). Reads DISCORD_BOT_TOKEN from .env (see set-token.sh).
cd "$(dirname "$(readlink -f "$0")")/.."
t=$(grep '^DISCORD_BOT_TOKEN=' .env 2>/dev/null | tail -1 | cut -d= -f2-)
[ -n "$t" ] || { echo "no DISCORD_BOT_TOKEN in .env, run deploy/set-token.sh first"; exit 1; }
# the token goes to curl through a file descriptor (printf is a shell builtin), never on a command line other users can read in ps
api() { curl -sf -H @<(printf 'Authorization: Bot %s\n' "$t") "$@"; }
# integration_types 0 = installed to a server, contexts 0/1/2 = server, bot DM, other DMs; option types: 3 string, 4 integer
cmd() { printf '{"name":"%s","description":"%s","integration_types":[0],"contexts":[0,1,2]}' "$1" "$2"; }
UCIT='{"name":"ucit","description":"Přidat blok studia do kalendáře Škola","integration_types":[0],"contexts":[0,1,2],"options":[{"type":3,"name":"predmet","description":"Předmět, např. ISA","required":true},{"type":3,"name":"datum","description":"Datum, např. 3.11.","required":true},{"type":3,"name":"od","description":"Začátek, např. 16:00 (výchozí 16:00)"},{"type":4,"name":"hodin","description":"Délka v hodinách (výchozí 2)","min_value":1,"max_value":12}]}'
AI='{"name":"ai","description":"Zeptat se ZHelperu","integration_types":[0],"contexts":[0,1,2],"options":[{"type":3,"name":"otazka","description":"Otázka nebo úkol","required":true}]}'
body="[$(cmd dnes 'Obnovit dnešní přehled'),$(cmd sync 'Plná synchronizace Studis + Moodle'),$(cmd tyden 'Přehled na příští týden'),$(cmd motd 'Zobrazit dnešní přehled'),$(cmd zitra 'Rozvrh na zítra'),$(cmd body 'Body z předmětů tento semestr'),$(cmd terminy 'Registrace termínů zkoušek'),$(cmd stav 'Poslední a další běhy synchronizace'),$UCIT,$AI]"
app=$(api https://discord.com/api/v10/applications/@me | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])') || { echo "bad token"; exit 1; }
api -X PUT -H "Content-Type: application/json" -d "$body" "https://discord.com/api/v10/applications/$app/commands" > /dev/null && echo "commands registered"
# the bot receives interactions over the gateway, so no HTTP endpoint may be set
echo '{"interactions_endpoint_url": null}' | api -X PATCH -H "Content-Type: application/json" -d @- https://discord.com/api/v10/applications/@me > /dev/null
