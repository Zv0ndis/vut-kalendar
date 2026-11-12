#!/bin/bash
# Usage: run.sh full|refresh|weekly|alerts|evening   (called by the studis@ systemd units)
# Dependencies are pinned in <script>.lock (uv run --locked); upgrade with `uv lock --script <script> --upgrade`.
set -uo pipefail
mode=${1:-refresh}
cd "$(dirname "$(readlink -f "$0")")/.."
export PATH=$HOME/.local/bin:$PATH
# one run at a time; the 5-minute alerts tick skips while another run holds the lock
exec 9>.fetch.lock
if [ "$mode" = alerts ]; then flock -n 9 || { echo "another run holds the lock, skipping"; exit 0; }; else flock 9; fi

run() {
  case $mode in
    alerts) uv run --locked alerts.py; return ;;
    weekly) uv run --locked digest.py --weekly; return ;;
    evening) uv run --locked digest.py --evening; return ;;
  esac
  # a failed fetch still syncs/posts from the previous files, then reports the failure
  if [ "$mode" = full ]; then uv run --locked fetch_studis.py || fetch_failed=1; uv run --locked sync.py || return 1; fi
  uv run --locked digest.py $([ "$mode" = full ] && echo --post) || return 1
  [ -z "${fetch_failed:-}" ]
}
fail() {  # alerts: at most one failure DM per hour, so a broken 5-minute timer can't spam
  if [ "$mode" = alerts ]; then
    [ -n "$(find .alerts-fail -mmin -60 2>/dev/null)" ] && return
    touch .alerts-fail
  fi
  # not --locked: the DM must still go out when a stale lock file is what broke the run
  uv run digest.py --fail "run.sh $mode (journalctl -u studis@$mode)"
}
run || { fail; exit 1; }
