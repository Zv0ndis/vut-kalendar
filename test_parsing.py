# uv run python test_parsing.py  -- pure parsing checks (no network, no Google)
import re, sys, types
from datetime import datetime
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules.setdefault("playwright.sync_api", types.SimpleNamespace(sync_playwright=None))
from sync import iso, parse_ics, day_of, reminders, soon, PLAIN_ZAP, synced
from fetch_studis import cz_date, parse_points, parse_items, parse_terms, parse_periods
from digest import PRAGUE, stamp

assert iso("20261106") == "2026-11-06" and iso("20261021T050000Z") == "2026-10-21T05:00:00Z"
ev = lambda a, b: f"BEGIN:VEVENT\nDTSTART{a}\nDTEND{b}\nSUMMARY:X\nEND:VEVENT"
assert parse_ics(ev(";VALUE=DATE:20261106", ";VALUE=DATE:20261107")) == {("2026-11-06", "2026-11-07", "X", "")}
assert parse_ics(ev(";TZID=Europe/Prague:20261106T100000", ";TZID=Europe/Prague:20261106T110000")) == {("2026-11-06T09:00:00Z", "2026-11-06T10:00:00Z", "X", "")}
H = 'BEGIN:VEVENT\nDTSTART:20261106T100000Z\nDTEND:20261106T110000Z\nSUMMARY:Konzultace IMS  <span class="normal-text">(rezervace)</span>\nLOCATION:B/C128  &amp;  D105\nEND:VEVENT'
assert parse_ics(H) == {("2026-11-06T10:00:00Z", "2026-11-06T11:00:00Z", "Konzultace IMS (rezervace)", "B/C128 & D105")}, parse_ics(H)
n = datetime(2026, 11, 1, 12, 0, tzinfo=PRAGUE)  # reservations only within a week; normal classes always
assert soon(("2026-11-04T10:00:00Z", "", "Konzultace IMS (rezervace)", ""), n) and not soon(("2026-11-11T10:00:00Z", "", "Konzultace IMS (rezervace)", ""), n)
assert soon(("2026-11-11T10:00:00Z", "", "IMS (P)", ""), n) and synced({"extendedProperties": {"private": {"src": "vut-kalendar"}}}) and not synced({"summary": "Konzultace IMS (rezervace)"})  # ownership is the tag, not the title
assert day_of("2026-10-21T22:30:00Z") == "2026-10-22" and day_of("2026-10-21") == "2026-10-21"
assert parse_points("12,5") == 12.5 and parse_points("12") == 12 and isinstance(parse_points("12"), int) and parse_points("-") is None and parse_points("") is None
assert parse_points("nan") is None and parse_points("inf") is None and parse_points("-inf") is None
rows = [["Název", "Poř. č.", "Min bodů", "Max bodů", "Body"], ["1. Projekt ISA (projekty)", "", "", "20", "0"], ["3. Zkouška ISA (závěrečná zkouška)", "", "20", "60", ""], ["4. Laby (hodnocení laboratoří)", "", "", "20", "7.25"]]
assert parse_items(rows) == [{"name": "Projekt ISA (projekty)", "min": None, "max": 20, "got": 0}, {"name": "Zkouška ISA (závěrečná zkouška)", "min": 20, "max": 60, "got": None},
                             {"name": "Laby (hodnocení laboratoří)", "min": None, "max": 20, "got": 7.25}], parse_items(rows)
assert cz_date("ne 22. listopadu 2026 23:59") == "2026-11-22T23:59" and cz_date("ne 22. listopadu 2026") is None
assert PLAIN_ZAP.search("ISA (zápočet)") and not PLAIN_ZAP.search("ITU (klasifikovaný zápočet)")
assert reminders("school", "ISA (zápočet)") == reminders("school", "IMP (L)") != reminders("school", "ITU (klasifikovaný zápočet)")
now, st = datetime(2026, 11, 1, 23, 50, tzinfo=PRAGUE), datetime(2026, 11, 2, 8, 0, tzinfo=PRAGUE)
assert (st.date() - now.date()).days == 1 and (st - now).days == 0  # date diff, not 24h chunks
assert stamp(st, "R") == "<t:1793602800:R>", stamp(st, "R")
# Studis + Moodle deadline for the same course within 60 min = one (Moodle's, it has the description); a floating Moodle time doesn't crash
import json, sync, tempfile
from pathlib import Path
sync.HERE = tmp = Path(tempfile.mkdtemp())
V = lambda s, summ: f"BEGIN:VEVENT\nDTSTART:{s}\nSUMMARY:{summ}\nCATEGORIES:ITU 26/27Z (302573)\nEND:VEVENT\n"  # noqa: E731
(tmp / "studis-moodle.ics").write_text("BEGIN:VCALENDAR\n" + V("20261120T225900Z", "Fáze I") + V("20261125T120000", "Floating") + "END:VCALENDAR\n")
(tmp / "studis.json").write_text(json.dumps({"deadlines": [{"course": "ITU", "title": "Projekt", "due": "2026-11-20T23:30"},  # 29 min from Fáze I: merged
                                                           {"course": "ITU", "title": "Pozdě", "due": "2026-11-21T01:59"},  # 2 h apart: kept
                                                           {"course": "IIS", "title": "Projekt", "due": "2026-11-20T23:59"}]}))  # other course: kept
