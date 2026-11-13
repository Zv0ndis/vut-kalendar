# uv run python test_api.py  -- VUT API client + API -> file conversion (no network: urlopen/get are replaced)
import json, os, stat, sys, tempfile, time, types, urllib.error, urllib.request
from unittest import mock
from datetime import datetime
from pathlib import Path
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules.setdefault("playwright.sync_api", types.SimpleNamespace(sync_playwright=None))
import fetch_studis as fs, vutapi
from sync import PRAGUE, parse_ics

# ---- token cache: one token shared through a 0600 file, minted only when none is left
tmp = Path(tempfile.mkdtemp())
vutapi.CACHE = tmp / "tok.json"
os.environ.update(VUT_API_CLIENT_ID="cid", VUT_API_SECRET="s3cr3t-value")
minted, real_mint = [], vutapi._mint
def fake_mint():
    minted.append(1)
    return {"access_token": f"tok{len(minted)}", "expires_at": time.time() + 3600}
vutapi._mint = fake_mint
assert vutapi.token() == "tok1" and vutapi.token() == "tok1" and len(minted) == 1
assert stat.S_IMODE(os.stat(vutapi.CACHE).st_mode) == 0o600
assert vutapi.token(rejected="tok1") == "tok2" and vutapi.token() == "tok2"
assert vutapi.token(rejected="tok1") == "tok2" and len(minted) == 2  # someone else renewed it already: the rejected token is not the cached one, no new mint
vutapi.CACHE.write_text(json.dumps({"access_token": "old", "expires_at": time.time() + 200}))  # inside the 5 min spare: renewed
assert vutapi.token() == "tok3"
vutapi.CACHE.write_text("{broken")
assert vutapi.token() == "tok4"  # corrupt cache: minted again, not a crash
vutapi.CACHE.write_text("[1]")
assert vutapi.token() == "tok5"  # not even a dict
# a leftover temp file (wrong mode, junk) is replaced; the cache stays 0600
tmpf = vutapi.CACHE.with_name(vutapi.CACHE.name + ".tmp")
tmpf.write_text("junk")
os.chmod(tmpf, 0o666)
vutapi.CACHE.unlink()
assert vutapi.token() == "tok6" and not tmpf.exists() and stat.S_IMODE(os.stat(vutapi.CACHE).st_mode) == 0o600
# a disk that refuses the write still gives this run its token
vutapi.CACHE.unlink()
with mock.patch.object(vutapi.os, "replace", side_effect=OSError("disk full")):
    assert vutapi.token() == "tok7" and not vutapi.CACHE.exists()

# ---- failed minting leaves a marker: the next runs fail fast without touching the network or the quota
def failing_mint():
    minted.append(1)
    raise vutapi.TokenError("token request failed: HTTP 429")
vutapi._mint = failing_mint
n = len(minted)
for _ in range(3):
    try:
        vutapi.token()
        raise SystemExit("expected TokenError")
    except vutapi.TokenError:
        pass
assert len(minted) == n + 1 and json.loads(vutapi.CACHE.read_text())["fail_until"] > time.time() + 800  # one attempt, then paused
vutapi.CACHE.write_text(json.dumps({"fail_until": time.time() - 1}))
vutapi._mint = fake_mint
assert vutapi.token().startswith("tok")  # pause over: minted again

# ---- GET: a 401 swaps the token once; the second one stops the run; errors never carry the secret or the token
class Resp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, *a): return self.body
def opener(fn): return types.SimpleNamespace(open=fn)
calls = []
def open_401_once(req, timeout=0):
    calls.append(req.get_header("Authorization"))
    if len(calls) == 1:
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
    return Resp(b'{"data": {"x": 1}}')
vutapi.OPENER = opener(open_401_once)
before = len(minted)
assert vutapi.get("/api/x", a="b") == {"data": {"x": 1}} and len(calls) == 2 and calls[0] != calls[1] and len(minted) == before + 1
def open_always_401(req, timeout=0):
    calls.append(req.get_header("Authorization"))
    raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
vutapi._state.update(refreshed=False, dead=False)
vutapi.OPENER = opener(open_always_401)
calls.clear()
before = len(minted)
for _ in range(2):
    try:
        vutapi.get("/api/x")
        raise SystemExit("expected TokenError")
    except vutapi.TokenError as e:
        assert "s3cr3t" not in str(e) and not any(f"tok{i}" in str(e) for i in range(30))
