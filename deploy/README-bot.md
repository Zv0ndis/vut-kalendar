# Discord bot and scheduled runs (technical notes)

Optional layer on top of the sync. One bot per person: it answers a single Discord user (`DISCORD_USER_ID`) and ignores everyone else. Needs a Linux machine with systemd that is always on (a free VM works) and `uv` installed.

## What it does
- `digest.py`: daily digest, evening preview ("tomorrow"), weekly overview, failure DMs.
- `alerts.py`: every 5 minutes checks the local files and DMs about exam registrations opening, free seats, new points, thresholds, reminders. Quiet hours hold the less urgent ones.
- `zhelper/bot.py`: slash commands `/dnes /sync /tyden /motd /zitra /body /terminy /stav /ucit /ai` and plain DMs (AI chat).
- `zhelper/agent.py`: optional AI chat. Without `ANTHROPIC_API_KEY` it answers that chat is disabled; every other command works. Write actions (add/move/delete event, study blocks) are only prepared and run after the user replies yes.
- `spend.py`: daily spend cap for the AI chat.

Timezone is fixed to Europe/Prague. Dependencies are pinned per script in the `*.lock` files (`uv run --locked`).

## Env variables (repo-root `.env`, chmod 600)
| Variable | Needed | Meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | bot token; set with `deploy/set-token.sh` (hidden input) |
| `DISCORD_USER_ID` | yes | the one user the bot talks to |
| `DIGEST_CALENDARS` | no | comma-separated extra calendars (by name, `primary` = main calendar) in the digest's per-calendar "today"; default only the school calendar |
| `QUIET_FROM`, `QUIET_TO` | no | quiet hours in Prague time, default 22 and 8; only registration-opens, seat alerts and reminders go out, the rest is sent as one message when quiet ends |
| `ANTHROPIC_API_KEY` | no | enables AI chat |
| `ZHELPER_MODEL` | no | default `claude-haiku-5-5` |
| `ZHELPER_DAILY_USD` | no | daily cap in USD, default `1.00` |
| `ZHELPER_PRICES` | no | override per-token prices used for the cap (see `spend.py`) |
| `CALENDAR_NAME` and the rest | no | belong to the core sync, see `.env.example` |

Template: `deploy/bot.env.example`. Invalid numbers in `QUIET_*` / `ZHELPER_DAILY_USD` fall back to the defaults with a warning.

## Discord setup
1. discord.com/developers -> New Application -> Bot -> Reset Token (keep it secret).
2. Intents: none privileged. The bot uses only DM messages, which need no Message Content intent.
3. Install: OAuth2 -> URL generator, scope `bot` (and `applications.commands`), no permissions needed. Open the URL and add the bot to a server you share with it (Discord requires a shared server before it can DM you), or use user install.
4. Your user ID: Settings -> Advanced -> Developer Mode, then right-click yourself -> Copy User ID.
5. Registering commands: `deploy/register-commands.sh` (asks for the token, hidden; run it again after changing commands). It also clears any interactions endpoint, the bot receives them over the gateway.

## Prerequisites
`uv`, `curl`, `python3`, `flock` (util-linux), `sudo`, systemd, and a working core sync (Google login done, `.env` filled with the core variables). Tested on Ubuntu; other systemd distributions should work. Playwright's Chromium is installed by `install.sh` only where `apt-get` exists, elsewhere it prints what to do.

## Install
Run as your normal user (not root) from the repo root, in this order:

    deploy/set-token.sh            # hidden token prompt, writes .env (restarts the bot only if its unit already exists)
    deploy/register-commands.sh    # reads the token from .env
    deploy/install.sh              # units with your user, path and uv; sudoers rule; browser; timers
    sudo systemctl enable --now zhelper

`install.sh` does not start the timers until `.env` has `DISCORD_BOT_TOKEN`; if it is missing, run `set-token.sh` and `install.sh` again. The sudoers rule (`/etc/sudoers.d/zhelper`, validated with `visudo -cf` before it is installed) lets the bot start `studis@*` runs, read their journal and restart itself without a password. Logs: `journalctl -u zhelper`, `journalctl -u studis@alerts`.

`.env` values are used as written: put comments on their own line, never after a value (systemd's `EnvironmentFile` keeps the trailing comment as part of the value). The scripts also read `.env` themselves, so they work when run by hand; real environment variables win.

SELinux (Fedora and similar): if the bot fails with status 203/EXEC, the cause is `uv` living in the home directory (label `user_home_t`). Install `uv` system-wide (for example in `/usr/local/bin`) or relabel it with `chcon -t bin_t`.

## Timers (units in `deploy/`, template `studis@.service` runs `deploy/run.sh <mode>`)
| Timer | Schedule | Mode |
|---|---|---|
| `studis-alerts` | every 5 min | `alerts` (skips if another run holds the lock) |
| `studis-refresh` | hourly at :33 UTC | `refresh`: rebuild and show the digest from current files |
| `studis-full` | 05:15 and 16:15 UTC | `full`: fetch Studis, sync calendar, post the digest |
| `studis-evening` | 20:00 Prague | `evening`: tomorrow preview |
| `studis-weekly` | Sunday 17:00 UTC | `weekly`: next week overview |

`run.sh` serializes runs with `.fetch.lock`, and on failure DMs the error (alerts: at most once an hour). Placeholders in the unit files: `@USER@`, `@DIR@`, `@UV@`, `@UVDIR@`. State files (`digest-state.json`, `alerts-state.json`, `points-seen.json`, `zhelper-notes.json`) stay local on the machine and are git-ignored by the core.

## Dependencies on the core
Shared helper: `envutil.py` (loads `.env`, parses numeric env vars with a fallback to the default and a warning). Imports `sync.PRAGUE`, `sync.CAL_NAME`, `sync.service`, `sync.list_events`, `sync.reminders`, `sync.extra_events`, `sync.points_left`, `sync.EXAM`, and runs `fetch_studis.py` and `sync.py` as scripts.

## Self-tests
`uv run alerts.py --selftest`, `uv run digest.py --selftest`, `cd zhelper && uv run agent.py --selftest`, and `uv run zhelper/bot.py --selftest`.