dl = sorted(k[2] for k in sync.extra_events(set()) if " – odevzdání: " in k[2])
assert dl == ["IIS – odevzdání: Projekt", "ITU – odevzdání: Floating", "ITU – odevzdání: Fáze I", "ITU – odevzdání: Pozdě"], dl
# a failed course card keeps that course's previous items; others are refreshed
from fetch_studis import collect_items
def _load(url):
    if url == "bad":
        raise RuntimeError("boom")
    return [url]
assert collect_items([("A", "bad"), ("B", "b"), ("C", "bad")], {"A": ["old"], "B": ["oldb"]}, _load) == {"A": ["old"], "B": ["b"]}
# Registrace termínů, real text shapes: time-only end = the day before the term, several terms per course/kind each get their own window, registered has none
TXT = """IIS – INFORMAČNÍ SYSTÉMY (písemná část závěrečné zkoušky?)
út 5. ledna 2027 9:00 až 10:50 - 1. termín - Semestrální zkouška -
neregistrován (registrace bude probíhat od út 29. prosince 2026 9:00 do 9:00)
zkoušející: Novák Jan, doc. Ing., Ph.D., místnost: B/D105, přihlášení: 0/405 (min. 0)
út 19. ledna 2027 14:00 až 15:50 - 2. termín - Semestrální zkouška -
neregistrován (registrace bude probíhat od út 12. ledna 2027 14:00 do 14:00)
zkoušející: Novák Jan, doc. Ing., Ph.D., místnost: B/D105, přihlášení: 0/405 (min. 0)
IIS – INFORMAČNÍ SYSTÉMY (zápočet?)
pá 11. prosince 2026 11:55 až 12:00 - Zápočet - Zápočet -
hodnocení termínu: sběrný termín (aktuálně: 12 b.)
ITU – TVORBA UŽIVATELSKÝCH ROZHRANÍ (písemný test?)
út 8. prosince 2026 14:00 až 15:00 - ITU Test - Test -
registrován - vaše registrace již byla potvrzena
ISA – SÍŤOVÉ APLIKACE A SPRÁVA SÍTÍ (závěrečná zkouška?)
út 5. ledna 2027 9:00 až 10:50 - 1. termín - Zkouška ISA -
neregistrován (registrace bude probíhat od st 30. prosince 9:00 do ne 3. ledna 9:00)
IMP – MIKROPROCESOROVÉ A VESTAVĚNÉ SYSTÉMY (závěrečná zkouška)
čt 14. ledna 2027 11:00 až 12:50 - 1. termín - Písemná zkouška -
neregistrován (registrace probíhá od čt 7. ledna 2027 11:00 do st 13. ledna 2027 11:00) - plný
"""
T = {(t["course"], t["dates"][0]): t for t in parse_terms(TXT)}
assert len(T) == 5 and ("IIS", "2026-12-11T11:55") not in T
t = T["IIS", "2027-01-05T09:00"]
assert (t["opens"], t["closes"], t["registered"], t["free"], t["kind"]) == ("2026-12-29T09:00", "2027-01-04T09:00", False, True, "písemná část závěrečné zkoušky?")
assert (T["IIS", "2027-01-19T14:00"]["opens"], T["IIS", "2027-01-19T14:00"]["closes"]) == ("2027-01-12T14:00", "2027-01-18T14:00")
assert T["ITU", "2026-12-08T14:00"] == {"course": "ITU", "kind": "písemný test?", "registered": True, "opens": None, "closes": None, "free": False, "dates": ["2026-12-08T14:00"]}
assert (T["ISA", "2027-01-05T09:00"]["opens"], T["ISA", "2027-01-05T09:00"]["closes"]) == ("2026-12-30T09:00", "2027-01-03T09:00")  # year from the term, New Year rolled back
assert (T["IMP", "2027-01-14T11:00"]["opens"], T["IMP", "2027-01-14T11:00"]["closes"], T["IMP", "2027-01-14T11:00"]["free"]) == ("2027-01-07T11:00", "2027-01-13T11:00", False)
assert cz_date("29. prosince 9:00") is None and cz_date("29. prosince 9:00", 2026) == "2026-12-29T09:00"
# Harmonogram of any faculty: its own periods count, the university-wide (VUT) ones do not, the first faculty listed wins
HP = """OBDOBÍ VÝUKY V ZIMNÍM SEMESTRU (FEKT)
Termín: 14.9.2026 00:00 - 11.12.2026 23:59
VÝUKA TV V ZIMNÍM SEMESTRU (VUT)
OBDOBÍ VÝUKY V LETNÍM SEMESTRU (VUT)
Termín: 1.1.2027 00:00 - 2.1.2027 23:59
ZKOUŠKOVÉ OBDOBÍ ZIMNÍHO SEMESTRU (FEKT)
Termín: 4.1.2027 00:00 - 5.2.2027 23:59 ZKOUŠKY
OBDOBÍ VÝUKY V ZIMNÍM SEMESTRU (FSI)
Termín: 21.9.2026 00:00 - 18.12.2026 23:59
"""
assert parse_periods(HP) == [{"name": "výuka ZS", "start": "2026-09-14", "end": "2026-12-11"}, {"name": "zkouškové ZS", "start": "2027-01-04", "end": "2027-02-05"}], parse_periods(HP)
assert parse_periods("OBDOBÍ VÝUKY V ZIMNÍM SEMESTRU (FIT)\nTermín: 14.9.2026 00:00 - 11.12.2026 23:59\n")[0]["name"] == "výuka ZS"
print("ok")