assert len(calls) == 2 and len(minted) == before + 1  # one swap, then dead: the second get() did not even call the API
def boom(req, timeout=0): raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, None)
vutapi._state.update(refreshed=False, dead=False)
vutapi.OPENER = opener(boom)
try:
    vutapi.get("/api/missing")
    raise SystemExit("expected ApiError")
except vutapi.ApiError as e:
    assert str(e) == "GET /api/missing: HTTP 404" and "s3cr3t" not in str(e) and "tok" not in str(e)

# ---- minting: response validated, expiry clamped, credentials and redirects kept out
vutapi._mint = real_mint
def minting(body, status=200):
    def f(req, timeout=0):
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, None)
        return Resp(body)
    vutapi.OPENER = opener(f)
for bad in (b"{}", b'{"access_token": ""}', b'{"access_token": 5}', b'{"access_token": "t", "expires_in": "soon"}', b"[1]", b"nope"):
    minting(bad)
    try:
        vutapi._mint()
        raise SystemExit(f"expected TokenError for {bad!r}")
    except vutapi.TokenError as e:
        assert "s3cr3t" not in str(e)
minting(b'{"access_token": "t", "expires_in": 99999999}')
assert 86300 < vutapi._mint()["expires_at"] - time.time() <= 86400
minting(b'{"access_token": "t", "expires_in": 5}')
assert 590 < vutapi._mint()["expires_at"] - time.time() <= 600
minting(b"", status=429)
try:
    vutapi._mint()
    raise SystemExit("expected TokenError")
except vutapi.TokenError as e:
    assert str(e) == "token request failed: HTTP 429" and "s3cr3t" not in str(e) and "cid" not in str(e)
assert any(isinstance(h, vutapi.NoRedirect) for h in urllib.request.build_opener(vutapi.NoRedirect).handlers)
assert vutapi.NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil.example/") is None  # no Authorization forwarded to a redirect target

# ---- API -> the files sync reads
assert fs.utc("2026-10-24T00:00:00", "1899-12-30T10:00:00") == "20261024T080000Z" and fs.utc("2026-10-26T00:00:00", "1899-12-30T10:00:00") == "20261026T090000Z"  # 25 Oct
assert fs.utc("2026-03-28T00:00:00", "1899-12-30T10:00:00") == "20260328T090000Z" and fs.utc("2026-03-30T00:00:00", "1899-12-30T10:00:00") == "20260330T080000Z"  # 29 Mar
assert fs.utc_dt("2026-12-08T14:00:00") == "20261208T130000Z"
assert fs.room_text(["B/D0206"]) == "B/D0206 (Božetěchova 1/2, Brno 61200) [B/D0206]" and fs.room_text(["B/D0206", "B/D105"]) == "B/D0206 & B/D105 (Božetěchova 1/2, Brno 61200)"
assert fs.room_text([]) == "" and fs.room_text(["B/C128"], address=False) == "B/C128" and fs.room_text(["X/1"]) == "X/1"
resp = {"data": {"vyucovani": [{"vyucovani_id": 1, "bloky": [{"vyucovani_blok_id": 2, "dny": [
    {"vyuc_blok_den_id": 10, "mistnosti": [{"zruseno": 0, "nahradni": 0, "mistnost_label": "B/D0206"}, {"zruseno": 0, "nahradni": 0, "mistnost_label": "B/D105"}]},
    {"vyuc_blok_den_id": 11, "mistnosti": [{"zruseno": 1, "nahradni": 0, "mistnost_label": "B/D0206"}, {"zruseno": 0, "nahradni": 1, "mistnost_label": "B/E112"}]}]}]}]}}
rooms = fs.day_rooms(resp)
assert rooms == {10: ["B/D0206", "B/D105"], 11: ["B/E112"]}  # cancelled rooms out, the substitute in
day = lambda i, d, z=0: {"vbd_vyuc_blok_den_id": i, "vbd_datum": d + "T00:00:00", "vbd_zruseno": z, "osoba_zruseno": 0, "zruseno": 0}  # noqa: E731
vy = [{"v_vyucovani_id": 1, "v_p_zkratka": "ISA", "v_trj_zkratka": "P", "bloky": [{"vb_cas_od": "1899-12-30T08:00:00", "vb_cas_do": "1899-12-30T09:50:00",
       "dny": [day(10, "2026-11-13"), day(11, "2026-11-20", z=1), day(12, "2026-11-27")]}]}]
