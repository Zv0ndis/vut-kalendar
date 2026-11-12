#!/bin/bash
# Install the systemd units for the current user and this checkout. Run as your normal user (it uses sudo itself).
# Order: set-token.sh -> register-commands.sh -> install.sh -> sudo systemctl enable --now zhelper
set -euo pipefail
[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root (the script calls sudo where needed)"; exit 1; }
for c in curl python3 flock sudo uv systemctl journalctl visudo; do
  command -v "$c" > /dev/null || { echo "missing prerequisite: $c"; exit 1; }
done
dir=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
uv=$(command -v uv)
sc=$(command -v systemctl); jc=$(command -v journalctl)
# sed replacement-safe values; % doubled for systemd specifiers
esc() { printf '%s' "${1//%/%%}" | sed -e 's/[&|\\]/\\&/g'; }
for f in "$dir"/deploy/studis@.service "$dir"/deploy/studis-*.timer "$dir"/deploy/zhelper.service; do
  sed -e "s|@USER@|$USER|g" -e "s|@DIR@|$(esc "$dir")|g" -e "s|@UV@|$(esc "$uv")|g" -e "s|@UVDIR@|$(esc "$(dirname "$uv")")|g" "$f" | sudo tee "/etc/systemd/system/$(basename "$f")" > /dev/null
done
# the bot (runs as you) starts runs, reads their journal and restarts itself without a password prompt
tmp=$(mktemp)
printf '%s ALL=(root) NOPASSWD: %s start --no-block studis@*, %s restart zhelper, %s -u studis@* *\n' "$USER" "$sc" "$sc" "$jc" > "$tmp"
sudo visudo -cf "$tmp" && sudo install -m 440 -o root -g root "$tmp" /etc/sudoers.d/zhelper
rm -f "$tmp"
sudo systemctl daemon-reload
# Chromium for fetch_studis.py; --with-deps is apt-only
if command -v apt-get > /dev/null; then
  (cd "$dir" && uv run --locked fetch_studis.py --install-browser) || echo "browser install failed, rerun: uv run --locked fetch_studis.py --install-browser"
else
  echo "no apt-get: install Chromium's system libraries with your package manager, then run: uv run --locked fetch_studis.py --install-browser (its --with-deps step will fail, install the browser with 'uv run --locked --with playwright playwright install chromium' instead)"
fi
if grep -q '^DISCORD_BOT_TOKEN=.' "$dir/.env" 2>/dev/null; then
  sudo systemctl enable --now studis-{alerts,evening,full,refresh,weekly}.timer
  echo "timers enabled; start the bot with: sudo systemctl enable --now zhelper"
else
  echo "no DISCORD_BOT_TOKEN in .env: timers NOT started. Run deploy/set-token.sh, then rerun deploy/install.sh"
fi
