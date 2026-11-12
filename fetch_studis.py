# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
"""Refresh studis-timetable.ics, studis-exams.ics, studis-moodle.ics and studis.json.

Hybrid: the official VUT IS API (vutapi.py; VUT_API_CLIENT_ID / VUT_API_SECRET) supplies the classes, reservations, registered exam terms,
points and semester periods. Studis is scraped headlessly (VUT_USER / VUT_PASS, in the environment or .env) only for what the API does not
have (course-card items, registration windows and seats, assignment deadlines) and as a fallback for a piece the API failed to deliver.
A failing scrape still writes the API pieces (previous values stay for the scraped ones) and exits 1 with a page dump.
"""
import json, os, re, sys, tempfile, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
import vutapi

HERE = Path(__file__).parent
BASE = "https://www.vut.cz/studis/student.phtml"
ICS_URL = BASE + "?sn=osobni_rozvrh&ics=1"
PRAGUE = ZoneInfo("Europe/Prague")
STREET = " (Božetěchova 1/2, Brno 61200)"  # what Studis appends to FIT room labels in class events, then " [label]"
MONTHS = "ledna února března dubna května června července srpna září října listopadu prosince".split()


def cz_date(s, year=None):
    """'ne 22. listopadu 2026 23:59' -> '2026-11-22T23:59'; a date without a year takes `year`; None when there is no date and time"""
    m = re.search(r"(\d+)\. (\S+)(?: (\d{4}))? (\d+):(\d+)", s)
    if not m or m[2] not in MONTHS or not (m[3] or year):
        return None
    return datetime(int(m[3] or year), MONTHS.index(m[2]) + 1, int(m[1]), int(m[4]), int(m[5])).strftime("%Y-%m-%dT%H:%M")


def parse_periods(text):
    """Teaching and exam periods of your faculty from Harmonogram: [{"name", "start", "end"}]. Any faculty suffix counts except (VUT), which is
    university-wide; if the page lists several faculties, the first one found for each period wins."""
    out, lines = [], [l.strip() for l in text.splitlines()]
    for i, l in enumerate(lines):
        m = re.match(r"^(OBDOBÍ VÝUKY V (ZIMNÍM|LETNÍM) SEMESTRU|ZKOUŠKOVÉ OBDOBÍ (ZIMNÍHO|LETNÍHO) SEMESTRU) \((?!VUT\))\w+\)$", l)
        t = next((x for x in lines[i + 1:i + 4] if x.startswith("Termín")), "") if m else ""
        d = re.findall(r"(\d+)\.(\d+)\.(\d{4})", t)
        name = ("výuka" if l.startswith("OBDOBÍ") else "zkouškové") + (" ZS" if "ZIM" in l else " LS")
        if m and len(d) == 2 and not any(p["name"] == name for p in out):
            out.append({"name": name, "start": f"{d[0][2]}-{int(d[0][1]):02}-{int(d[0][0]):02}", "end": f"{d[1][2]}-{int(d[1][1]):02}-{int(d[1][0]):02}"})
    return out


DAY = r"(?:(?:po|út|st|čt|pá|so|ne) )?"
WINDOW = re.compile(rf"od {DAY}(\d+\. \S+(?: \d{{4}})? \d+:\d+) do {DAY}((?:\d+\. \S+(?: \d{{4}})? )?\d+:\d+)")


def parse_window(l, start):
    """'... od út 29. prosince 2026 9:00 do 9:00' -> (opens, closes) ISO or (None, None). `start` is the term's own start: a time-only end is
    the day BEFORE it (Studis drops the date, so the safer reading: a warning a day early beats one after closing), a date without a year takes the year of the term (the opening rolls back a year if it would fall after the term)."""
    w = WINDOW.search(l)
    if not w:
        return None, None
    y = int(start[:4])
    opens = cz_date(w[1], y)
    if opens and opens > start:
        opens = cz_date(w[1], y - 1)
    closes = f"{(datetime.fromisoformat(start) - timedelta(days=1)).date()}T{int(w[2].split(':')[0]):02}:{w[2].split(':')[1]}" if re.fullmatch(r"\d+:\d+", w[2]) else cz_date(w[2], y)
    if closes and opens and closes < opens:  # no year given and the window runs over New Year
        closes = cz_date(w[2], y + 1)
    return (opens, closes) if opens and closes else (None, None)