resv = [{"zruseno": 0, "datum": "2026-11-26T00:00:00", "cas_od": "1899-12-30T13:00:00", "cas_do": "1899-12-30T16:50:00", "popis_verejny": "", "popis": "konzultace IMS", "mistnost_label": "B/C128"},
        {"zruseno": 1, "datum": "2026-11-27T00:00:00", "cas_od": "1899-12-30T13:00:00", "cas_do": "1899-12-30T16:50:00", "popis_verejny": "x", "popis": "x", "mistnost_label": "B/C128"},
        {"zruseno": 0, "datum": "2026-11-05T00:00:00", "cas_od": "1899-12-30T13:00:00", "cas_do": "1899-12-30T16:50:00", "popis_verejny": "over", "popis": "", "mistnost_label": "B/C128"}]
ev = fs.timetable_events(vy, {10: ["B/D0206", "B/D105"], 12: ["B/E112"]}, resv, datetime(2026, 11, 12, 12, 0, tzinfo=PRAGUE))
assert ev == [("20261113T070000Z", "20261113T085000Z", "ISA (P)", "B/D0206 & B/D105 (Božetěchova 1/2, Brno 61200)"),   # cancelled day (20th) out, event over (5 Nov) out
              ("20261126T120000Z", "20261126T155000Z", "Konzultace IMS (rezervace)", "B/C128"),
              ("20261127T070000Z", "20261127T085000Z", "ISA (P)", "B/E112 (Božetěchova 1/2, Brno 61200) [B/E112]")], ev
# what sync reads back: the same tuples, commas and all
back = parse_ics(fs.ics_text([("20261113T070000Z", "20261113T085000Z", "A, B; C", "B/D0206 (Božetěchova 1/2, Brno 61200) [B/D0206]")]))
assert back == {("2026-11-13T07:00:00Z", "2026-11-13T08:50:00Z", "A, B; C", "B/D0206 (Božetěchova 1/2, Brno 61200) [B/D0206]")}, back
tm = lambda i, s, e, code, year, kind="písemný test": {"termin_id": i, "zacatek_zkousky": s, "konec_zkousky": e, "zp_typ_hodnoceni_popis": kind,  # noqa: E731
                                                       "zkousejici_label_pr": "Svoboda Petr, doc. Ing., Ph.D.", "aktualni_predmety": [{"p_zkratka": code, "ap_rok": year}]}
terms = [tm(1, "2026-12-08T14:00:00", "2026-12-08T15:00:00", "ITU", 2026), tm(2, "2024-11-19T10:00:00", "2024-11-19T10:45:00", "IEL", 2024), tm(3, "2027-01-05T09:00:00", "2027-01-05T10:50:00", "IIS", 2026)]
ex = parse_ics(fs.ics_text(fs.exam_events(terms, {1: ["B/D105"], 3: ["B/D105", "B/E105"]}, 2026)))
assert ex == {("2026-12-08T13:00:00Z", "2026-12-08T14:00:00Z", "ITU(písemný test) - Svoboda Petr, doc. Ing., Ph.D. [/]", "B/D105 (Božetěchova 1/2, Brno 61200) [B/D105]"),
              ("2027-01-05T08:00:00Z", "2027-01-05T09:50:00Z", "IIS(písemný test) - Svoboda Petr, doc. Ing., Ph.D. [/]", "")}, ex  # old year out; two candidate rooms: none
idx = [{"zkratka": "IIS", "akrok": 2026, "bodove_hodnoceni": 12}, {"zkratka": "IIS", "akrok": 2025, "bodove_hodnoceni": 40}, {"zkratka": "ISA", "akrok": 2026, "bodove_hodnoceni": 11.25},
       {"zkratka": "IMS", "akrok": 2026, "bodove_hodnoceni": None}, {"zkratka": "IAL", "akrok": 2025, "bodove_hodnoceni": 61.0}]
assert fs.api_points(idx) == {"IIS": 12, "ISA": 11.25, "IMS": None, "IAL": 61} and isinstance(fs.api_points(idx)["IAL"], int)
wk = lambda ak, sem, z, a, b: {"akademicky_rok": ak, "semestr_zkratka": sem, "zkouskove": z, "real_datum_od": a + "T00:00:00", "real_datum_do": b + "T00:00:00"}  # noqa: E731
weeks = [wk(2025, "Z", 0, "2025-09-15", "2025-09-19"), wk(2026, "L", 0, "2027-02-08", "2027-02-12"), wk(2026, "L", 0, "2027-05-03", "2027-05-07"), wk(2026, "Z", 1, "2027-01-04", "2027-01-08"),
         wk(2026, "Z", 0, "2026-09-14", "2026-09-18"), wk(2026, "Z", 0, "2026-12-07", "2026-12-11"), wk(2026, "Z", 1, "2027-02-01", "2027-02-05"), wk(2026, "L", 1, "2027-05-10", "2027-06-11")]
