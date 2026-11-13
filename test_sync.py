# uv run python test_sync.py  -- plan/dedupe/brake/ics edge cases on synthetic data (no network, no Google)
import os, types
from datetime import datetime
import sync
from sync import TAG, PRAGUE, ZAPOC, attempt, SEP, desc_of, exam_desc, wanted_from, merged, extra_events, holiday_class, holidays, item_for, kind_of, parse_ics, plan, points_left, rooms, style, brake, dedupe_source, drop_redundant, reminders

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=PRAGUE)  # tomorrow = 2026-11-13
D = "2026-12-01T"
K = lambda h, m, summ, loc="", dur=50: (f"{D}{h:02}:{m:02}:00Z", f"{D}{h + (m + dur) // 60:02}:{(m + dur) % 60:02}:00Z", summ, loc)  # noqa: E731
G = lambda i, k, **kw: {"id": i, "summary": k[2], "location": k[3], "start": {"dateTime": k[0]}, "end": {"dateTime": k[1]}, "reminders": reminders("school", k[2]), "extendedProperties": TAG, **{f: v for f, v in style(k[2]).items() if v != "opaque"}, **({"description": merged("", desc_of(k))} if desc_of(k) else {}), **kw}  # noqa: E731
kinds = lambda ops: sorted(o[0] for o in ops)  # noqa: E731

