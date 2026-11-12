# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth-oauthlib"]
# ///
"""Sync a Studis iCal export into a dedicated Google Calendar.

Usage: uv run sync.py [timetable.ics] [--dry-run] [--adopt] [--force] [--login]
  1. Studis -> Individuální rozvrh -> "Export zobrazeného rozvrhu do iCalendar"
  2. save the file as studis-timetable.ics next to this script (or pass its path)
  3. run this script; first run opens a browser for Google consent
  --adopt  one-off: move course events (e.g. "ABC (P)") out of the primary
           calendar into CAL_NAME first, then dedupe.
  --dry-run  read Google, print the plan (keep/patch/delete/insert), write nothing.
  --force    apply deletes even when the mass-delete brake (>30% of future events, or empty/non-iCal source) trips.
"""
import html, json, os, re, sys, unicodedata
from collections import Counter
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

HERE = Path(__file__).parent


def load_env():  # .env next to the code, setdefault semantics (a real environment variable wins), same as fetch_studis.py
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env()
CAL_NAME = os.environ.get("CALENDAR_NAME") or "Škola"  # the Google calendar this script owns
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CODE = r"[A-Z0-9][A-Z0-9-]{1,7}"  # course code as VUT writes it ("ITU", "BPC-ABC")
COURSE = re.compile(rf"^{CODE} \([^)]+\)$")  # "ABC (L)", "ABC (termín)"
SKIP = re.compile(os.environ.get("SKIP_TITLES") or r"(?!)")  # optional regex (env SKIP_TITLES) for titles that are noise to you, e.g. ^ABC \(termín\)$
TAG = {"private": {"src": "vut-kalendar"}}  # extendedProperties on every event this script inserts: only tagged events are ever patched or deleted, whatever their title
EXAM = re.compile(r"zkou[sš]k|exam|term[ií]n|p[uů]lsem|z[aá]po[cč]|\btest|odevzd|\((Zk|ZK|PZ)\)", re.I)
PLAIN_ZAP = re.compile(r"^(?!.*klasifikovan).*z[aá]po[cč]", re.I)  # zápočet that isn't "klasifikovaný": no exam treatment
ZAPOC = re.compile(rf"(Učení: )?{CODE} ?\((klasifikovan\w+ )?z[aá]po[cč]et\)", re.I)  # "ISA (zápočet)", "ITU (klasifikovaný zápočet)" (+ its study block): university placeholders, never in the calendar
PRAGUE = ZoneInfo("Europe/Prague")
DESC = {}  # event key -> description (Moodle); key_of ignores it
RESERVE_DAYS = 7  # Studis repeats a one-off room reservation every week; sync only those starting this soon
# Google event colours (colorId): lectures blue, labs/exercises green, exams red, deadlines orange, reservations grey, study blocks palest (lavender)
COLORS = {"lecture": "7", "lab": "10", "exam": "11", "deadline": "6", "reservation": "8", "study": "1"}
SEP = "— Studis —"  # generated description lives below this line; anything above it is the user's
ROOM_URL = os.environ.get("ROOM_URL") or "https://www.fit.vut.cz/fit/room/{}/.cs"  # FIT room page ("{}" = room); ROOM_URL=off turns the links off
if ROOM_URL == "off":
    ROOM_URL = ""
MIN = {"class": [120, 60], "exam": [7 * 1440, 2 * 1440, 1440], "work": [60, 5, 1]}


def reminders(kind, summary=""):
    mins = [] if summary.startswith(("Učení", "Studium:")) else MIN["exam" if kind == "school" and EXAM.search(summary) and not PLAIN_ZAP.search(summary) else "class" if kind == "school" else kind]
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in mins]}


def iso(s):
    if re.fullmatch(r"\d{8}", s):  # VALUE=DATE all-day
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return re.sub(r"(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z", r"\1-\2-\3T\4:\5:\6Z", s)