assert fs.api_periods(weeks) == [{"name": "výuka ZS", "start": "2026-09-14", "end": "2026-12-11"}, {"name": "zkouškové ZS", "start": "2027-01-04", "end": "2027-02-05"},
                                 {"name": "výuka LS", "start": "2027-02-08", "end": "2027-05-07"}, {"name": "zkouškové LS", "start": "2027-05-10", "end": "2027-06-11"}]
# malformed data: a null/garbled time or an end before the start skips that event only; a datetime with an offset is converted
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=PRAGUE)
vy2 = [{"v_p_zkratka": "ISA", "v_trj_zkratka": "P", "bloky": [{"vb_cas_od": "1899-12-30T08:00:00", "vb_cas_do": "1899-12-30T09:50:00", "dny": [day(10, "2026-11-13")]},
                                                             {"vb_cas_od": None, "vb_cas_do": "1899-12-30T09:50:00", "dny": [day(11, "2026-11-14")]},
                                                             {"vb_cas_od": "1899-12-30T10:00:00", "vb_cas_do": "1899-12-30T09:00:00", "dny": [day(12, "2026-11-15")]},
                                                             {"vb_cas_od": "garbage", "vb_cas_do": "1899-12-30T09:50:00", "dny": [day(13, "2026-11-16")]}]}]
assert [e[0] for e in fs.timetable_events(vy2, {}, [{**resv[0], "cas_od": None}], NOW)] == ["20261113T070000Z"]
assert fs.utc_dt("2026-12-08T14:00:00Z") == "20261208T140000Z" and fs.utc_dt("2026-12-08T14:00:00+01:00") == "20261208T130000Z" and fs.utc_dt("2026-12-08T14:00:00") == "20261208T130000Z"
for call in (lambda: fs.utc_dt(None), lambda: fs.utc("2026-11-14T00:00:00", None), lambda: fs.utc(None, "1899-12-30T08:00:00"), lambda: fs.utc_dt("garbage")):
    try:
        call()
        raise SystemExit("expected TypeError/ValueError")
    except (TypeError, ValueError):
        pass
bad_terms = [tm(4, None, "2026-12-08T15:00:00", "ITU", 2026), tm(5, "2026-12-08T15:00:00", "2026-12-08T14:00:00", "ITU", 2026), tm(6, "2026-12-09T14:00:00", "2026-12-09T15:00:00", "ITU", 2026)]
assert [e[0] for e in fs.exam_events(bad_terms, {}, 2026)] == ["20261209T130000Z"]
# lessons that should have rooms but got none: the piece fails (falls back) instead of emitting events without a location
def no_rooms_get(path, **kw):
    if "osobni/vyucovani" in path:
        return {"data": {"vyucovani": [{**vy[0], "v_vyucovani_id": 1}]}}
    if "mistnosti/podrobne" in path:
        return {"data": {"vyucovani": [{"vyucovani_id": 1, "bloky": [{"dny": [{"vyuc_blok_den_id": 10, "mistnosti": []}]}]}]}}
    return {"data": {"rezervace": []}}
vutapi.get = no_rooms_get
try:
    fs.fetch_timetable(NOW)
    raise SystemExit("expected ApiError")
except vutapi.ApiError as e:
    assert "no rooms for ISA (P)" in str(e)
# iCal text: newlines and control characters cannot break the file or inject properties
txt = fs.ics_text([("20261113T070000Z", "20261113T085000Z", "A\nDTSTART:2000\r\nB\tC\x07", "Room\\1, x; y")])
assert sum(l.startswith("DTSTART") for l in txt.splitlines()) == 1 and "\x07" not in txt and "\r" not in txt and parse_ics(txt) == {("2026-11-13T07:00:00Z", "2026-11-13T08:50:00Z", "A DTSTART:2000 B C", "Room\\1, x; y")}, parse_ics(txt)
# atomic writes and the failure streak
wd = Path(tempfile.mkdtemp())
fs.write_atomic(wd / "a.txt", "one")
fs.write_atomic(wd / "a.txt", "two")
assert (wd / "a.txt").read_text() == "two" and [p.name for p in wd.iterdir()] == ["a.txt"]
assert stat.S_IMODE(os.stat(wd / "a.txt").st_mode) == 0o600  # grades and timetable: owner only
sf = wd / "streak.json"
assert [fs.api_streak(True, sf) for _ in range(3)] == [1, 2, 3] and fs.api_streak(False, sf) == 0 and fs.api_streak(True, sf) == 1
sf.write_text("garbage")
assert fs.api_streak(True, sf) == 1
# the token problem stops api_pieces at the first piece
seen = []
def dead_get(path, **kw):
    seen.append(path)
    raise vutapi.TokenError("token minting paused after a failure")