# pass 1: exact (and cosmetically different) key -> keep; nothing else happens
w = K(9, 0, "ITU (P)", "B/D105")
assert plan({w}, [G("a", w)], NOW) == [("keep", G("a", w))]
assert kinds(plan({w}, [{**G("a", w), "summary": "ITU  (p)", "location": "b/d105"}], NOW)) == ["keep"]
# pass 2: room changed / end moved -> patch, same id
ops = plan({w}, [G("a", K(9, 0, "ITU (P)", "B/D206"))], NOW)
assert ops[0][:2] == ("patch", G("a", K(9, 0, "ITU (P)", "B/D206"))) and ops[0][2] == {"location": "B/D105", "description": merged(G("a", K(9, 0, "ITU (P)", "B/D206"))["description"], desc_of(w))} and len(ops) == 1, ops
ops = plan({w}, [G("a", K(9, 0, "ITU (P)", "B/D105", dur=100))], NOW)
assert ops[0][0] == "patch" and ops[0][2] == {"end": {"dateTime": w[1]}}, ops
# pass 3: moved class (same length, same Prague day) -> patch time; other day or length -> delete + insert
mv = K(11, 0, "ITU (P)", "B/D105")
ops = plan({mv}, [G("a", w)], NOW)
assert kinds(ops) == ["patch"] and ops[0][2] == {"start": {"dateTime": mv[0]}, "end": {"dateTime": mv[1]}}, ops
assert kinds(plan({mv}, [G("a", K(9, 0, "ITU (P)", "B/D105", dur=90))], NOW)) == ["delete", "insert"]
day2 = ("2026-12-02T09:00:00Z", "2026-12-02T09:50:00Z", "ITU (P)", "B/D105")
assert kinds(plan({day2}, [G("a", w)], NOW)) == ["delete", "insert"]
# Prague day, not UTC day: 23:30Z and 00:30Z the next UTC day are the same Prague day (winter, UTC+1 -> 00:30 and 01:30)
late = ("2026-12-01T23:30:00Z", "2026-12-02T00:20:00Z", "ITU (P)", ""); late2 = ("2026-12-02T00:30:00Z", "2026-12-02T01:20:00Z", "ITU (P)", "")
assert kinds(plan({late2}, [G("a", late)], NOW)) == ["patch"]
# consumed matching: one existing event never serves two wanted ones
a, b = K(13, 0, "ITU (P)", "x"), K(15, 0, "ITU (P)", "x")
assert kinds(plan({a, b}, [G("a", w)], NOW)) == ["insert", "patch"]
assert kinds(plan({K(9, 0, "ITU (P)", "r1"), K(9, 0, "ITU (P)", "r2")}, [G("a", K(9, 0, "ITU (P)", "r1"))], NOW)) == ["insert", "keep"]
# stale: deleted only when SYNCED and from tomorrow on; hand-made never touched
old = ("2026-11-12T09:00:00Z", "2026-11-12T09:50:00Z", "IMP (L)", "")  # today: already past in the export
hand = [G("h1", K(9, 0, "Zubař"), extendedProperties={}), G("h2", K(9, 0, "Zubař"), extendedProperties={}), G("h3", K(9, 0, "Kafe s Honzou", "bar"), extendedProperties={})]
ops = plan({w}, [G("a", w), G("o", old), G("s", K(14, 0, "IMS (P)")), *hand], NOW)
assert sorted((o[0], o[1]["id"]) for o in ops if o[0] != "insert") == [("delete", "s"), ("keep", "a")], ops
other = ("2026-12-01T09:00:00Z", "2026-12-01T09:50:00Z", "Kafe s Honzou", "bar2")  # a non-SYNCED wanted title next to a hand-made event: insert, never patch
assert kinds(plan({other}, hand, NOW)) == ["insert"]
# Google duplicates: extras of the same normalized key go, SYNCED only; keep the one with a description, else the oldest
wl = K(9, 0, "ITU (P)")  # no room: no generated description
ops = plan({wl}, [G("n", wl, created="2026-10-02"), {**G("o", wl, created="2026-10-01"), "summary": "ITU  (P)"}, G("d", wl, created="2026-10-03", description="x")], NOW)
assert sorted((o[0], o[1]["id"]) for o in ops) == [("delete", "n"), ("delete", "o"), ("keep", "d")], ops
ops = plan({wl}, [G("n", wl, created="2026-10-02"), G("o", wl, created="2026-10-01")], NOW)
assert sorted((o[0], o[1]["id"]) for o in ops) == [("delete", "n"), ("keep", "o")], ops
assert plan(set(), hand[:2], NOW) == []  # hand-made twins stay
# drift: reminders and (Moodle) description are patched on SYNCED events; user notes are not cleared; hand-made are not patched
ops = plan({wl}, [G("a", wl, reminders={"useDefault": True})], NOW)
assert ops[0][0] == "patch" and ops[0][2] == {"reminders": reminders("school", "ITU (P)")}, ops
dl = ("2026-12-06T22:29:00Z", "2026-12-06T22:59:00Z", "ITU – odevzdání: Fáze II", "")
sync.DESC[dl] = "Zadání\n  a text"
gen = "Zadání\n  a text"
assert plan({dl}, [G("a", dl)], NOW)[0][0] == "keep" and G("a", dl)["description"] == f"{SEP}\n{gen}"
assert plan({dl}, [G("a", dl, description="Zadání a text")], NOW)[0][2] == {"description": f"{SEP}\n{gen}"}  # same text without separator: converted, not duplicated
# marker merge: user text above the separator survives, a second run changes nothing, nothing is duplicated
for cur in ["old", "my note\n\nmore", f"my note\n\n{SEP}\nstale generated"]:
    ev = G("a", dl, description=cur)
    body = plan({dl}, [ev], NOW)[0][2]
    assert body["description"] == cur.partition(SEP)[0].rstrip().join(["", "\n\n"]) * bool(cur.partition(SEP)[0].strip()) + f"{SEP}\n{gen}", body
    assert body["description"].startswith(cur.partition(SEP)[0]) and body["description"].count(SEP) == 1 and body["description"].count("a text") == 1
    assert plan({dl}, [{**ev, **body}], NOW)[0][0] == "keep"  # idempotent
assert merged("", "") is None and merged("my note", "") is None and merged(f"x\n{SEP}\n{gen}  ", gen) is None

assert plan({wl}, [G("a", wl, description="my note")], NOW)[0][0] == "keep"
hm = ("2026-12-01T09:00:00Z", "2026-12-01T09:50:00Z", "Zubař", "")
assert plan({hm}, [G("a", hm, extendedProperties={}, reminders={"useDefault": True})], NOW)[0][0] == "keep"

