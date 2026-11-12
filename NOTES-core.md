# Core notes (for the bot and README authors)

Stable interface: `sync.py` exports `PRAGUE`, `CAL_NAME`, `service`, `list_events`, `reminders`, `extra_events`, `points_left`, `EXAM`
(and `SYNCED`, `COURSE`, `ZAPOC`, `holidays`). `fetch_studis.py` keeps its `parse_*` helpers. Files stay `studis.json`,
`studis-timetable.ics`, `studis-exams.ics`, `studis-moodle.ics`. Nothing was renamed.

## Environment variables of the core

| Variable | Meaning | Default |
|---|---|---|
| `VUT_USER`, `VUT_PASS` | Studis login (scraper: exam registrations, deadlines, course items) | required |
| `CALENDAR_NAME` | name of the Google calendar the tool owns (`sync.CAL_NAME`) | `Škola` |
| `SKIP_TITLES` | regex of event titles to leave out | none |
| `ROOM_URL` | room page template, `{}` = room (only FIT `B/` rooms are linked); `off` disables | FIT room page |
| `MOODLE_ICS_URL` | Moodle calendar export URL | none |
| `VUT_API_CLIENT_ID`, `VUT_API_SECRET` | official VUT API (optional; without it everything is scraped, no warnings) | none |
| `VUT_TOKEN_FILE` | where `vutapi.py` caches the API token | `.vut-token.json` next to the code |

Empty values count as unset (GitHub Actions passes empty strings for missing secrets).

## Commands
- `uv run sync.py --login` Google consent in a browser, writes `token.json` (paste it into the `GOOGLE_TOKEN` secret).
- `uv run sync.py [--dry-run] [--adopt] [--force]`; a tripped mass-delete brake DMs through `digest.send`, and fails the run when no Discord is configured.
- `uv run fetch_studis.py [--install-browser]`; `--install-browser` installs the Chromium matching the locked Playwright (Actions).

State written locally and git-ignored: `api-failures.json`, `.vut-token.json`, `studis*.json|ics`.

## Changes after review
- **Event ownership:** every event the script inserts carries `extendedProperties.private.src = "vut-kalendar"` (`sync.TAG`). Only tagged events are
  ever patched or deleted (`sync.synced(ev)`); untagged events are hand-made and kept whatever their title, even duplicates of a wanted key.
  The old title-shape regex `SYNCED` is gone. `sync.py --adopt` tags the events it moves into the calendar.
- **Workflow input:** `sync.yml` has a manual `workflow_dispatch` boolean input `force`; when true it runs `sync.py --force`.
  A first step fails early with a clear message when `VUT_USER`, `VUT_PASS`, `GOOGLE_CLIENT_SECRET` or `GOOGLE_TOKEN` is empty.
  `CALENDAR_NAME`, `SKIP_TITLES`, `ROOM_URL` are read from repository variables or secrets.
- **Faculties:** the VUT API works for any faculty. The scraper handles the timetable and exam terms for any faculty; the Harmonogram periods
  accept any faculty suffix except the university-wide `(VUT)` one. Room links are FIT only (`B/` rooms).
- **Course codes:** all parsing uses `sync.CODE` (`[A-Z0-9][A-Z0-9-]{1,7}`), so `BPC-ABC` works.
- **fetch_studis.py exit code:** 1 only when no timetable could be fetched (or the scraper itself failed); empty points/periods only warn,
  an empty timetable is written as a valid empty calendar, a Moodle failure or a missing exam-export link is a warning.
- **sync.py** reads `.env` next to it itself (a real environment variable wins), pages through `calendarList`, and prints
  `FAILED: ...` (no traceback) when the Google token is missing, empty or expired.