def parse_terms(text):
    """Exam terms from 'Registrace termínů': one entry per term (a course/kind has several), with its registration status and window."""
    terms, cur = [], None
    kind = None
    for l in (x.strip() for x in text.splitlines()):
        m = re.match(r"^([A-Z0-9][A-Z0-9-]{1,7}) – .+ \((.+)\)$", l)
        if m:
            kind, cur = (m[1], m[2]), None
            continue
        if not kind:
            continue
        d = re.match(r"^(po|út|st|čt|pá|so|ne) (\d+\. \S+ \d{4} \d+:\d+)", l)
        if d:
            if x := cz_date(d[2]):
                cur = {"course": kind[0], "kind": kind[1], "registered": False, "opens": None, "closes": None, "free": False, "dates": [x]}
                terms.append(cur)
        elif cur and l.startswith("registrován"):
            cur["registered"] = True
        elif cur and l.startswith("neregistrován"):
            cur["opens"], cur["closes"] = parse_window(l, cur["dates"][0])
            if "plný" not in l:
                cur["free"] = True
    return [t for t in terms if t["opens"] or t["registered"]]


def parse_deadlines(text):
    """Registered assignments on 'Registrace zadání' that still have a submission deadline."""
    out, block = [], None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([A-Z0-9][A-Z0-9-]{1,7}) – .+ - .+ \(.+\)$", line)
        if m:
            block = {"course": m[1], "title": None, "due": None, "reg": False}
            out.append(block)
        elif block and block["title"] is None:
            block["title"] = line
        elif block and "zadání odevzdat do" in line:
            block["due"] = cz_date(line)  # None -> entry dropped below
        elif block and block["title"] and line.startswith(block["title"] + ", registrováno"):
            block["reg"] = True
    return [{"course": b["course"], "title": b["title"], "due": b["due"]} for b in out if b["reg"] and b["due"]]


def parse_points(c):
    """'12,5' -> 12.5, '12' -> 12, anything else -> None"""
    try:
        v = float(c.replace(",", ".").strip())
        return int(v) if v == int(v) else v
    except (ValueError, OverflowError):  # "-", "nan", "inf"
        return None


def parse_items(rows):
    """'Dílčí hodnocení' item rows of a predmet_detail page -> [{"name", "min", "max", "got"}]; sub-rows and headers skipped"""
    return [{"name": m[1], "min": parse_points(r[2]), "max": parse_points(r[3]), "got": parse_points(r[4])}
            for r in rows if len(r) >= 5 and (m := re.match(r"^\d+\. (.+)$", r[0]))]


def collect_items(details, prev, load):
    """{course: items} via load(url); a course whose card fails keeps its previous items, so a transient error never changes the data."""
    items = {}
    for code, url in details:
        try:
            items[code] = load(url)
        except Exception as e:  # a missing breakdown is no reason to drop the whole fetch
            print(f"items {code}: {e}", file=sys.stderr)
            if code in prev:
                items[code] = prev[code]
    return items


def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


class ScrapeError(Exception):
    pass


def dump(pg, why):
    print(f"FAILED: {why}\nURL: {pg.url}", file=sys.stderr)
    for e in pg.query_selector_all("input, button, a"):
        print(" ", e.evaluate("e=>[e.tagName,e.type||'',e.name||'',(e.textContent||'').trim().slice(0,50)].join('|')"), file=sys.stderr)
    print(re.sub(r"\s+", " ", pg.inner_text("body"))[:600], file=sys.stderr)
    raise ScrapeError(why)


# ---------- official API -> the same files the scraper writes ----------

def utc(day, tod):
    """API local date '2026-11-12T00:00:00' + time of day '1899-12-30T15:00:00' -> '20261112T140000Z'. None or garbage raises (the caller skips that event)."""
    return utc_dt(day[:10] + "T" + tod[11:19])