# source duplicates
s1, s2 = ("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", "ITU (písemný test)", ""), ("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", "ITU  (Písemný TEST)", "A112")
assert dedupe_source({s1, s2}) == {s2}  # same event from two sources: the one with a room
s3 = ("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", "ITU (písemný test - 1. termín)", "")
assert len(dedupe_source({s1, s3})) == 2  # a different title is a different event, no fuzzy merge
assert dedupe_source({s1, ("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", "ITU (písemný test) - Svoboda Petr, doc. Ing., Ph.D. [/]", "A112")}) == {("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", "ITU (písemný test) - Svoboda Petr, doc. Ing., Ph.D. [/]", "A112")}
x = lambda t, loc="": ("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", t, loc)  # noqa: E731
for a, b in [("ITU – odevzdání: Fáze I", "ITU – odevzdání: Fáze II"), ("ITU – odevzdání: Projekt 1", "ITU – odevzdání: Projekt 2"), ("ITU (zkouška 1)", "ITU (zkouška 10)")]:
    assert len(dedupe_source({x(a), x(b)})) == 2, (a, b)
assert len(dedupe_source({x("ITU (P)", "A"), x("ITU (P)", "B")})) == 2 and len(dedupe_source({x("ITU (P)", "A"), x("ITU (P)", "a")})) == 1  # parallel rooms stay
cls = ("2026-12-08T13:00:00Z", "2026-12-08T14:50:00Z", "ITU (P)", "B/D0206")
assert dedupe_source({s1, cls}) == {s1, cls}  # exam + class at the same start stay two
assert dedupe_source({s1, ("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", "IMS (písemný test)", "")}) != {s1}  # other course stays
assert len(dedupe_source({s1, ("2026-12-08T13:00:00Z", "2026-12-08T15:00:00Z", "ITU (klasifikovaný zápočet)", "")})) == 2  # different exams stay
u = lambda t: ("2026-12-07T23:00:00Z", "2026-12-08T23:00:00Z", t, "")  # noqa: E731
assert len(dedupe_source({u("Učení: ITU (písemný test)"), u("Učení: ITU (písemný test - 1. termín)"), u("Učení: IMS (písemný test)")})) == 2
d1, d2 = ("2026-12-06T22:29:00Z", "2026-12-06T22:59:00Z", "ITU – odevzdání: Fáze II", ""), ("2026-12-06T22:29:00Z", "2026-12-06T22:59:00Z", "ITU – odevzdání: Fáze II.", "")
assert len(dedupe_source({d1, d2, ("2026-12-06T22:29:00Z", "2026-12-06T22:59:00Z", "ITU – odevzdání: Prezentace", "")})) == 2
# reservations: redundant when a class of the same course sits in the same room at the same time
res = lambda loc, h=9: (*K(h, 0, "Konzultace IMS (rezervace)", loc)[:2], "Konzultace IMS (rezervace)", loc)  # noqa: E731
c = K(9, 0, "IMS (P)", "B/D0206")
assert drop_redundant({c, res("B/D0206")}) == {c} and drop_redundant({c, res("B/C128")}) == {c, res("B/C128")}
assert drop_redundant({c, res("")}) == {c, res("")} and drop_redundant({c, res("B/D0206", 11)}) == {c, res("B/D0206", 11)}

# brake
fut = [G(f"e{i}", ("2026-12-%02dT09:00:00Z" % (i + 1), "2026-12-%02dT09:50:00Z" % (i + 1), "ITU (P)", "")) for i in range(20)]
keep = {(e["start"]["dateTime"], e["end"]["dateTime"], e["summary"], "") for e in fut[:10]}
ops = plan(keep, fut, NOW)
assert brake(ops, fut, NOW, False) == "would delete 10 of 20 future synced events", brake(ops, fut, NOW, False)
assert brake(plan({*keep, *{(e["start"]["dateTime"], e["end"]["dateTime"], "ITU (P)", "") for e in fut[10:]}}, fut, NOW), fut, NOW, False) == ""
assert brake(plan({e for e in (tuple(x) for x in [(f["start"]["dateTime"], f["end"]["dateTime"], "ITU (P)", "") for f in fut[:17]])}, fut, NOW), fut, NOW, False) == ""  # 3 stale: under 5
assert brake(plan(set(), fut, NOW), fut, NOW, True) and brake([], fut, NOW, True) == ""  # empty/HTML source only matters if it would delete
big = [G(f"b{i}", ("2026-12-%02dT09:00:00Z" % (i + 1), "2026-12-%02dT09:50:00Z" % (i + 1), "ITU (P)", "")) for i in range(30)]
assert brake(plan({tuple(x) for x in [(f["start"]["dateTime"], f["end"]["dateTime"], "ITU (P)", "") for f in big[:22]]}, big, NOW), big, NOW, False) == ""  # 8 of 30 = 27%