def props(body):
    return {k: (p, v) for k, p, v in re.findall(r"^([A-Z]+)((?:;[^:]*)?):(.*)$", body, re.M)}


def zs(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ics_time(p, v):
    """DTSTART/DTEND value + params -> iso(); a TZID-local time is converted to UTC, a floating one (no Z/TZID) is Prague-local."""
    v = v.strip()
    if re.fullmatch(r"\d{8}T\d{6}", v):
        tz = ZoneInfo(m[1].strip('"')) if (m := re.search(r"TZID=([^;:]+)", p)) else PRAGUE
        return zs(datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=tz))
    return iso(v)


def add_dur(s, v):
    """iso() start + iCal DURATION -> end. No (or unparsable) DURATION: a timed event is instantaneous, an all-day one lasts a day."""
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    w, d, h, mi, sec = (int(x or 0) for x in m.groups()[1:]) if m else (0,) * 5
    td = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=sec) * (-1 if m and m[1] == "-" else 1)
    if len(s) == 10:
        return (date.fromisoformat(s) + (td or timedelta(days=1))).isoformat()
    return zs(datetime.fromisoformat(s) + td)


def shift(s, days, utc=False):  # same Prague wall-clock time `days` later (DST-safe), or the same UTC time if the source was in UTC; an all-day date just moves
    if utc and len(s) > 10:
        return zs(datetime.fromisoformat(s) + timedelta(days=days))
    if len(s) == 10:
        return (date.fromisoformat(s) + timedelta(days=days)).isoformat()
    return zs((datetime.fromisoformat(s).astimezone(PRAGUE).replace(tzinfo=None) + timedelta(days=days)).replace(tzinfo=PRAGUE))


def expand(s, e, rule, utc=False):
    """[(start, end)] of one VEVENT. Simple DAILY/WEEKLY RRULEs with COUNT or UNTIL are expanded (in UTC if DTSTART was UTC, else in Prague wall-clock time); anything else warns and keeps the first instance."""
    rule = rule.strip()
    if not rule:
        return [(s, e)]
    try:
        r = dict(p.split("=", 1) for p in rule.split(";"))
        step = {"DAILY": 1, "WEEKLY": 7}[r["FREQ"]] * int(r.get("INTERVAL", 1))
        if not ({"COUNT", "UNTIL"} & r.keys()) or r.keys() - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST"}:
            raise ValueError
        until, out = (ics_time("", r["UNTIL"]) if "UNTIL" in r else None), []
        for n in range(min(int(r.get("COUNT", 500)), 500)):  # 500: cap for a typo'd COUNT/UNTIL
            sn, en = shift(s, n * step, utc), shift(e, n * step, utc)
            if until and (day_of(sn) if len(until) == 10 else sn) > until:
                break
            out.append((sn, en))
        return out
    except (KeyError, ValueError):
        print(f"warning: unsupported RRULE {rule!r}, keeping the first instance", file=sys.stderr)
        return [(s, e)]


def unesc(s):  # iCal TEXT escapes: \, \; \\ \n
    return re.sub(r"\\(.)", lambda m: "\n" if m[1] in "nN" else m[1], s)


def clean(s):  # Studis puts HTML in SUMMARY: 'X  <span class="normal-text">(rezervace)</span>' -> 'X (rezervace)'
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", s)).split())


def vevents(text):  # props() of every VEVENT that has a start and is not cancelled
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold long lines
    for body in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        f = props(body)
        if "DTSTART" in f and f.get("STATUS", ("", ""))[1].strip().upper() != "CANCELLED":
            yield f


def parse_ics(text):
    out = set()
    for f in vevents(text):
        s = ics_time(*f["DTSTART"])
        e = ics_time(*f["DTEND"]) if "DTEND" in f else add_dur(s, f.get("DURATION", ("", ""))[1])
        summ, loc = (clean(unesc(f.get(k, ("", ""))[1])) for k in ("SUMMARY", "LOCATION"))
        out.update((a, b, summ, loc) for a, b in expand(s, e, f.get("RRULE", ("", ""))[1], f["DTSTART"][1].strip().endswith("Z")))
    return out