def utc_dt(s):
    """API datetime (Prague local) -> '20261208T130000Z'. One that carries its own offset/Z is converted, not re-read as Prague time."""
    d = datetime.fromisoformat(s)
    return (d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=PRAGUE).astimezone(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def ics_text(events):
    """[(start, end, summary, location)] with '...Z' times -> VCALENDAR text that sync.parse_ics reads like the Studis export."""
    def esc(t):  # iCal TEXT: backslash, comma, semicolon and newlines escaped, any other control character gone
        t = re.sub(r"([\\,;])", r"\\\1", t.replace("\t", " "))
        return re.sub(r"[\x00-\x1f\x7f]", "", re.sub(r"\r\n?|\n", r"\\n", t))
    return "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:vutapi\n" + "".join(
        f"BEGIN:VEVENT\nUID:{s}-{i}@vutapi\nDTSTART:{s}\nDTEND:{e}\nSUMMARY:{esc(t)}\n" + (f"LOCATION:{esc(loc)}\n" if loc else "") + "END:VEVENT\n"
        for i, (s, e, t, loc) in enumerate(events)) + "END:VCALENDAR\n"


def room_text(labels, address=True):
    """Room labels -> location text. One FIT room keeps the Studis shape 'B/D0206 (Božetěchova 1/2, Brno 61200) [B/D0206]' (the key of the Google event);
    several rooms (a lecture held in two halls) are 'B/D0206 & B/D105 (Božetěchova 1/2, Brno 61200)'."""
    fit = address and bool(labels) and all(l.startswith("B/") for l in labels)
    if len(labels) == 1:
        return labels[0] + (f"{STREET} [{labels[0]}]" if fit else "")
    return " & ".join(labels) + (STREET if fit else "")


def day_rooms(resp):
    """mistnosti/podrobne response -> {vyuc_blok_den_id: [room label]} without cancelled rooms."""
    return {d["vyuc_blok_den_id"]: [m["mistnost_label"] for m in d["mistnosti"] if not m["zruseno"]]
            for v in resp["data"]["vyucovani"] for b in v["bloky"] for d in b["dny"]}


def timetable_events(vyucovani, rooms, reservations, now):
    """Personal timetable (rozvrh/osobni/vyucovani + rezervace) -> [(start, end, summary, location)]; cancelled days and events already over are left out.
    An event with a missing/garbled time or an end before its start is skipped (with a warning), not allowed to fail the whole piece."""
    out, bad = [], 0
    for v in vyucovani:
        for b in v["bloky"]:
            for d in b["dny"]:
                if d["zruseno"] or d["vbd_zruseno"] or d["osoba_zruseno"]:
                    continue
                try:
                    s, e = utc(d["vbd_datum"], b["vb_cas_od"]), utc(d["vbd_datum"], b["vb_cas_do"])
                except (TypeError, ValueError):
                    bad += 1
                    continue
                if e < s:
                    bad += 1
                    continue
                out.append((s, e, f"{v['v_p_zkratka']} ({v['v_trj_zkratka']})", room_text(rooms.get(d["vbd_vyuc_blok_den_id"], []))))
    for r in reservations:  # Studis exports one reservation as a weekly series; the API has only the real dates
        if r["zruseno"]:
            continue
        what = (r["popis_verejny"] or r["popis"] or r["mistnost_label"] or "").strip()
        try:
            s, e = utc(r["datum"], r["cas_od"]), utc(r["datum"], r["cas_do"])
        except (TypeError, ValueError):
            bad += 1
            continue
        if e < s:
            bad += 1
            continue
        out.append((s, e, f"{what[:1].upper()}{what[1:]} (rezervace)", r["mistnost_label"] or ""))
    if bad:
        print(f"warning: {bad} timetable entries skipped (missing time or end before start)", file=sys.stderr)
    cut = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return sorted(x for x in out if x[1] > cut)


def exam_events(terms, rooms, year):
    """rozvrh/osobni/terminy (the terms you are registered in) of academic year `year` -> [(start, end, summary, location)] in the Studis export's shape.
    A term with exactly one room gets it as the location (the export had none); a term with a missing/garbled time or end before start is skipped."""
    out = []
    for t in terms:
        if not (t["aktualni_predmety"] and t["aktualni_predmety"][0]["ap_rok"] == year):
            continue
        try:
            s, e = utc_dt(t["zacatek_zkousky"]), utc_dt(t["konec_zkousky"])
        except (TypeError, ValueError):
            print(f"warning: exam term {t.get('termin_id')} skipped (bad time)", file=sys.stderr)
            continue
        if e < s:
            continue
        out.append((s, e, f"{t['aktualni_predmety'][0]['p_zkratka']}({t['zp_typ_hodnoceni_popis']}) - {t['zkousejici_label_pr']} [/]",
                    room_text(rooms[t["termin_id"]]) if len(rooms.get(t["termin_id"], [])) == 1 else ""))
    return sorted(out)


def api_points(index):
    """index/v1 records -> {course: points}: the newest year of a course wins; None = not graded yet."""
    out = {}
    for r in sorted(index, key=lambda r: -r["akrok"]):
        v = r["bodove_hodnoceni"]
        out.setdefault(r["zkratka"], v if v is None or v != int(v) else int(v))
    return out


def api_periods(weeks):
    """rozvrh/moje/tydny weeks of the newest academic year -> [{"name", "start", "end"}] (výuka/zkouškové ZS/LS), like the Harmonogram page."""
    year, out = max(w["akademicky_rok"] for w in weeks), {}
    for w in (w for w in weeks if w["akademicky_rok"] == year):
        k = (w["semestr_zkratka"], w["zkouskove"])
        a, b = w["real_datum_od"][:10], w["real_datum_do"][:10]
        out[k] = (min(out[k][0], a), max(out[k][1], b)) if k in out else (a, b)
    return [{"name": ("zkouškové" if z else "výuka") + f" {sem}S", "start": a, "end": b} for (sem, z), (a, b) in sorted(out.items(), key=lambda x: x[1])]


def fetch_timetable(now):
    today = now.date().isoformat()
    vyuc = vutapi.get("/api/rozvrh/osobni/vyucovani/v4", datum_od=today, datum_do=(now + timedelta(days=270)).date().isoformat())["data"].get("vyucovani", [])
    rooms = {}
    for v in vyuc:
        rooms.update(day_rooms(vutapi.get(f"/api/rozvrh/vyucovani/{v['v_vyucovani_id']}/mistnosti/podrobne/v1")))
        if not v.get("v_webinar_link") and not any(rooms.get(d["vbd_vyuc_blok_den_id"]) for b in v["bloky"] for d in b["dny"]):
            raise vutapi.ApiError(f"no rooms for {v['v_p_zkratka']} ({v['v_trj_zkratka']})")  # events without a location would re-key every event in Google
    resv = [r for r in vutapi.get("/api/rozvrh/osobni/rezervace/v1")["data"].get("rezervace", []) if r["datum"][:10] >= today]
    return ics_text(timetable_events(vyuc, rooms, resv, now))  # empty before the timetable is published: a valid empty calendar (sync.py's brake guards the deletes)


def fetch_exams(now):
    terms = vutapi.get("/api/rozvrh/osobni/terminy/v3")["data"].get("terminy", [])
    if not terms:
        raise vutapi.ApiError("no exam terms")
    year = max(t["aktualni_predmety"][0]["ap_rok"] for t in terms if t["aktualni_predmety"])
    rooms = {t["termin_id"]: [m["mistnost_label"] for m in vutapi.get(f"/api/rozvrh/termin/{t['termin_id']}/mistnosti/v2")["data"]["terminy"][0]["mistnosti"]]
             for t in terms if t["konec_zkousky"] >= now.strftime("%Y-%m-%dT%H:%M:%S")}  # rooms of past terms are not worth a call
    return ics_text(exam_events(terms, rooms, year))


def fetch_points():
    studia = vutapi.get("/api/moje/studia/v1")["data"]["studia"]
    sid = next((x for x in studia if x.get("aktivni_studium")), studia[0])["studium_id"]
    points = api_points(vutapi.get(f"/api/moje/studia/studium/{sid}/index/v1")["data"]["studia"][0]["index"])
    if not points:
        raise vutapi.ApiError("empty index")
    return points


def fetch_periods():
    periods = api_periods(vutapi.get("/api/rozvrh/moje/tydny/v1")["data"]["tydny"])
    if not periods:
        raise vutapi.ApiError("no weeks")
    return periods


def api_pieces(now):
    """{piece: value} for every piece the API delivered; a failing one is warned about and left to the scraper."""
    got = {}
    for name, fn in (("timetable", lambda: fetch_timetable(now)), ("exams", lambda: fetch_exams(now)), ("points", fetch_points), ("periods", fetch_periods)):
        try:
            got[name] = fn()
        except vutapi.TokenError as e:  # no token (or refused twice): the other pieces would fail the same way
            print(f"warning: API unusable ({e}); scraping the rest", file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001  ApiError, a changed response shape, ...: fall back to the scraper for this piece
            print(f"warning: API {name} failed ({type(e).__name__}: {e}); scraping it instead", file=sys.stderr)
    return got


# ---------- Studis scraping: what the API lacks, plus fallbacks ----------

def scrape(need, prev):
    """Log into Studis and return {piece: value}: always items, terms, deadlines; timetable, exams, points, periods only when in `need`."""
    user, pw = os.environ["VUT_USER"], os.environ["VUT_PASS"]
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            pg = b.new_page(locale="cs-CZ")
            pg.goto("https://www.vut.cz/login/", wait_until="networkidle")
            pg.fill("input[name=login]", user)
            pg.click("button[name=btnsubmit]")
            try:
                pg.wait_for_selector("input[type=password]", timeout=15000)
            except Exception:
                dump(pg, "no password field after username step")
            pg.fill("input[type=password]", pw)
            pg.keyboard.press("Enter")
            try:
                pg.wait_for_url(re.compile(r"^https://www\.vut\.cz/(?!login)"), timeout=30000)
            except Exception:
                dump(pg, "did not land back on www.vut.cz after password")
            if "timetable" in need:
                pg.goto(ICS_URL.split("&ics")[0], wait_until="networkidle")  # export covers the displayed timetable
                out["timetable"] = pg.request.get(ICS_URL).text()
                if "BEGIN:VCALENDAR" not in out["timetable"]:  # an empty calendar is fine (timetable not published yet); a login page is not
                    print(re.sub(r"<[^>]+>|\s+", " ", out["timetable"])[:500], file=sys.stderr)
                    dump(pg, "export is not a calendar")
            pg.goto(BASE + "?sn=terminy_zk", wait_until="networkidle")
            if "exams" in need:
                try:
                    out["exams"] = pg.request.get(pg.locator("a", has_text="Export do iCal").first.evaluate("a => a.href", timeout=3000)).text()
                except Exception as e:  # noqa: BLE001  no link (nothing registered yet): no exams, the rest of the scrape goes on
                    print(f"warning: no exam export ({type(e).__name__}); treating it as no exams", file=sys.stderr)
                    out["exams"] = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
            out["terms"] = parse_terms(pg.inner_text("body"))
            if "periods" in need:
                pg.goto(BASE + "?sn=harmonogram", wait_until="networkidle")
                out["periods"] = parse_periods(pg.inner_text("body"))
            pg.goto(BASE + "?sn=projekty", wait_until="networkidle")
            out["deadlines"] = parse_deadlines(pg.inner_text("body"))
            pg.goto(BASE + "?sn=el_index", wait_until="networkidle")
            if "points" in need:
                rows = pg.evaluate("[...document.querySelectorAll('tr')].map(tr => [...tr.children].map(td => td.innerText.trim()))")
                out["points"] = {}
                for r in rows:  # first (current) year wins; column 9 is 'Body'
                    if len(r) >= 10 and re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,7}", r[0]) and r[0] not in out["points"]:
                        out["points"][r[0]] = parse_points(r[9])
            # current year's table only: per-item min/max/earned points from each course's detail page
            details = pg.evaluate("""[...([...document.querySelectorAll('table')].find(t => t.querySelector('a[href*="sn=predmet_detail"]'))
                ?.querySelectorAll('a[href*="sn=predmet_detail"]') || [])].map(a => [a.closest('tr').children[0].innerText.trim(), a.href])""")

            def load(url):
                pg.goto(url, wait_until="domcontentloaded")
                return parse_items(pg.evaluate("""[...document.querySelectorAll('tr')].filter(tr => [...tr.children].every(c => c.tagName == 'TH'))
                    .map(tr => [...tr.children].map(c => c.innerText.trim()))"""))
            out["items"] = collect_items(details, prev.get("items", {}), load)
        finally:
            b.close()
    return out


def write_atomic(path, text):
    """tmp file in the same directory + rename: a reader never sees half a file."""
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as f:
        f.write(text)
    os.chmod(f.name, 0o644)  # NamedTemporaryFile is 0600; the files were world-readable before
    os.replace(f.name, path)


def api_streak(failed, f=None):
    """Runs in a row in which some API piece failed (reset by a clean run), kept in a small state file."""
    f = f or HERE / "api-failures.json"
    try:
        n = int(json.loads(f.read_text())["count"])
    except (OSError, ValueError, KeyError, TypeError):
        n = 0
    n = n + 1 if failed else 0
    try:
        write_atomic(f, json.dumps({"count": n}))
    except OSError:
        pass
    return n


def main():
    if "--install-browser" in sys.argv:  # GitHub Actions: installs the Chromium that matches the locked Playwright version
        from playwright.__main__ import main as playwright_cli
        sys.argv = ["playwright", "install", "--with-deps", "chromium"]
        return playwright_cli()
    load_env()
    prev = {}
    try:
        prev = json.loads((HERE / "studis.json").read_text())
    except (OSError, ValueError):
        pass
    use_api = vutapi.configured()  # no VUT_API_CLIENT_ID / VUT_API_SECRET: scrape everything, no warnings
    api = api_pieces(datetime.now(PRAGUE)) if use_api else {}
    got, failed = dict(api), False
    if use_api and api_streak(len(api) < 4) == 3:  # the scraper hides a broken API; say so once, in the line the failure DM quotes
        print("FAILED: the VUT API failed 3 runs in a row (pieces scraped instead)", file=sys.stderr)
        failed = True
    try:
        got = {**scrape({"timetable", "exams", "points", "periods"} - api.keys(), prev), **api}  # the API wins where it delivered
    except Exception as e:  # noqa: BLE001  ScrapeError or a Playwright timeout: keep what the API gave, previous values for the rest
        failed = True
        print(f"FAILED: scraping Studis: {type(e).__name__}: {e}", file=sys.stderr)
    data = {k: got.get(k, prev.get(k)) for k in ("deadlines", "points", "items", "terms", "periods")}
    if "timetable" not in got:  # neither the API nor the scraper delivered it: the calendar cannot be synced
        print("FAILED: no timetable", file=sys.stderr)
        failed = True
    if not (data["periods"] and data["points"]):  # new student / layout change: not fatal, the calendar does not need them
        print(f"warning: empty parse: {len(data['terms'] or [])} terms, {len(data['periods'] or [])} periods, {len(data['points'] or [])} points", file=sys.stderr)
    moodle = None
    if os.environ.get("MOODLE_ICS_URL"):  # Moodle -> Kalendář -> Exportovat; a Moodle outage must not stop the rest
        try:
            moodle = urllib.request.urlopen(os.environ["MOODLE_ICS_URL"], timeout=30).read().decode()
            if "BEGIN:VCALENDAR" not in moodle:
                raise ValueError("not a calendar")
        except Exception as e:  # noqa: BLE001
            moodle = None
            print(f"warning: Moodle export failed ({type(e).__name__}); keeping the previous file", file=sys.stderr)
    for piece, name in (("timetable", "studis-timetable.ics"), ("exams", "studis-exams.ics")):  # everything validated: now write
        if piece in got:
            write_atomic(HERE / name, got[piece])
    if moodle:
        write_atomic(HERE / "studis-moodle.ics", moodle)
    write_atomic(HERE / "studis.json", json.dumps({k: v for k, v in data.items() if v is not None}, ensure_ascii=False, indent=1))
    print(f"fetched {got.get('timetable', '').count('BEGIN:VEVENT')} events, {got.get('exams', '').count('BEGIN:VEVENT')} exam terms, {len(data['terms'] or [])} term groups, "
          f"{len(data['deadlines'] or [])} deadlines, points for {len(data['points'] or {})} courses, {len(data['periods'] or [])} periods; from the API: {', '.join(sorted(api)) or 'nothing'}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