# zápočet placeholders: pruned like any SYNCED event (from tomorrow), not counted by the brake
zp = lambda n, d: (f"2026-12-{d:02}T06:00:00Z", f"2026-12-{d + 6:02}T22:59:00Z", n, "")  # noqa: E731
Z = [G(f"z{i}", zp(n, 7 + i)) for i, n in enumerate(["ISA (zápočet)", "IIS (zápočet)", "ITU (klasifikovaný zápočet)", "Učení: ITU (klasifikovaný zápočet)"] * 3)]
Z.append(G("zold", ("2026-11-12T06:00:00Z", "2026-11-12T07:00:00Z", "ISA (zápočet)", "")))
ops = plan(set(), Z, NOW)
assert sorted(o[1]["id"] for o in ops if o[0] == "delete") == sorted(e["id"] for e in Z if e["id"] != "zold")  # today's is never pruned
assert brake(ops, Z, NOW, False) == "" and brake(ops, Z + fut, NOW, False) == ""
assert brake(plan(set(), Z + fut, NOW), Z + fut, NOW, False) == "would delete 20 of 20 future synced events"

past = G("zp", ("2026-11-02T06:00:00Z", "2026-11-20T22:59:00Z", "ISA (zápočet)", ""))  # window started in the past, still running: pruned
gone = G("zg", ("2026-10-02T06:00:00Z", "2026-11-10T22:59:00Z", "ISA (zápočet)", ""))  # ended: kept
cls_ = G("zc", ("2026-11-02T06:00:00Z", "2026-11-20T22:59:00Z", "ITU (P)", ""))  # not zápočet, started in the past: kept
ops = plan(set(), [past, gone, cls_], NOW)
assert [(o[0], o[1]["id"]) for o in ops] == [("delete", "zp")] and brake(ops, fut + [past], NOW, False) == ""

# only the university placeholders are hidden, not real work that mentions zápočet
assert all(ZAPOC.fullmatch(x) for x in ["ISA (zápočet)", "ITU (klasifikovaný zápočet)", "Učení: ITU (klasifikovaný zápočet)", "IIS (Zápočet)"])
assert not any(ZAPOC.fullmatch(x) for x in ["ITU – odevzdání: Zápočtový projekt", "ITU – odevzdání: zápočtová písemka", "ITU (písemný test)", "ISA (zápočet) - oprava"])
# a delete that finds the event already gone (404/410) is success
class Req:
    def __init__(self, st): self.st = st
    def execute(self, num_retries=0): raise type("E", (Exception,), {"resp": type("R", (), {"status": self.st})()})()
f = []
assert attempt(Req(404), "x", f, gone_ok=True) == {} and attempt(Req(410), "x", f, gone_ok=True) == {} and not f
attempt(Req(404), "x", f) ; attempt(Req(500), "y", f, gone_ok=True)
assert f == ["x", "y"]

# colours and busy/free: one colour per kind, lectures free; drift patches both
kinds_ = {"ITU (P)": "lecture", "ITU (L)": "lab", "ILI (Cp)": "lab", "IMP (S)": "lab", "IIS (písemná část půlsemestrální zkoušky)": "exam", "ITU (písemný test)": "exam",
          "ITU – odevzdání: Fáze I": "deadline", "Učení: ITU (písemný test)": "study", "Konzultace IMS (rezervace)": "reservation", "Zubař": None}