def day_of(s):  # Prague-local date of an event start ("2026-10-21T22:00:00Z" or all-day "2026-10-21")
    return s if len(s) == 10 else datetime.fromisoformat(s).astimezone(PRAGUE).strftime("%Y-%m-%d")


def soon(k, now):  # timetable event worth syncing: not a "(rezervace)" or one starting within RESERVE_DAYS
    t = datetime.fromisoformat(k[0])
    return "(rezervace)" not in k[2] or (t if t.tzinfo else t.replace(tzinfo=PRAGUE)) < now + timedelta(days=RESERVE_DAYS)


def kind_of(summ):  # what an event we create is: lecture/lab/exam/deadline/reservation/study, None = unknown shape
    if summ.startswith("Učení: "):
        return "study"
    if " – odevzdání: " in summ:
        return "deadline"
    if "(rezervace)" in summ:
        return "reservation"
    if EXAM.search(summ):
        return "exam"
    return ("lecture" if summ.endswith("(P)") else "lab") if COURSE.match(summ) else None


def style(summ):  # colour + busy/free of an event we create: lectures show as free
    kind = kind_of(summ)
    return {**({"colorId": COLORS[kind]} if kind else {}), "transparency": "transparent" if kind == "lecture" else "opaque"}


def rooms(loc):  # links to the FIT room pages of the FIT rooms (building B) in a location ("B/D0206 (Božetěchova 1/2, Brno 61200) [B/D0206]"); other rooms get none
    return "\n".join(f"Místnost {r}: {ROOM_URL.format(r)}" for r in dict.fromkeys(re.findall(r"(?:^|[\s\[,])B/([A-Z]\d{3,4}(?:\.\d)?)\b", loc))) if ROOM_URL else ""


def desc_of(k):  # description we want on the event: source text (Moodle, exam points + teacher) then room links; the location itself stays plain
    return "\n\n".join(x for x in (DESC.get(k), rooms(k[3])) if x)


def merged(cur, gen):
    """Description to store for an event whose current description is `cur` and generated text `gen`; None = nothing to change. Text above SEP is kept verbatim."""
    flat = lambda t: " ".join(t.split())  # noqa: E731
    user, sep, old = cur.partition(SEP)
    if not gen:  # nothing generated any more: drop a stale generated part, keep the user's text
        return user.rstrip() if sep else None
    if sep and flat(old) == flat(gen):
        return None
    if not sep:  # no separator yet: the whole current text is the user's, unless it is just the generated text
        user = "" if flat(cur) in ("", flat(gen)) else cur.rstrip() + "\n\n"
    return f"{user}{SEP}\n{gen}"