vutapi.get = dead_get
assert fs.api_pieces(NOW) == {} and len(seen) == 1
# a failing API piece is warned about and left to the scraper; the others still come through
def fake_get(path, **kw):
    if "osobni/terminy" in path:
        raise vutapi.ApiError("GET: HTTP 500")
    if "moje/tydny" in path:
        return {"data": {"tydny": weeks}}
    if "moje/studia/v1" in path:
        return {"data": {"studia": [{"studium_id": 7, "aktivni_studium": 1}]}}
    if path.endswith("index/v1"):
        return {"data": {"studia": [{"index": idx}]}}
    if "osobni/vyucovani" in path:
        return {"data": {"vyucovani": vy}}
    if "mistnosti/podrobne" in path:
        return resp
    return {"data": {"rezervace": []}}
vutapi.get = fake_get
got = fs.api_pieces(datetime(2026, 11, 12, 12, 0, tzinfo=PRAGUE))
assert sorted(got) == ["periods", "points", "timetable"] and "ISA (P)" in got["timetable"], sorted(got)
# fetch_studis.main() on a fresh checkout: no studis.json, no API credentials, no Moodle -> scraper only, files written, nothing else needed
for k in ("VUT_API_CLIENT_ID", "VUT_API_SECRET", "MOODLE_ICS_URL"):
    os.environ.pop(k, None)
fs.HERE = vutapi.HERE = Path(tempfile.mkdtemp())
assert not vutapi.configured()
scraped = {"timetable": "BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR\n", "exams": "BEGIN:VCALENDAR\nEND:VCALENDAR\n", "points": {"ABC": 5}, "periods": [{"name": "výuka ZS", "start": "2026-09-14", "end": "2026-12-11"}],
           "terms": [], "deadlines": [], "items": {"ABC": []}}
asked = []
fs.scrape = lambda need, prev: (asked.append(set(need)), scraped)[1]
fs.main()
assert asked == [{"timetable", "exams", "points", "periods"}] and json.loads((fs.HERE / "studis.json").read_text())["points"] == {"ABC": 5}
assert (fs.HERE / "studis-timetable.ics").exists() and (fs.HERE / "studis-exams.ics").exists() and not (fs.HERE / "api-failures.json").exists()  # no API configured: no failure counting
# an empty timetable (not published yet) is a valid empty calendar, not a failure; Moodle trouble and missing optional data never stop the rest
vutapi.get = lambda path, **kw: {"data": {}}
empty = fs.fetch_timetable(datetime(2026, 11, 12, 12, 0, tzinfo=PRAGUE))
assert empty.startswith("BEGIN:VCALENDAR") and "BEGIN:VEVENT" not in empty and parse_ics(empty) == set()
fs.HERE = vutapi.HERE = Path(tempfile.mkdtemp())
os.environ["MOODLE_ICS_URL"] = "http://moodle.invalid/x.ics"
def moodle_down(*a, **kw): raise urllib.error.URLError("down")
real_urlopen, fs.urllib.request.urlopen = fs.urllib.request.urlopen, moodle_down
fs.scrape = lambda need, prev: {"timetable": empty, "exams": "BEGIN:VCALENDAR\nEND:VCALENDAR\n", "terms": [], "deadlines": [], "items": {}}  # no points, no periods
try:
    fs.main()  # exits 0: empty periods/points only warn, the Moodle failure is skipped, the empty timetable is written
finally:
    fs.urllib.request.urlopen = real_urlopen
    os.environ.pop("MOODLE_ICS_URL")
assert (fs.HERE / "studis-timetable.ics").read_text() == empty and not (fs.HERE / "studis-moodle.ics").exists() and json.loads((fs.HERE / "studis.json").read_text())["terms"] == []
# no timetable from anywhere: the only case that exits 1, other pieces are still written
fs.HERE = Path(tempfile.mkdtemp())
fs.scrape = lambda need, prev: {"exams": "BEGIN:VCALENDAR\nEND:VCALENDAR\n", "points": {"ABC": 1}, "periods": [], "terms": [], "deadlines": [], "items": {}}
try:
    fs.main()
    raise SystemExit("expected exit 1")
except SystemExit as e:
    assert e.code == 1 and (fs.HERE / "studis-exams.ics").exists() and not (fs.HERE / "studis-timetable.ics").exists()
print("ok")