assert {k: kind_of(k) for k in kinds_} == kinds_ and len({style(k).get("colorId") for k in kinds_ if kinds_[k]}) == 6
assert style("ITU (P)")["transparency"] == "transparent" and style("ITU (L)")["transparency"] == "opaque" and "colorId" not in style("Zubař")
ops = plan({wl}, [G("a", wl, colorId="3", transparency="opaque")], NOW)
assert ops[0][2] == {"colorId": "7", "transparency": "transparent"}, ops
lab = K(9, 0, "ITU (L)")
assert plan({lab}, [G("a", lab, transparency="transparent")], NOW)[0][2] == {"transparency": "opaque"}
assert plan({lab}, [G("a", lab)], NOW)[0][0] == "keep" and plan({hm}, [G("a", hm, extendedProperties={}, colorId="3")], NOW)[0][0] == "keep"  # hand-made keeps its colour
# public holidays: fixed + Easter Friday/Monday (Easter 2026-04-05, 2027-03-28)
assert {"2026-04-03", "2026-04-06", "2027-03-26", "2027-03-29", "2026-11-17", "2026-10-28", "2026-12-24", "2026-05-08"} <= holidays(2026) | holidays(2027) and "2026-04-05" not in holidays(2026)
assert holiday_class(("2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z", "ITU (P)", "")) and not holiday_class(("2026-11-18T09:00:00Z", "2026-11-18T10:00:00Z", "ITU (P)", ""))
assert not holiday_class(("2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z", "ITU (písemný test)", "")) and not holiday_class(("2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z", "ITU – odevzdání: X", ""))
assert holiday_class(("2026-11-16T23:30:00Z", "2026-11-17T00:20:00Z", "ITU (L)", ""))  # Prague-local day, not UTC
# room links (location text stays plain, the link goes in the description)
assert rooms("B/D0206 (Božetěchova 1/2, Brno 61200) [B/D0206]") == "Místnost D0206: https://www.fit.vut.cz/fit/room/D0206/.cs"
assert rooms("B/L306.2") == "Místnost L306.2: https://www.fit.vut.cz/fit/room/L306.2/.cs" and rooms("A112") == ""  # only FIT (B/) rooms get a link and rooms("") == "" == rooms("virtuální místnost")
assert rooms("B/D105, B/E104").count("\n") == 1 and rooms("A/X100") == ""
# exam points: item matched on the parenthesised kind (case/diacritics ignored), "lze získat" shared with the bot
its = {"IIS": [{"name": "Projekt (projekty)", "min": 1, "max": 30, "got": 0}, {"name": "Půlsemestrální zkouška (písemná část půlsemestrální zkoušky)", "min": None, "max": 19, "got": None},
               {"name": "Zápočet (zápočet)", "min": 25, "max": None, "got": None}, {"name": "Semestrální zkouška (Písemná část ZÁVĚREČNÉ zkoušky)", "min": 20, "max": 51, "got": None}],
       "ILI": [{"name": "Bonusové body (písemný test)", "min": None, "max": 20, "got": None}, {"name": "Závěrečný test (písemný test)", "min": 0, "max": 80, "got": None}]}
assert item_for(its, "IIS", "písemná část půlsemestrální zkoušky")["max"] == 19 and item_for(its, "IIS", "pisemna cast zaverecne zkousky")["max"] == 51
assert item_for(its, "IIS", "písemný test") is None and item_for(its, "XXX", "x") is None and item_for(its, "ILI", "písemný test")["max"] == 80
assert points_left("IIS", its["IIS"]) == (100, ["Projekt 30 (min 1)", "Půlsemestrální zkouška 19", "Semestrální zkouška 51 (min 20)"])
assert exam_desc("IIS", item_for(its, "IIS", "písemná část půlsemestrální zkoušky"), 12, its, "Novák Jan, doc. Ing., Ph.D.") == \
    "Max. 19 b.\nV předmětu zatím 12 b., ještě lze získat: Projekt 30 (min 1), Půlsemestrální zkouška 19, Semestrální zkouška 51 (min 20)\nVyučující: Novák Jan, doc. Ing., Ph.D."
