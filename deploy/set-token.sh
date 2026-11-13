#!/bin/bash
# Prompt for the Discord bot token (hidden input), store it in .env at the repo root, restart the bot if its unit exists.
cd "$(dirname "$(readlink -f "$0")")/.."
umask 077  # .env.new is never readable by others, not even for a moment
read -rsp "Discord bot token: " t; echo
[ -n "$t" ] || exit 1
touch .env && chmod 600 .env
{ grep -v '^DISCORD_BOT_TOKEN=' .env; printf 'DISCORD_BOT_TOKEN=%s\n' "$t"; } > .env.new && mv .env.new .env && chmod 600 .env
echo "token saved to .env"
if systemctl cat zhelper > /dev/null 2>&1; then
  sudo systemctl restart zhelper && sleep 10 && systemctl is-active zhelper
fi
