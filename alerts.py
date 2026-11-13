# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Discord alerts, each a new DM so it notifies: exam registration opens/closes, a seat frees up, deadlines (24 h, 3 h,
none once marked done), new points, point thresholds reached, reminders set through the agent (notes with "at"). Rule-based from the local files, no model calls. Runs every 5 min as studis@alerts.

  uv run alerts.py              send what is due, write alerts-state.json
  uv run alerts.py --dry-run    print what would be sent, write nothing, no seat check
  uv run alerts.py --selftest   asserts on the rules, no network
Quiet hours (default 22-08 Prague, env QUIET_FROM / QUIET_TO): only registration-opens, seat alerts and reminders go out, the rest is held and sent as one message when they end.
"""
import copy, json, os, subprocess, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
import envutil
from sync import PRAGUE, extra_events
from digest import stamp, send, load_notes, points_text, plain

HERE = Path(__file__).parent
STATE = HERE / "alerts-state.json"
LINK = "<https://www.vut.cz/studis/student.phtml?sn=terminy_zk>"
H, M = timedelta(hours=1), timedelta(minutes=1)


def dt(s):
    """Studis / ics time string -> aware datetime; naive ones (studis.json, all-day dates) are Prague local."""
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=PRAGUE)


QUIET_FROM, QUIET_TO = envutil.num("QUIET_FROM", 22), envutil.num("QUIET_TO", 8)  # hours; quiet = [FROM, 24) + [0, TO)


def quiet(t):
    return not QUIET_TO <= t.astimezone(PRAGUE).hour < QUIET_FROM


def before_quiet(t):
    """5 minutes before the evening whose quiet stretch t falls in (21:55 by default)."""
    t = t.astimezone(PRAGUE)
    return (t if t.hour >= QUIET_FROM else t - timedelta(days=1)).replace(hour=QUIET_FROM - 1, minute=55, second=0, microsecond=0)


def tkey(t):  # one entry per term: a course/kind has several, told apart by their dates
    return (t["course"], t["kind"], tuple(t["dates"]))


# rules: item -> [(id, text, loud)] for every alert whose window contains now

def term(now, t, prev):
    c, k = t["course"], t["kind"]
    if t["registered"] or not t["opens"]:
        return []
    opens, closes, out = dt(t["opens"]), t["closes"] and dt(t["closes"]), []
    if opens - 10 * M <= now < opens + 5 * M:
        out.append((f"reg-open:{c}:{k}:{t['opens']}", f"🔔 **{c} ({k})**: registrace se otevírá {stamp(opens, 'R')} · {LINK}", True))
    if closes and closes - 24 * H <= now < closes:
        out.append((f"reg-close:{c}:{k}:{t['closes']}", f"⏰ **{c} ({k})**: registrace se zavírá {stamp(closes, 'R')} · {LINK}", False))
    if (p := prev.get(tkey(t))) and not p["free"] and t["free"]:
        dates = ", ".join(stamp(dt(d), "f") for d in t["dates"])
        out.append((f"seat:{c}:{k}:{now:%Y-%m-%dT%H:%M}:{(t['dates'] or [''])[0]}", f"🪑 **{c} ({k})**: uvolnilo se místo! {dates} · {LINK}", True))
    return out


def deadline(now, d):
    title, due_ = d[0], dt(d[1])
    out, start = [], due_ - 3 * H
    if due_ - 24 * H <= now < due_:
        out.append((f"dl24:{title}:{d[1]}", f"📌 {plain(title)} · {stamp(due_, 'R')}", False))
    if moved := quiet(start):  # moved to 21:55 and loud, so a missed 21:55 tick still goes out
        start = before_quiet(start)
    if start <= now < due_:
        out.append((f"dl3:{title}:{d[1]}", f"❗ {plain(title)} · {stamp(due_, 'R')}", moved))
    return out


def reminder(now, n):
    at = dt(n["at"])
    return [(f"rem:{n['id']}:{n['at']}", f"⏰ {n['text']}", True)] if at <= now < at + 24 * H else []  # a day late at most (bot down)


def due(now, data, state):
    """-> (messages [(ids, text)], new state). The caller marks ids once the DM went out.
    No state (first run, corrupt, {}): seed - everything already due is marked sent without sending, so nothing is replayed."""
    seed = not state
    st = {"sent": {}, "held": [], "seat_check": {"last": None, "fails": 0}, "points": {}, **copy.deepcopy(state or {})}
    st["sent"] = sent = {i: t for i, t in st["sent"].items() if datetime.fromisoformat(t) >= now - timedelta(days=30)}
    info, pts, thr = data.get("studis") or {}, st["points"], data.get("thresholds") or {}
    prev = {tkey(t): t for t in data.get("prev_terms") or []}

    def point(cv):
        c, v = cv
        if v is None or v == pts.get(c):
            return []
        t, out = thr.get(c), []
        if t and (pts.get(c) or 0) < t["points"] <= v:  # crossed since the last points alert; before the early return so a failed ✅ retries
            out.append((f"thr:{c}:{t['points']}", f"✅ {c}: {t['label']} splněn ({v}/{t['points']})", False))
        if (i := f"pts:{c}:{v}") in sent:  # went out last tick
            pts[c] = v
            return out
        return [(i, f"📈 **{c}**: {'–' if pts.get(c) is None else pts[c]} → {points_text(v, t)}", False), *out]

    found = []
    for rule, items in ((lambda t: term(now, t, prev), lambda: info.get("terms") or []), (lambda d: deadline(now, d), lambda: (data.get("deadlines") or list)()),
                        (point, lambda: list((info.get("points") or {}).items())),
                        (lambda n: reminder(now, n), lambda: data.get("reminders") or [])):
        try:
            items = items()
        except Exception as e:  # malformed file (e.g. studis.json): skip that source only
            print(f"alert source failed: {e!r}")
            items = []
        for it in items:
            try:
                found += rule(it)
            except Exception as e:  # one bad entry must not stop the rest
                print(f"alert rule failed on {str(it)[:80]}: {e!r}")
    new = [a for a in found if a[0] not in sent]
    regs = [a for a in new if a[0].startswith(("reg-open:", "reg-close:"))]
    batch = regs if len(regs) > 2 and (not quiet(now) or any(a[2] for a in regs)) else []  # many windows at once (first sight of new terms): one DM, not a burst
    new = [a for a in new if a not in batch]
    if seed:
        st["points"] = {c: v for c, v in (info.get("points") or {}).items() if v is not None}
        sent.update(dict.fromkeys((i for i, _, _ in new), now.isoformat()))
        return [], st
    out = []
    for i, text, loud in new:
        if not loud and quiet(now):
            st["held"].append([i, text])
            sent[i] = now.isoformat()
        else:
            out.append(([i], text))
    if batch:
        out.append(([i for i, _, _ in batch], "\n".join(t for _, t, _ in batch)))
    if st["held"] and not quiet(now):
        out.append(([i for i, _ in st["held"]], "\n".join(t for _, t in st["held"])))
    return out, st


def mark(st, ids, now):
    st["sent"].update(dict.fromkeys(ids, now.isoformat()))
    st["held"] = [h for h in st["held"] if h[0] not in ids]


def seat_gate(now, terms, state):
    """Re-fetch Studis only while an exam you have no term in (same course and kind) has an open window and every open term of it is full, at most every 15 min."""
    last = (state.get("seat_check") or {}).get("last")
    if last and now - datetime.fromisoformat(last) < 15 * M:
        return False
    mine = {(t["course"], t["kind"]) for t in terms if t["registered"]}
    win = [t for t in terms if t.get("opens") and t.get("closes") and not t["registered"] and dt(t["opens"]) <= now < dt(t["closes"]) and (t["course"], t["kind"]) not in mine]
    return any(not any(u["free"] for u in win if (u["course"], u["kind"]) == (t["course"], t["kind"])) for t in win)


def seat_done(state, now, err):
    sc = state.setdefault("seat_check", {"last": None, "fails": 0})
    sc["last"], sc["fails"] = now.isoformat(), sc.get("fails", 0) + 1 if err else 0
    return f"⚠️ kontrola míst selhává: {err}" if sc["fails"] == 3 else None


def load(name, default=None):
    try:
        return json.loads((HERE / name).read_text())
    except (OSError, ValueError):  # missing or half-written: treated as empty
        return default


def save(st):
    with tempfile.NamedTemporaryFile("w", dir=HERE, suffix=".tmp", delete=False) as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(f.name, STATE)


def seat_check(now, data, state):
    """Gate passed -> run fetch_studis.py (run.sh already holds the run lock); on success the pre-fetch terms go to
    state["seat_prev"] (kept until the seat alerts they produce went out) and data gets the fresh studis.json."""
    if not seat_gate(now, data["studis"].get("terms") or [], state):
        return None
    try:
        r = subprocess.run(["uv", "run", "--locked", "fetch_studis.py"], cwd=HERE, capture_output=True, text=True, timeout=300)
        lines = r.stderr.strip().splitlines()
        err = None if r.returncode == 0 else next((ln for ln in lines if ln.startswith("FAILED")), lines[-1] if lines else f"exit {r.returncode}")[:200]
    except subprocess.TimeoutExpired:
        err = "fetch_studis.py běžel přes 5 min"
    if err:
        print("seat check failed:", err)
    else:  # an older unsent seat_prev wins: it is still the last state you were told about
        state.setdefault("seat_prev", data["studis"].get("terms") or [])
        data["studis"] = load("studis.json", {})
    return seat_done(state, now, err)


def tick(now, data, state, send, extra=()):
    """due() + send each message; marks what went out. -> (new state, message count)."""
    msgs, state = due(now, {**data, "prev_terms": (state or {}).get("seat_prev")}, state)
    for ids, text in [*extra, *msgs]:
        try:
            send(text)
        except Exception as e:  # not marked: retried next tick
            print(f"send failed: {e!r}")
            continue
        mark(state, ids, now)
    if all(i in state["sent"] for ids, _ in msgs for i in ids if i.startswith("seat:")):
        state.pop("seat_prev", None)
    return state, len(extra) + len(msgs)


def main():
    dry, now = "--dry-run" in sys.argv, datetime.now(PRAGUE)
    if not dry and not (os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_USER_ID")):
        sys.exit("DISCORD_BOT_TOKEN/DISCORD_USER_ID not set; refusing to mark alerts sent (use --dry-run)")
    state = load(STATE.name)
    # deadlines read lazily inside due()'s per-source guard: a broken studis-moodle.ics drops only that source
    notes = load_notes()
    data = {"studis": load("studis.json", {}), "thresholds": notes["thresholds"], "reminders": [n for n in notes["notes"] if n.get("at")],
            "deadlines": lambda: [(k[2], k[1]) for k in extra_events(set()) if " – odevzdání: " in k[2] and k[2] not in notes["done"]]}
    extra = []
    if state and not dry and (msg := seat_check(now, data, state)):
        extra.append(([], msg))
    # send() without a state argument: always a new DM, so it notifies
    state, n = tick(now, data, state, (lambda text: print(text + "\n")) if dry else send, extra)
    if not dry:
        save(state)
    print(f"alerts: {n} messages")


def selftest():
    from unittest import mock
    T = lambda s: datetime.fromisoformat(s).replace(tzinfo=PRAGUE)  # noqa: E731
    tm = {"course": "IIS", "kind": "zkouška", "registered": False, "opens": "2026-11-10T23:00", "closes": "2026-11-12T23:59",
          "free": False, "dates": ["2026-12-01T09:00"]}
    D = lambda **kw: {"studis": {"terms": [tm], "points": {"IMP": 4, "IIS": None}}, "deadlines": list, **kw}  # noqa: E731
    OPEN, CLOSE = "reg-open:IIS:zkouška:2026-11-10T23:00", "reg-close:IIS:zkouška:2026-11-12T23:59"

    def run(now, d, st):
        out, st = due(T(now), d, st)
        return [i for ids, _ in out for i in ids], out, st

    # seeding: sends nothing, marks what is already due, stores points; {} seeds too
    base = due(T("2026-11-01T12:00"), D(), None)[1]
    assert base["points"] == {"IMP": 4} and not base["held"] and "pts:IMP:4" in base["sent"], base
    assert due(T("2026-11-01T12:00"), D(), {})[0] == []
    s2 = due(T("2026-11-10T22:52"), D(), None)[1]
    assert OPEN in s2["sent"] and run("2026-11-10T22:55", D(), s2)[0] == []  # open window at seed time: not replayed
    # reg-open: [opens-10m, opens+5m), breaks through quiet hours, sent once
    assert run("2026-11-10T22:49", D(), base)[0] == []
    ids, out, st = run("2026-11-10T22:50", D(), base)
    assert ids == [OPEN] and "<t:" in out[0][1] and LINK in out[0][1], out
    assert run("2026-11-10T22:55", D(), base)[0] == [OPEN]  # send failed (not marked): due again
    mark(st, ids, T("2026-11-10T22:50"))
    assert run("2026-11-10T22:55", D(), st)[0] == []
    assert run("2026-11-10T23:05", D(), base)[0] == []
    assert run("2026-11-10T22:50", D(studis={"terms": [{**tm, "registered": True}], "points": {}}), base)[0] == []
    # registered terms with no window (today's real data) and garbage entries
    reg = {"course": "IMP", "kind": "x", "registered": True, "opens": None, "closes": None, "free": False, "dates": ["2026-11-05T13:30"]}
    assert run("2026-11-01T12:00", D(studis={"terms": [reg], "points": {}}), base)[0] == []
    assert run("2026-11-10T22:50", D(studis={"terms": [{"course": "X"}, tm], "points": {}}), base)[0] == [OPEN]  # bad entry logged, rest goes out
    assert due(T("2026-11-01T12:00"), {}, base)[0] == []  # no files at all
    # reg-close: 24 h before closes; starts in quiet hours -> held, flushed as one message at 8:00
    assert run("2026-11-11T23:58", D(), base)[0] == []
    ids, out, st = run("2026-11-11T23:59", D(), base)
    assert ids == [] and st["held"][0][0] == CLOSE and CLOSE in st["sent"], st
    assert run("2026-11-12T07:55", D(), st)[0] == []
    ids, out, st = run("2026-11-12T08:00", D(), st)
    assert ids == [CLOSE] and len(out) == 1 and "zavírá" in out[0][1], out
    mark(st, ids, T("2026-11-12T08:00"))
    assert not st["held"] and run("2026-11-12T08:05", D(), st)[0] == []
    # seat: only right after a seat check (prev_terms), full -> free, loud
    full, freed = {**tm, "opens": "2026-11-01T08:00"}, {**tm, "opens": "2026-11-01T08:00", "free": True}
    assert run("2026-11-02T23:30", D(studis={"terms": [freed], "points": {}}, prev_terms=[full]), base)[0] == ["seat:IIS:zkouška:2026-11-02T23:30:2026-12-01T09:00"]
    assert run("2026-11-02T23:30", D(studis={"terms": [freed], "points": {}}), base)[0] == []
    assert run("2026-11-02T23:30", D(studis={"terms": [freed], "points": {}}, prev_terms=[freed]), base)[0] == []
    # deadlines: dl24 / dl3; due 23:59 Prague
    dl = lambda due_: D(studis={"terms": [], "points": {"IMP": 4}}, deadlines=lambda: [("ITU – odevzdání: Fáze I", due_)])  # noqa: E731
    d = dl("2026-11-06T22:59:00Z")
    assert run("2026-11-05T23:58", d, base)[0] == []
    ids, _, st = run("2026-11-05T23:59", d, base)
    assert ids == [] and st["held"][0][0] == "dl24:ITU – odevzdání: Fáze I:2026-11-06T22:59:00Z"  # dl24 in quiet hours: held
    assert run("2026-11-06T20:58", d, base)[0] == ["dl24:ITU – odevzdání: Fáze I:2026-11-06T22:59:00Z"]
    assert "dl3:ITU – odevzdání: Fáze I:2026-11-06T22:59:00Z" in run("2026-11-06T20:59", d, base)[0]
    # dl3 whose 3 h mark is in quiet hours (due 01:00) fires at 21:55, and still goes out if that tick was missed
    d = dl("2026-11-07T00:00:00Z")
    DL3 = "dl3:ITU – odevzdání: Fáze I:2026-11-07T00:00:00Z"
    assert DL3 not in run("2026-11-06T21:54", d, base)[0] and DL3 in run("2026-11-06T21:55", d, base)[0]
    assert DL3 in run("2026-11-06T23:00", d, base)[0]
    # points: new value alerts once, stored after it went out
    p = D(studis={"terms": [], "points": {"IMP": 5, "IIS": None}})
    ids, out, st = run("2026-11-01T12:05", p, base)
    assert ids == ["pts:IMP:5"] and "4 → 5" in out[0][1], out
    mark(st, ids, T("2026-11-01T12:05"))
    ids, _, st = run("2026-11-01T12:10", p, st)
    assert ids == [] and st["points"]["IMP"] == 5
    # thresholds: crossing fires once next to the points alert (shown as v/limit (label)); a failed ✅ is retried; seeding sends nothing
    th = {"IMP": {"points": 5, "label": "zápočet"}}
    P = lambda v: D(studis={"terms": [], "points": {"IMP": v}}, thresholds=th)  # noqa: E731
    ids, out, st = run("2026-11-01T12:05", P(5), base)
    assert ids == ["pts:IMP:5", "thr:IMP:5"] and "4 → 5/5 (zápočet)" in out[0][1] and out[1][1] == "✅ IMP: zápočet splněn (5/5)", out
    half = copy.deepcopy(base)
    half["sent"]["pts:IMP:5"] = "2026-11-01T12:05:00+01:00"
    assert run("2026-11-01T12:10", P(5), half)[0] == ["thr:IMP:5"]  # points DM went out, ✅ didn't
    assert run("2026-11-01T12:05", P(4.5), base)[0] == ["pts:IMP:4.5"]  # still below
    msgs, seeded = due(T("2026-11-01T12:00"), P(6), None)
    assert msgs == [] and "thr:IMP:5" in seeded["sent"] and run("2026-11-01T12:05", P(6), seeded)[0] == []
    # reminders: loud (quiet hours too), once, at most a day late; seeding marks a due one sent
    R = lambda at: D(studis={"terms": [], "points": {"IMP": 4}}, reminders=[{"id": 7, "text": "zaplatit kolej", "at": at}])  # noqa: E731
    RID = "rem:7:2026-11-05T23:30:00+01:00"
    assert run("2026-11-05T23:25", R("2026-11-05T23:30:00+01:00"), base)[0] == []
    ids, out, st = run("2026-11-05T23:30", R("2026-11-05T23:30:00+01:00"), base)
    assert ids == [RID] and out[0][1] == "⏰ zaplatit kolej", out
    mark(st, ids, T("2026-11-05T23:30"))
    assert run("2026-11-05T23:35", R("2026-11-05T23:30:00+01:00"), st)[0] == []
    assert run("2026-11-06T23:31", R("2026-11-05T23:30:00+01:00"), base)[0] == []  # over a day late: dropped
    assert RID in due(T("2026-11-05T23:40"), R("2026-11-05T23:30:00+01:00"), None)[1]["sent"]
    assert run("2026-11-05T23:30", R(None), base)[0] == []  # malformed reminder: logged, skipped
    # sent ids older than 30 days are pruned
    assert "x" not in due(T("2026-11-01T12:00"), D(), {**base, "sent": {"x": "2026-09-01T00:00:00+02:00"}})[1]["sent"]
    # opens set, closes null: no raise, reg-open still fires
    assert run("2026-11-10T22:50", D(studis={"terms": [{**tm, "closes": None}], "points": {}}), base)[0] == [OPEN]
    def broken(*a):
        raise ValueError("bad studis-moodle.ics")
    assert run("2026-11-10T22:50", D(deadlines=broken), base)[0] == [OPEN]  # broken deadlines source: only it is dropped
    # a real run without Discord credentials refuses before touching state (send() would only print, marking alerts sent)
    with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "", "DISCORD_USER_ID": ""}), mock.patch.object(sys, "argv", ["alerts.py"]), \
            mock.patch.dict(globals(), load=broken):  # guard: a real run must never get as far as loading state
        try:
            main()
            raise AssertionError("main() ran without credentials")
        except SystemExit as e:
            assert e.code and "DISCORD_BOT_TOKEN" in str(e.code), e
    # seat check gate: a window open, not registered, all full, 15 min since the last check
    w, now = {**tm, "opens": "2026-11-01T08:00"}, T("2026-11-02T10:00")
    assert seat_gate(now, [w], {}) and not seat_gate(now, [{**w, "free": True}], {}) and not seat_gate(now, [{**w, "registered": True}], {})
    assert not seat_gate(T("2026-11-13T00:00"), [w], {}) and not seat_gate(now, [reg], {})
    assert not seat_gate(now, [w], {"seat_check": {"last": (now - 10 * M).isoformat(), "fails": 0}})
    assert seat_gate(now, [w], {"seat_check": {"last": (now - 15 * M).isoformat(), "fails": 0}})
    # failure counter: one DM at the 3rd consecutive failure, reset on success
    s = {}
    assert [seat_done(s, now, "boom") for _ in range(4)] == [None, None, "⚠️ kontrola míst selhává: boom", None] and s["seat_check"]["fails"] == 4
    assert seat_done(s, now, None) is None and s["seat_check"] == {"last": now.isoformat(), "fails": 0}
    # seat check frees a seat, the DM fails; the next tick (no new fetch) still sends it, then seat_prev is dropped
    st, data, ok = copy.deepcopy(base), {"studis": {"terms": [w], "points": {}}}, subprocess.CompletedProcess([], 0, "", "")
    with mock.patch.object(subprocess, "run", return_value=ok) as sp, mock.patch.dict(globals(), load=lambda n, d=None: {"terms": [{**w, "free": True}]}):
        assert seat_check(now, data, st) is None and st["seat_prev"] == [w] and st["seat_check"]["fails"] == 0
        assert sp.call_args[0][0] == ["uv", "run", "--locked", "fetch_studis.py"]  # pinned like the rest of the run

    def boom(text):
        raise OSError("discord down")
    st, n = tick(now, data, st, boom)
    assert n == 1 and st["seat_prev"] == [w] and not any(i.startswith("seat:") for i in st["sent"])
    got = []
    st, n = tick(now + 5 * M, data, st, got.append)
    assert n == 1 and "uvolnilo" in got[0] and "seat_prev" not in st and any(i.startswith("seat:") for i in st["sent"])
    assert tick(now + 10 * M, data, st, got.append)[1] == 0
    # real shapes: a course/kind with several terms, each with its own window; same-kind terms tell apart by dates in the seat comparison
    t2 = {**tm, "dates": ["2026-12-08T09:00"], "opens": "2026-12-01T09:00", "closes": "2026-12-08T09:00", "free": False}
    t1 = {**tm, "dates": ["2026-12-01T09:00"], "opens": "2026-11-24T09:00", "closes": "2026-12-01T09:00", "free": True}
    two = lambda **kw: D(studis={"terms": [t1, t2], "points": {}}, **kw)  # noqa: E731
    assert run("2026-11-24T08:55", two(), base)[0] == ["reg-open:IIS:zkouška:2026-11-24T09:00"]  # each term announces its own opening, once
    assert run("2026-11-30T12:00", two(), base)[0] == ["reg-close:IIS:zkouška:2026-12-01T09:00"]
    assert run("2026-12-05T12:00", two(), base)[0] == []
    freed2 = {**t2, "free": True}
    assert run("2026-12-02T10:00", D(studis={"terms": [t1, freed2], "points": {}}, prev_terms=[t1, t2]), base)[0] == ["seat:IIS:zkouška:2026-12-02T10:00:2026-12-08T09:00"]
    assert run("2026-12-02T10:00", D(studis={"terms": [t1, t2], "points": {}}, prev_terms=[t1, {**t2, "free": True}]), base)[0] == []  # the other term's state is not mixed in
    # first deploy with many windows already open: no replayed "opens", and closes go out as one DM
    many = [{**tm, "course": c, "dates": ["2026-12-02T09:00"], "opens": "2026-11-25T09:00", "closes": "2026-12-02T09:00", "free": True} for c in "ABCD"]
    ids, out, _ = run("2026-12-01T12:00", D(studis={"terms": many, "points": {}}), base)
    assert len(ids) == 4 and len(out) == 1 and all(i.startswith("reg-close:") for i in ids), out
    assert run("2026-11-30T08:30", D(studis={"terms": many, "points": {}}), base)[0] == []
    # seat gate: only exams without a term of yours, and only while every open term is full
    G = lambda **kw: {**tm, "opens": "2026-11-01T08:00", "closes": "2026-11-20T08:00", **kw}  # noqa: E731
    gate = lambda terms: seat_gate(T("2026-11-10T12:00"), terms, {})  # noqa: E731
    assert gate([G()]) and not gate([G(free=True)]) and not gate([G(), G(registered=True, opens=None, closes=None)])  # registered in that kind: no polling
    assert gate([G(), G(registered=True, kind="jiný")]) and not gate([G(), G(free=True, dates=["2026-12-02T09:00"])])  # another kind registered / another term free
    assert not gate([G(opens="2026-11-11T08:00")]) and not gate([G(opens="2027-01-12T14:00", closes="2027-01-19T14:00")] * 16)  # not open yet: many future windows never poll
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    elif sys.argv[1:] in ([], ["--dry-run"]):
        main()
    else:
        sys.exit(print(__doc__, file=sys.stderr) or 2)