assert exam_desc("IIS", None, 12, its, "T. T.") == "Vyučující: T. T." and exam_desc("IIS", None, None, its, "") == ""
# exam events end to end: teacher + points in the description, study block sized by the points at stake
import json, tempfile
from pathlib import Path
sync.HERE = tmp = Path(tempfile.mkdtemp())
X = lambda c, d: f"BEGIN:VEVENT\nDTSTART:2099{d}T080000Z\nDTEND:2099{d}T090000Z\nSUMMARY:{c}(písemný test) - Svoboda Petr\\, doc. Ing.\\, Ph.D. [/]\nEND:VEVENT\n"  # noqa: E731
(tmp / "studis-exams.ics").write_text("BEGIN:VCALENDAR\n" + X("AAA", "0310") + X("BBB", "0310") + X("CCC", "0310") + X("DDD", "0310") + "END:VCALENDAR\n")
mk = lambda mx: [{"name": "Test (písemný test)", "min": 5, "max": mx, "got": None}]  # noqa: E731
(tmp / "studis.json").write_text(json.dumps({"points": {"AAA": 7, "BBB": None}, "items": {"AAA": mk(10), "BBB": mk(25), "CCC": mk(51)}}))
sync.DESC.clear()
ev = extra_events(set())
study = lambda c: sorted((k[0], k[1]) for k in ev if k[2] == f"Učení: {c}(písemný test)".replace("(", " (", 1))  # noqa: E731
assert study("AAA") == [("2099-03-09T16:00:00Z", "2099-03-09T20:00:00Z")], study("AAA")  # <20: 17:00-21:00 the evening before (Prague)
assert study("BBB") == [("2099-03-08T23:00:00Z", "2099-03-09T23:00:00Z")] and study("DDD") == study("BBB")  # 20-39 and no match: the whole day before
assert study("CCC") == [("2099-03-07T23:00:00Z", "2099-03-08T23:00:00Z"), ("2099-03-08T23:00:00Z", "2099-03-09T23:00:00Z")]  # >=40: two whole days
ak = next(k for k in ev if k[2] == "AAA (písemný test)")
assert sync.DESC[ak] == "Max. 10 b., min. 5 b.\nV předmětu zatím 7 b., ještě lze získat: Test 10 (min 5)\nVyučující: Svoboda Petr, doc. Ing., Ph.D.", sync.DESC[ak]
assert sync.DESC[next(k for k in ev if k[2] == "DDD (písemný test)")] == "Vyučující: Svoboda Petr, doc. Ing., Ph.D."
assert len({k for k in ev if k[2].startswith("Učení: CCC")}) == 2 and kinds(plan(ev, [], NOW)) == ["insert"] * len(ev)

# study blocks already over are not created; today's, still running, is
NOWU = datetime(2099, 3, 9, 14, 0, tzinfo=PRAGUE)
sync.DESC.clear()
(tmp / "studis-exams.ics").write_text("BEGIN:VCALENDAR\n" + X("AAA", "0310") + X("CCC", "0310") + "END:VCALENDAR\n")
ev = extra_events(set(), NOWU)  # AAA evening block (17-21 on the 9th) is still ahead; CCC's 8th is over, its 9th is running
assert sorted(k[2] for k in ev if k[2].startswith("Učení")) == ["Učení: AAA (písemný test)", "Učení: CCC (písemný test)"]
assert [k[0] for k in ev if k[2].startswith("Učení: CCC")] == ["2099-03-08T23:00:00Z"]
assert not [k for k in extra_events(set(), datetime(2099, 3, 9, 22, 0, tzinfo=PRAGUE)) if k[2].startswith("Učení: AAA")]  # past 21:00: gone
# filter order: a reservation duplicating a class is dropped even when a holiday removes the class
hc, hr = ("2026-11-17T09:00:00Z", "2026-11-17T10:50:00Z", "IMS (P)", "B/D0206"), ("2026-11-17T09:00:00Z", "2026-11-17T10:50:00Z", "Konzultace IMS (rezervace)", "B/D0206")
assert wanted_from({hc, hr}) == set() and wanted_from({hr}) == {hr}
# stale generated text: separator + generated part go, the user's text stays; SYNCED events only
assert plan({wl}, [G("a", wl, description=f"my note\n\n{SEP}\nstale")], NOW)[0][2] == {"description": "my note"}
assert plan({wl}, [G("a", wl, description=f"{SEP}\nstale")], NOW)[0][2] == {"description": ""}
assert plan({wl}, [G("a", wl, description="")], NOW)[0][0] == "keep" and plan({hm}, [G("a", hm, extendedProperties={}, description=f"n\n{SEP}\nstale")], NOW)[0][0] == "keep"