@lru_cache
def holidays(y):  # Czech public holidays of year y as ISO dates: fixed ones + Easter Friday/Monday (Gregorian computus)
    a, b, c = y % 19, y // 100, y % 100
    h = (19 * a + b - b // 4 - (8 * b + 13) // 25 + 15) % 30
    l = (32 + 2 * (b % 4) + 2 * (c // 4) - h - c % 4) % 7  # noqa: E741
    n = h + l - 7 * ((a + 11 * h + 19 * l) // 433) + 114
    easter = date(y, n // 31, n % 31 + 1)
    return {d.isoformat() for d in [easter - timedelta(days=2), easter + timedelta(days=1)] + [date(y, m, d) for m, d in
            [(1, 1), (5, 1), (5, 8), (7, 5), (7, 6), (9, 28), (10, 28), (11, 17), (12, 24), (12, 25), (12, 26)]]}


def holiday_class(k):  # a class (not an exam, deadline or reservation) on a public holiday
    return bool(COURSE.match(k[2])) and not EXAM.search(k[2]) and day_of(k[0]) in holidays(int(day_of(k[0])[:4]))


def fold(s):  # case- and diacritics-insensitive text
    return " ".join("".join(c for c in unicodedata.normalize("NFD", s.casefold()) if not unicodedata.combining(c)).split())


def item_for(items, course, kind):
    """Course-card item whose parenthesised kind is the exam's kind ("Test (písemný test)" <-> "ITU (písemný test)"); a bonus row only if nothing else."""
    hits = [i for i in items.get(course, []) if (m := re.search(r"\(([^()]*)\)$", i["name"])) and fold(m[1]) == fold(kind)]
    return min(hits, key=lambda i: "bonus" in i["name"].casefold(), default=None)


def points_left(c, its):
    """Course-card items -> (max points in total, ["Projekt 20 (min 5)", ...] still obtainable); zápočet rows count only when nothing else does."""
    its = [i for i in its if i["max"] and "zápočet" not in i["name"]] or [i for i in its if i["max"]]
    name = lambda i: re.sub(rf"\s*\(.*\)$|\s+{re.escape(c)}\b", "", i["name"]) or i["name"]  # noqa: E731
    return sum(i["max"] for i in its), [f"{name(i)} {i['max'] - (i['got'] or 0):g}" + (f" (min {i['min']:g})" if i["min"] else "") for i in its if i["max"] > (i["got"] or 0)]


def exam_desc(course, item, points, items, teacher):  # short Czech note for an exam: points at stake, standing in the course, teacher
    lines = []
    if item and item["max"]:
        lines.append(f"Max. {item['max']:g} b." + (f", min. {item['min']:g} b." if item["min"] else ""))
        _, rest = points_left(course, items.get(course, []))
        lines.append(f"V předmětu zatím {points or 0:g} b." + (f", ještě lze získat: {', '.join(rest)}" if rest else ""))
    return "\n".join(lines + ([f"Vyučující: {teacher}"] if teacher else []))


def extra_events(wanted, now=None):
    """Exam terms (studis-exams.ics), project deadlines (Moodle, then studis.json ones Moodle doesn't have) and a study block the evening before each exam."""
    out = set()
    f = HERE / "studis.json"
    st = json.loads(f.read_text()) if f.exists() else {}
    f = HERE / "studis-exams.ics"
    if f.exists():
        for s, e, summ, loc in parse_ics(f.read_text()):
            m = re.match(rf"^({CODE})\s*\((.+?)\)", summ)  # "IIS(písemná část ...) - Novák ..." -> "IIS (písemná část ...)"
            out.add(k := (s, e, f"{m[1]} ({m[2]})" if m else summ, loc))
            if m:
                teacher = re.sub(r"^\s*[-–]\s*|\s*\[[^\]]*\]\s*$", "", summ[m.end():])
                if d := exam_desc(m[1], item_for(st.get("items", {}), m[1], m[2]), st.get("points", {}).get(m[1]), st.get("items", {}), teacher):
                    DESC[k] = d
    moodle = []  # (course, due) of Moodle deadlines; a studis.json one within 60 min is the same assignment
    f = HERE / "studis-moodle.ics"  # Moodle due dates: CATEGORIES "ITU 26/27Z (302573)" carries the course code
    if f.exists():
        for g in vevents(f.read_text()):
            course = g.get("CATEGORIES", ("", ""))[1].split(" ")[0] or "Moodle"
            desc = unesc(g.get("DESCRIPTION", ("", ""))[1]).strip()
            s0 = ics_time(*g["DTSTART"])
            for s, _ in expand(s0, s0, g.get("RRULE", ("", ""))[1], g["DTSTART"][1].strip().endswith("Z")):
                due = datetime.fromisoformat(s + "T23:59:00" if len(s) == 10 else s)
                due = due.replace(tzinfo=PRAGUE).astimezone(timezone.utc) if len(s) == 10 else due
                moodle.append((course, due))
                k = ((due - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), due.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     f"{course} – odevzdání: {clean(unesc(g.get('SUMMARY', ('', ''))[1]))}", "")
                out.add(k)
                if desc:
                    DESC[k] = desc
    for d in st.get("deadlines", []):
        due = datetime.fromisoformat(d["due"]).replace(tzinfo=PRAGUE).astimezone(timezone.utc)
        if any(c == d["course"] and abs(m - due) <= timedelta(minutes=60) for c, m in moodle):
            continue  # Moodle's copy has the description
        out.add(((due - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), due.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 f"{d['course']} – odevzdání: {d['title']}", ""))
    now = now or datetime.now(timezone.utc)
    for s, e, summ, loc in list(out):
        if "odevzdání" in summ or len(s) == 10 or datetime.fromisoformat(s) < now or PLAIN_ZAP.search(summ) \
                or datetime.fromisoformat(e) - datetime.fromisoformat(s) > timedelta(days=1):  # all-day, past, plain zápočet, multi-day windows
            continue
        day = datetime.fromisoformat(s).astimezone(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
        utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        m = re.match(rf"^({CODE}) \((.+)\)$", summ)
        top = (item_for(st.get("items", {}), m[1], m[2]) or {}).get("max") if m else None  # points at stake sizes the block
        for a, b in [(day - timedelta(days=1), day)] if top is None or 20 <= top < 40 else [(day - timedelta(days=2), day - timedelta(days=1)), (day - timedelta(days=1), day)] if top >= 40 \
                else [((day - timedelta(days=1)).replace(hour=17), (day - timedelta(days=1)).replace(hour=21))]:
            if b > now:  # a block that is over already is pointless
                out.add((utc(a), utc(b), f"Učení: {summ}", ""))  # whole day(s) = full column; small stakes only the evening before
    return out


def when(ev, k):  # "2026-10-21T05:00:00Z" for timed events, "2026-10-21" for all-day ones
    return ev[k].get("dateTime", ev[k].get("date", "")).replace("+00:00", "Z")


def key_of(ev):
    return (when(ev, "start"), when(ev, "end"), ev.get("summary", "").strip(), ev.get("location", "").strip())


def norm(s):
    return clean(s).casefold()


def nkey(k):  # match key: cosmetic differences in title/location (case, spaces, HTML) don't make events different
    return (k[0], k[1], norm(k[2]), norm(k[3]))


def drop_redundant(wanted):  # a "(rezervace)" exactly overlapping a class of the same course in the same room adds nothing
    classes = {(k[0], k[1], k[2].split(" ")[0], norm(k[3])) for k in wanted if COURSE.match(k[2])}
    def redundant(k):
        m = re.search(r"(\S+)\s*\(rezervace\)", k[2])
        return bool(m and k[3] and (k[0], k[1], m[1], norm(k[3])) in classes)
    return {k for k in wanted if not redundant(k)}


def dedupe_source(wanted):
    """One event that arrives twice becomes one: same start and title (case, spaces, HTML, punctuation ignored; a trailing " - teacher" / "[/]" on an
    exam title too) with the same room or none given, or a study block repeated for the same window and course. Titles that differ by any
    word or digit are different events, and so are parallel sessions in different rooms or an exam + class sharing a start."""
    tail = re.compile(r"^(\S+ ?\([^)]*\))\s*(?:[-–]\s.*|\[[^\]]*\])$")
    title = lambda t: " ".join(re.sub(r"\W+", " ", norm(tail.sub(r"\1", t))).split())  # noqa: E731
    pref = lambda k: (k in DESC, bool(k[3]), k)  # noqa: E731  keep the one with a description, then a room
    groups = {}
    for k in sorted(wanted, key=pref, reverse=True):
        study = k[2].startswith("Učení: ")
        hits = groups.setdefault((study, k[0], k[2].removeprefix("Učení: ").split(" ")[0] if study else title(k[2])), [])
        if not any(h[1] == k[1] if study else not (h[3] and k[3] and norm(h[3]) != norm(k[3])) for h in hits):
            hits.append(k)
    return {k for hits in groups.values() for k in hits}


def wanted_from(events):
    """Source events -> what the calendar should hold. Order matters: a reservation duplicating a class goes before a holiday removes that class."""
    ks = {k for k in events if not SKIP.match(k[2]) and not ZAPOC.fullmatch(k[2])}
    return {k for k in dedupe_source(drop_redundant(ks)) if not holiday_class(k)}


def tomorrow(now):
    return (now.astimezone(PRAGUE) + timedelta(days=1)).strftime("%Y-%m-%d")


def synced(ev):  # created by this script (tagged); an untagged event is hand-made and left alone
    return ev.get("extendedProperties", {}).get("private", {}).get("src") == TAG["private"]["src"]


def when_body(s):
    return {"date" if len(s) == 10 else "dateTime": s}


def drift(ev, k):  # fields of a matched own (tagged) event that differ from what we'd create (a description is never cleared: may hold user notes)
    sig = lambda r: (bool(r.get("useDefault")), {(o["method"], o["minutes"]) for o in r.get("overrides", [])})  # noqa: E731
    body, want, st = {}, reminders("school", k[2]), style(k[2])
    if sig(ev.get("reminders", {})) != sig(want):
        body["reminders"] = want
    body |= {f: v for f, v in st.items() if ev.get(f, "opaque" if f == "transparency" else None) != v}
    if (d := merged(ev.get("description", ""), desc_of(k))) is not None:
        body["description"] = d
    return body


def plan(wanted, have_events, now):
    """Pure diff. -> [("keep", ev) | ("patch", ev, body) | ("delete", ev, "dup" | "stale") | ("insert", key)]. Hand-made events are only ever kept."""
    ops, free, by_key, tom = [], {}, {}, tomorrow(now)
    for ev in have_events:
        by_key.setdefault(nkey(key_of(ev)), []).append(ev)
    for nk, evs in by_key.items():
        mine = sorted((e for e in evs if synced(e)), key=lambda e: (not e.get("description"), e.get("created", "9"), e["id"]))
        ops += [("delete", e, "dup") for e in mine[1:]]  # Google duplicates (own events only): keep the one with a description, else the oldest
        for e in mine[:1] + [e for e in evs if not synced(e)]:  # ours first: an exact match prefers our own event over a hand-made twin
            free[id(e)] = (e, key_of(e), nk)
    matched, todo = [], []
    for w in sorted(wanted):  # pass 1: same key
        nk = nkey(w)
        hit = next((i for i, (_, _, n) in free.items() if n == nk), None)
        (matched.append((free.pop(hit)[0], w, {})) if hit else todo.append(w))
    rest = []
    for w in todo:  # pass 2: same title and start; end and/or room changed
        hit = next((i for i, (e, k, n) in free.items() if synced(e) and k[0] == w[0] and n[2] == norm(w[2])), None)
        if hit:
            e, k, _ = free.pop(hit)
            matched.append((e, w, {**({"end": when_body(w[1])} if k[1] != w[1] else {}), **({"location": w[3]} if norm(k[3]) != norm(w[3]) else {})}))
        else:
            rest.append(w)
    todo, rest, span = rest, [], lambda k: datetime.fromisoformat(k[1]) - datetime.fromisoformat(k[0])  # noqa: E731
    for w in todo:  # pass 3: same title and length, start moved within the same Prague day (timed events only)
        near = [i for i, (e, k, n) in free.items() if synced(e) and len(k[0]) > 10 and len(w[0]) > 10 and day_of(k[0]) >= tom and n[2] == norm(w[2])
                and day_of(k[0]) == day_of(w[0]) and span(k) == span(w)]
        if near:
            e, k, _ = free.pop(min(near, key=lambda i: abs(datetime.fromisoformat(free[i][1][0]) - datetime.fromisoformat(w[0]))))
            matched.append((e, w, {"start": when_body(w[0]), "end": when_body(w[1]), **({"location": w[3]} if norm(k[3]) != norm(w[3]) else {})}))
        else:
            rest.append(w)
    for e, w, body in matched:
        body = {**body, **(drift(e, w) if synced(e) else {})}
        ops.append(("patch", e, body) if body else ("keep", e))
    live_zap = lambda k: bool(ZAPOC.fullmatch(k[2])) and (day_of(k[1]) >= tom if len(k[1]) == 10 else datetime.fromisoformat(k[1]) > now)  # noqa: E731  semester-long placeholder already under way
    ops += [("delete", e, "stale") for e, k, _ in free.values() if synced(e) and (day_of(k[0]) >= tom or live_zap(k))]  # Studis exports from tomorrow on
    return ops + [("insert", w) for w in rest]


def brake(ops, have_events, now, bad):
    """Why this run must not delete anything ("" = go ahead): the source looks broken, or the plan wipes out a big part of the calendar."""
    real = lambda e: not ZAPOC.fullmatch(clean(e.get("summary", "")))  # noqa: E731  pruning zápočet placeholders is intended, not suspicious
    n = sum(o[0] == "delete" and o[2] == "stale" and real(o[1]) for o in ops)
    future = sum(synced(e) and real(e) and day_of(key_of(e)[0]) >= tomorrow(now) for e in have_events)
    if n and bad:
        return f"source is empty or not an iCal file, would delete {n} events"
    if n > 5 and n > .3 * future:
        return f"would delete {n} of {future} future synced events"
    return ""


def service():
    import os, tempfile
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    tok = HERE / "token.json"
    try:
        creds = Credentials.from_authorized_user_file(tok, SCOPES) if tok.exists() else None
    except (ValueError, OSError):  # empty or garbled token.json (e.g. an empty GOOGLE_TOKEN secret)
        creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:  # revoked/expired refresh token
                creds = None
        if not creds or not creds.valid:
            if not sys.stdin.isatty():  # systemd/bot: never block on a browser login
                raise RuntimeError("the Google token (token.json / secret GOOGLE_TOKEN) is missing, empty or expired: run `uv run sync.py --login` on a computer with a browser and update the GOOGLE_TOKEN secret")
            creds = InstalledAppFlow.from_client_secrets_file(HERE / "client_secret.json", SCOPES).run_local_server(port=0, prompt="consent")  # consent every time: Google only hands out a refresh token then
        fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".tmp")  # atomic: concurrent readers never see a half-written file
        with os.fdopen(fd, "w") as f:
            f.write(creds.to_json())
        os.replace(tmp, tok)
    return build("calendar", "v3", credentials=creds)


def all_calendars(svc):
    out, tok = [], None
    while True:
        r = svc.calendarList().list(pageToken=tok).execute(num_retries=3)
        out += r.get("items", [])
        if not (tok := r.get("nextPageToken")):
            return out


def list_events(svc, cid, single=True, **kw):
    out, tok = [], None
    while True:
        r = svc.events().list(calendarId=cid, maxResults=2500, singleEvents=single, timeZone="UTC", pageToken=tok,
                              orderBy="startTime" if single else None, **kw).execute(num_retries=3)
        out += [e for e in r.get("items", []) if e.get("start")]
        if not (tok := r.get("nextPageToken")):
            return out


def attempt(req, label, fails, gone_ok=False):  # one failing Google call must not stop the rest; deleting what is already gone is fine
    try:
        return req.execute(num_retries=3)
    except Exception as e:  # noqa: BLE001
        if gone_ok and getattr(getattr(e, "resp", None), "status", None) in (404, 410):
            return {}
        fails.append(label)
        print(f"FAILED {label}: {e!r}")


def notify(msg):
    try:
        from digest import send  # late: digest imports this module
        send(msg)
    except Exception as e:  # noqa: BLE001
        print(f"DM failed: {e!r}")


def main():
    if "--login" in sys.argv:  # browser consent once; the printed token.json goes into the GOOGLE_TOKEN secret
        service()
        return print(f"Přihlášení hotovo. Obsah souboru {HERE / 'token.json'} vlož do tajného klíče GOOGLE_TOKEN.")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry, adopt, force = "--dry-run" in sys.argv, "--adopt" in sys.argv, "--force" in sys.argv
    now = datetime.now(PRAGUE)
    text = Path(args[0] if args else HERE / "studis-timetable.ics").read_text()
    wanted = wanted_from({k for k in parse_ics(text) if soon(k, now)} | extra_events(set()))
    bad = not wanted or not text.lstrip().startswith("BEGIN:VCALENDAR")
    print(f"ics: {len(wanted)} events" + (f", {min(k[0] for k in wanted)[:10]} .. {max(k[1] for k in wanted)[:10]}" if wanted else ""))
    svc, fails = service(), []
    cal = next((c for c in all_calendars(svc) if c["summary"] == CAL_NAME), None)
    if not cal and not dry:
        cal = svc.calendars().insert(body={"summary": CAL_NAME, "timeZone": "Europe/Prague"}).execute(num_retries=3)
        print("created calendar", CAL_NAME)
    cid = cal["id"] if cal else None

    if adopt and dry:
        print("dry-run: --adopt skipped")
    elif adopt:
        moved = 0
        for e in list_events(svc, "primary"):
            if COURSE.match(e.get("summary", "").strip()) and not e.get("recurringEventId"):
                if attempt(svc.events().move(calendarId="primary", eventId=e["id"], destination=cid), f"move {e['summary']}", fails) is not None:
                    attempt(svc.events().patch(calendarId=cid, eventId=e["id"], body={"extendedProperties": TAG}), f"tag {e['summary']}", fails)  # adopted = ours from now on
                    moved += 1
        print(f"adopt: moved {moved} course events from primary")

    have = list_events(svc, cid) if cid else []
    ops = plan(wanted, have, now)
    if (why := brake(ops, have, now, bad)) and not force:
        print(f"BRAKE: {why}; skipping all deletes this run (--force overrides)")
        ops = [o for o in ops if o[0] != "delete"]
        if not dry and not (os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_USER_ID")):
            fails.append("deletes skipped by the mass-delete brake")  # no Discord to tell you: fail the run (red in Actions) so you notice
        if not dry:
            notify(f"vut-kalendar sync: {why}. Deletes skipped; check the source, or rerun with --force.")
    for op in ops:
        k = op[1] if op[0] == "insert" else key_of(op[1])
        label = f"{op[0]} {k[0]} {k[2]}" + (f" {sorted(op[2])}" if op[0] == "patch" else f" ({op[2]})" if op[0] == "delete" else "")
        if op[0] == "keep":
            continue
        if dry:
            print(label)
        elif op[0] == "insert":
            s, e, summ, loc = k
            attempt(svc.events().insert(calendarId=cid, body={"summary": summ, "location": loc, "reminders": reminders("school", summ), "start": when_body(s),
                    "end": when_body(e), "extendedProperties": TAG, **style(summ), **({"description": merged("", desc_of(k))} if desc_of(k) else {})}), label, fails)
        elif op[0] == "patch":
            attempt(svc.events().patch(calendarId=cid, eventId=op[1]["id"], body=op[2]), label, fails)
        elif op[0] == "delete":
            attempt(svc.events().delete(calendarId=cid, eventId=op[1]["id"]), label, fails, gone_ok=True)
    c = Counter(o[0] for o in ops)
    print(f"google: {len(have)} before; keep {c['keep']}, patch {c['patch']}, delete {c['delete']}, insert {c['insert']}" + (" (dry-run, nothing written)" if dry else ""))
    if fails:
        print(f"{len(fails)} operations failed")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:  # e.g. the Google token is missing: a message, not a traceback
        sys.exit(f"FAILED: {e}")