# ownership: only events carrying the tag are patched or deleted, whatever their title; untagged ones are hand-made
mine_ = K(9, 0, "ITU (P)", "B/D206")
untagged = lambda i, k, **kw: G(i, k, extendedProperties={}, **kw)  # noqa: E731
assert [o[0] for o in plan({w}, [untagged("a", mine_)], NOW)] == ["insert"]  # looks like ours (title, room differs) but is not: never patched, ours is inserted next to it
assert [o[0] for o in plan(set(), [untagged("a", K(9, 0, "ITU (P)")), untagged("b", K(10, 0, "Učení: ITU (zkouška)"), description="x")], NOW)] == []  # not deleted as stale
ops = plan({w}, [untagged("a", w), G("b", w), untagged("c", w)], NOW)
assert sorted((o[0], o[1]["id"]) for o in ops) == [("keep", "b")]  # untagged twins of a wanted key: kept, nothing deleted, nothing inserted
assert [o[0] for o in plan({w}, [untagged("a", w)], NOW)] == ["keep"] and plan({w}, [untagged("a", w, colorId="3")], NOW)[0][0] == "keep"  # exact match with a hand-made event: not touched
assert [o[0] for o in plan({w}, [untagged("a", w), untagged("b", w, created="2020")], NOW)] == ["keep"]  # untagged duplicates are not "duplicates"
assert sync.synced({"extendedProperties": {"private": {"src": "vut-kalendar"}}}) and not sync.synced({"summary": "ITU (P)"}) and not sync.synced({"extendedProperties": {"private": {"src": "x"}}})
# hyphenated course codes (other faculties) work everywhere a code is parsed
H = "BPC-ABC"
assert sync.COURSE.match(f"{H} (P)") and ZAPOC.fullmatch(f"{H} (zápočet)") and ZAPOC.fullmatch(f"Učení: {H} (klasifikovaný zápočet)") and kind_of(f"{H} (P)") == "lecture" and kind_of(f"{H} – odevzdání: X") == "deadline"
assert points_left(H, [{"name": f"Projekt {H} (projekty)", "min": None, "max": 20, "got": 0}]) == (20, ["Projekt 20"])
(tmp / "studis-exams.ics").write_text("BEGIN:VCALENDAR\n" + X(H, "0310") + "END:VCALENDAR\n")
(tmp / "studis.json").write_text(json.dumps({"items": {H: mk(10)}, "points": {H: 3}}))
sync.DESC.clear()
ev = extra_events(set(), NOWU)
assert any(k[2] == f"{H} (písemný test)" for k in ev) and any(k[2] == f"Učení: {H} (písemný test)" for k in ev), sorted(k[2] for k in ev)  # title normalized, study block made
assert sync.DESC[next(k for k in ev if k[2] == f"{H} (písemný test)")].startswith("Max. 10 b., min. 5 b.")  # the item matched
# .env next to the code is read with setdefault semantics; calendars are paged
envd = Path(tempfile.mkdtemp())
(envd / ".env").write_text('# c\nCALENDAR_NAME="Moje škola"\nSKIP_TITLES=^X$\n')
old_home, sync.HERE = sync.HERE, envd
os.environ.pop("CALENDAR_NAME", None)
os.environ["SKIP_TITLES"] = "set-in-env"
sync.load_env()
sync.HERE = old_home
assert os.environ["CALENDAR_NAME"] == "Moje škola" and os.environ["SKIP_TITLES"] == "set-in-env"
os.environ.pop("CALENDAR_NAME"); os.environ.pop("SKIP_TITLES")
pages = [{"items": [{"id": 1}], "nextPageToken": "t"}, {"items": [{"id": 2}]}]
class CL:
    def list(self, pageToken=None): return types.SimpleNamespace(execute=lambda num_retries=0: pages[0 if pageToken is None else 1])
assert [c["id"] for c in sync.all_calendars(types.SimpleNamespace(calendarList=lambda: CL()))] == [1, 2]
# an empty or garbled token.json gives the clear message (needs the Google libraries; skipped where they are not installed)
try:
    import google.oauth2.credentials  # noqa: F401
except ImportError:
    pass
else:
    old_home, sync.HERE = sync.HERE, envd
    for content in ("", "{garbled", "{}"):
        (envd / "token.json").write_text(content)
        try:
            sync.service()
            raise SystemExit("expected RuntimeError")
        except RuntimeError as e:
            assert "--login" in str(e) and "GOOGLE_TOKEN" in str(e), e
    sync.HERE = old_home

# iCal edge cases
V = lambda *lines: "BEGIN:VCALENDAR\n" + "".join(f"BEGIN:VEVENT\n{chr(10).join(ln)}\nEND:VEVENT\n" for ln in lines) + "END:VCALENDAR\n"  # noqa: E731
assert parse_ics(V(["DTSTART:20261106T100000Z", "DTEND:20261106T110000Z", "SUMMARY:X", "STATUS:CANCELLED"])) == set()
assert parse_ics(V(["DTSTART:20261106T100000", "DTEND:20261106T110000", "SUMMARY:X"])) == {("2026-11-06T09:00:00Z", "2026-11-06T10:00:00Z", "X", "")}  # floating = Prague
assert parse_ics(V(["DTSTART:20260706T100000", "DTEND:20260706T110000", "SUMMARY:X"])) == {("2026-07-06T08:00:00Z", "2026-07-06T09:00:00Z", "X", "")}
assert parse_ics(V(["DTSTART:20261106T100000Z", "DURATION:PT1H30M", "SUMMARY:X"])) == {("2026-11-06T10:00:00Z", "2026-11-06T11:30:00Z", "X", "")}
assert parse_ics(V(["DTSTART:20261106T100000Z", "SUMMARY:X"])) == {("2026-11-06T10:00:00Z", "2026-11-06T10:00:00Z", "X", "")}
assert parse_ics(V(["DTSTART;VALUE=DATE:20261106", "SUMMARY:X"])) == {("2026-11-06", "2026-11-07", "X", "")}
assert parse_ics(V(["DTSTART:20261106T100000Z", "DTEND:20261106T110000Z", r"SUMMARY:A\, B\; C\nD", r"LOCATION:Room\, 1"])) == {("2026-11-06T10:00:00Z", "2026-11-06T11:00:00Z", "A, B; C D", "Room, 1")}
R = lambda rule: parse_ics(V(["DTSTART:20261106T100000Z", "DTEND:20261106T110000Z", "SUMMARY:X", f"RRULE:{rule}"]))  # noqa: E731
assert sorted(k[0] for k in R("FREQ=WEEKLY;COUNT=3")) == ["2026-11-06T10:00:00Z", "2026-11-13T10:00:00Z", "2026-11-20T10:00:00Z"]
assert len(R("FREQ=DAILY;UNTIL=20261109T235959Z")) == 4 and len(R("FREQ=WEEKLY;INTERVAL=2;UNTIL=20261207")) == 3
assert len(R("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=5")) == 1 and len(R("FREQ=WEEKLY")) == 1 and len(R("FREQ=MONTHLY;COUNT=3")) == 1  # first instance only (+ warning)
assert sorted(k[0] for k in parse_ics(V(["DTSTART:20261022T080000Z", "DTEND:20261022T090000Z", "SUMMARY:X", "RRULE:FREQ=WEEKLY;COUNT=2"]))) == ["2026-10-22T08:00:00Z", "2026-10-29T08:00:00Z"]  # UTC source steps in UTC
# DST (25 Oct 2026 and 29 Mar 2026): Prague wall-clock time is stable, UTC moves
T = lambda d: next(iter(parse_ics(V([f"DTSTART;TZID=Europe/Prague:{d}T100000", f"DTEND;TZID=Europe/Prague:{d}T110000", "SUMMARY:X"]))))[:2]  # noqa: E731
assert T("20261024") == ("2026-10-24T08:00:00Z", "2026-10-24T09:00:00Z") and T("20261026") == ("2026-10-26T09:00:00Z", "2026-10-26T10:00:00Z")
assert T("20260328")[0] == "2026-03-28T09:00:00Z" and T("20260330")[0] == "2026-03-30T08:00:00Z"
W = lambda d: sorted(k[0] for k in parse_ics(V([f"DTSTART;TZID=Europe/Prague:{d}T100000", f"DTEND;TZID=Europe/Prague:{d}T110000", "SUMMARY:X", "RRULE:FREQ=WEEKLY;COUNT=2"])))  # noqa: E731
assert W("20261022") == ["2026-10-22T08:00:00Z", "2026-10-29T09:00:00Z"] and W("20260326") == ["2026-03-26T09:00:00Z", "2026-04-02T08:00:00Z"]
print("ok")

# clean(): entities are decoded before tags are stripped, so encoded markup never comes back to life (also when encoded twice)
from sync import clean  # noqa: E402
assert clean('X  <span class="normal-text">(rezervace)</span>') == "X (rezervace)" and clean("A &amp; B") == "A & B"
for enc in ("&lt;b&gt;x&lt;/b&gt;", "&amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;", "&lt;script&gt;x", "<b>x</b>"):
    assert clean(enc) in ("x", "scriptx") and "<" not in clean(enc) and clean(clean(enc)) == clean(enc), clean(enc)
print("clean ok")
