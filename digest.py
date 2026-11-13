# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth-oauthlib"]
# ///
"""Discord DM from the bot. One digest per day (split over several messages past 2000 chars), edited in place on every later run.

  uv run digest.py            edit today's message (overview + points + exam registrations); --post also creates it
  uv run digest.py --weekly   next 7 days, sent as a new message
  uv run digest.py --evening  tomorrow's preview (20:00), sent as a new message; nothing when tomorrow is empty
  uv run digest.py --selftest no network
  uv run digest.py --fail URL short failure notice, sent as a new message
  --dry-run prints instead of sending. Needs DISCORD_BOT_TOKEN and DISCORD_USER_ID.
"""
import json, os, re, sys, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
import envutil  # noqa: F401  loads .env
from sync import service, list_events, EXAM, PRAGUE, CAL_NAME as SCHOOL

HERE = Path(__file__).parent
SEEN, STATE, NOTES = HERE / "points-seen.json", HERE / "digest-state.json", HERE / "zhelper-notes.json"
DAYS = "pondělí úterý středa čtvrtek pátek sobota neděle".split()
ICON = {SCHOOL: "🎓"}  # other calendars get a house
EXTRA_CALS = [c.strip() for c in os.environ.get("DIGEST_CALENDARS", "").split(",") if c.strip()]  # extra calendars by name; "primary" = main one
API = "https://discord.com/api/v10"
DRY = False  # set by main(); keeps --dry-run free of side effects (points-seen.json)


def stamp(dt, fmt="t"):
    """Discord timestamp (rendered in the reader's timezone): t time, d date, f date+time, R relative."""
    return f"<t:{int(dt.timestamp())}:{fmt}>"


def studis():
    f = HERE / "studis.json"
    return json.loads(f.read_text()) if f.exists() else {}


def load_notes():
    """zhelper-notes.json (agent notes + point thresholds); missing, corrupt or malformed parts count as empty."""
    try:
        d = json.loads(NOTES.read_text())
    except (OSError, ValueError):
        d = {}
    ns, th = (d.get(k) if isinstance(d, dict) else None for k in ("notes", "thresholds"))
    dn = d.get("done") if isinstance(d, dict) else None
    return {"notes": [n for n in ns if isinstance(n, dict) and isinstance(n.get("id"), int) and isinstance(n.get("text"), str)
                      and isinstance(n.get("at", ""), str)] if isinstance(ns, list) else [],  # "at" (ISO, aware) = a reminder, sent by alerts.py
            "done": {t: v for t, v in dn.items() if isinstance(v, str)} if isinstance(dn, dict) else {},  # deadline summary -> its due, marked done
            "thresholds": {c: t for c, t in th.items() if isinstance(t, dict) and isinstance(t.get("points"), (int, float)) and isinstance(t.get("label"), str)}
            if isinstance(th, dict) else {},
            **({"next": d["next"]} if isinstance(d, dict) and isinstance(d.get("next"), int) else {})}  # agent.remember's id counter


def plain(s, cap=200):
    """Calendar/Studis/Moodle text for a DM: one line, capped, Discord markdown defused, so a title can't pose as a
    masked link [text](url), a mention, a timestamp or a new section. Ordinary titles come out unchanged."""
    return re.sub(r"([\\*_~`|\[\]<>])", r"\\\1", " ".join(str(s).split())[:cap])


def points_text(v, t=None):
    """Points as /body and alerts show them: 7 b., or 7/20 (zápočet) when the course has a threshold."""
    return "–" if v is None else f"{v}/{t['points']} ({t['label']})" if t else f"{v} b."


def discord(method, path, body):
    req = urllib.request.Request(API + path, data=body and json.dumps(body).encode(), method=method, headers={
        "Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}", "Content-Type": "application/json",
        "User-Agent": "DiscordBot (vut-kalendar, 1.0)"})
    r = urllib.request.urlopen(req, timeout=30).read()
    return json.loads(r) if r else None


def chunks(msg, cap=2000):
    """Split msg into pieces under Discord's 2000-char cap, at section breaks where possible, else at line breaks."""
    out = [""]
    for part in msg.split("\n\n"):
        if out[-1] and len(out[-1]) + 2 + len(part) > cap and len(part) <= cap:
            out.append("")
        sep = "\n\n"
        for ln in part.split("\n"):
            if out[-1] and len(out[-1]) + len(sep) + len(ln) > cap:
                out.append("")
            out[-1] = f"{out[-1]}{sep}{ln}" if out[-1] else ln
            sep = "\n"
    return out


def send(msg, state=None):
    """Post msg as DMs (as many as it takes); with state, edit the messages recorded there instead and drop leftovers."""
    if not os.environ.get("DISCORD_BOT_TOKEN") or not os.environ.get("DISCORD_USER_ID"):
        return print("DISCORD_BOT_TOKEN/DISCORD_USER_ID not set, would send:\n" + msg)
    ch = discord("POST", "/users/@me/channels", {"recipient_id": os.environ["DISCORD_USER_ID"]})["id"]
    old = (state or {}).get("messages") or ([state["message"]] if state and state.get("message") else [])
    ids = []
    for i, c in enumerate(chunks(msg)):
        if i < len(old):
            try:
                discord("PATCH", f"/channels/{ch}/messages/{old[i]}", {"content": c})
                ids.append(old[i])
                continue
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        ids.append(discord("POST", f"/channels/{ch}/messages", {"content": c})["id"])
    for m in old[len(ids):]:
        try:
            discord("DELETE", f"/channels/{ch}/messages/{m}", None)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if state is not None:
        state.pop("message", None)
        state["messages"] = ids


# ---------- pieces ----------

def semester_line(today):
    periods = studis().get("periods", [])
    for p in periods:
        start, end = (datetime.fromisoformat(p[k]).date() for k in ("start", "end"))
        if start <= today <= end and p["name"].startswith("výuka"):
            week, pct = (today - start).days // 7 + 1, (today - start).days / (end - start).days
            return f"{week}. týden · {'▓' * round(pct * 12)}{'░' * (12 - round(pct * 12))} {pct:.0%} semestru"
        if start <= today <= end:
            return f"zkouškové · {(end - today).days} d do konce"
    nxt = min((datetime.fromisoformat(p["start"]).date() for p in periods if datetime.fromisoformat(p["start"]).date() > today), default=None)
    return f"zkouškové začíná za {(nxt - today).days} d" if nxt else ""


def hm(ev):
    s, e = ev["start"], ev["end"]
    if "dateTime" not in s:
        return "celý den"
    st, en = (datetime.fromisoformat(x["dateTime"]).astimezone(PRAGUE) for x in (s, e))
    return "celý den" if st.hour == st.minute == 0 and en.hour == en.minute == 0 else st.strftime("%H:%M")


def line(ev):
    room = ev.get("location", "").split(" (")[0].split(",")[0]
    t = hm(ev)
    return f"{t if t == 'celý den' else stamp(datetime.fromisoformat(ev['start']['dateTime']))}  {plain(ev.get('summary', '?'))}" + (f"  ·  {plain(room)}" if room and not room.startswith("http") else "")


def calendars(svc):
    """School calendar first, then the DIGEST_CALENDARS that exist."""
    items = [c for c in svc.calendarList().list().execute()["items"] if c.get("accessRole") == "owner"]
    want = [SCHOOL] + EXTRA_CALS
    pick = lambda w: next((c for c in items if c["summary"] == w or (w == "primary" and c.get("primary"))), None)
    return [(c["id"], c["summary"]) for w in want if (c := pick(w))]


def day_block(svc, cals, day0, head):
    out = []
    for cid, name in cals:
        evs = list_events(svc, cid, timeMin=day0.isoformat(), timeMax=(day0 + timedelta(days=1)).isoformat())
        if evs:
            out.append(f"**{ICON.get(name, '🏠')} {'Osobní' if '@' in name else name}**\n" + "\n".join(line(e) for e in sorted(evs, key=hm)))
    return head + ("\n" + "\n".join(out) if out else "\nnic v kalendáři")


def due_events(svc, cals, now, days=200):
    """Exams and deadlines in Škola from now for `days` days (Učení/Studium blocks left out)."""
    school = next((cid for cid, n in cals if n == SCHOOL), None)
    if not school:
        return []
    done = load_notes()["done"]
    return [e for e in list_events(svc, school, timeMin=now.isoformat(), timeMax=(now + timedelta(days=days)).isoformat())
            if EXAM.search(e.get("summary", "")) and not e["summary"].startswith(("Učení", "Studium:")) and "dateTime" in e["start"] and e["summary"] not in done]


def due_row(e, now):
    st = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(PRAGUE)
    d = (st.date() - now.date()).days
    dot = "" if d > 7 else "🟢" if d > 5 else "🟡" if d > 3 else "🟠" if d > 1 else "🔴"
    return f"{stamp(st, 'R')} · {stamp(st, 'f')} {dot or ' '} {plain(e['summary'])}"


def upcoming(svc, cals, now):
    up = due_events(svc, cals, now)
    return "**⏳ Blíží se**\n" + "\n".join(due_row(e, now) for e in up) if up else ""


def registrations(now):
    """Exam terms you are not registered for: opening today, open now, or all full."""
    out = []
    for t in studis().get("terms", []):
        if t["registered"] or not t["opens"]:
            continue
        opens, closes = (datetime.fromisoformat(t[k]).replace(tzinfo=PRAGUE) for k in ("opens", "closes"))
        name = f"{t['course']} ({t['kind']})"
        if opens.date() == now.date() and opens > now:
            out.append(f"🔔 dnes v {opens:%H:%M} se otevírá registrace: {name}")
        elif opens <= now <= closes:
            out.append(f"{'❗ ZAREGISTRUJ SE' if t['free'] else '⛔ všechny termíny plné'}: {name}, do {closes.day}.{closes.month}. {closes:%H:%M}")
        elif now < opens < now + timedelta(days=7):  # only what opens soon: later terms would flood the digest
            out.append(f"🗓️ registrace od {opens.day}.{opens.month}. {opens:%H:%M}: {name}")
    return "**📝 Registrace termínů**\n" + "\n".join(out) if out else ""


def points_today(state):
    new = studis().get("points", {})
    old = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    if old:
        for c, p in new.items():
            row = f"{c}: {old.get(c)} → {p} b."
            if p is not None and old.get(c) != p and row not in state["points"]:
                state["points"].append(row)
    if new and not DRY:
        SEEN.write_text(json.dumps(new, indent=1))
    return "**📈 Nové body**\n" + "\n".join(state["points"]) if state["points"] else ""


# ---------- messages ----------

def daily(svc, state):
    now = datetime.now(PRAGUE)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cals = calendars(svc)
    head = f"# 📅 {DAYS[now.weekday()].capitalize()} {now.day}. {now.month}." + (f"\n{s}" if (s := semester_line(now.date())) else "")
    parts = [day_block(svc, cals, day0, head), registrations(now), upcoming(svc, cals, now), points_today(state)]
    return "\n\n".join(p for p in parts if p)


def weekly(svc):
    now = datetime.now(PRAGUE)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    cals = calendars(svc)
    parts = ["# 🗓️ Příští týden"]
    if len(busy := due_events(svc, cals, now, days=10)) >= 3:  # deadlines bunch up: say so before the day-by-day list
        parts.append(f"**⚠️ Náročné období: {len(busy)} termínů za 10 dní**, začni s prvními:\n" + "\n".join(due_row(e, now) for e in busy))
    for i in range(7):
        d = day0 + timedelta(days=i)
        parts.append(day_block(svc, cals, d, f"**{DAYS[d.weekday()].capitalize()} {d.day}. {d.month}.**"))
    return "\n\n".join(parts)


def evening(svc, now=None):
    """Tomorrow's preview for the 20:00 DM: the day (deadlines included, they are Škola events). "" when tomorrow is empty."""
    now = now or datetime.now(PRAGUE)
    d = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    cals = calendars(svc)
    block = day_block(svc, cals, d, f"**🌙 Zítra** {DAYS[d.weekday()]} {d.day}. {d.month}.")
    return "" if block.endswith("\nnic v kalendáři") else block


def main():
    global DRY
    dry = DRY = "--dry-run" in sys.argv
    if "--fail" in sys.argv:
        msg = f"❌ studis-gcal selhal: {sys.argv[sys.argv.index('--fail') + 1]}"
        return print(msg) if dry else send(msg)
    if "--weekly" in sys.argv:
        msg = weekly(service())
        return print(msg) if dry else send(msg)
    if "--evening" in sys.argv:  # a ping, no state; a send error exits non-zero -> studis-run.sh's failure DM
        msg = evening(service())
        if not msg:
            return print("tomorrow is empty, nothing sent")
        return print(msg) if dry else send(msg)
    today = datetime.now(PRAGUE).strftime("%Y-%m-%d")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if state.get("date") != today:
        if "--post" not in sys.argv:  # hourly runs only edit; the day's message is posted by the morning run
            return print("no message for today yet, nothing to edit")
        state = {"date": today, "messages": [], "points": []}
    msg = daily(service(), state)
    if dry:
        print(msg)
    else:
        send(msg, state)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def selftest():
    g = globals()
    T = lambda s: datetime.fromisoformat(s).replace(tzinfo=PRAGUE)  # noqa: E731
    ev = lambda s, e, summ, **kw: {"summary": summ, "start": {"dateTime": T(s).isoformat()}, "end": {"dateTime": T(e).isoformat()}, **kw}  # noqa: E731
    data = {"s": [ev("2026-11-03T09:00", "2026-11-03T11:00", "IMP (L)", location="N103"), ev("2026-11-03T23:29", "2026-11-03T23:59", "ITU – odevzdání: Fáze I"),
                  ev("2026-11-05T09:00", "2026-11-05T11:00", "IIS (zkouška)")],
            "primary": []}
    g["list_events"] = lambda svc, cid, timeMin, timeMax, **kw: [e for e in data.get(cid, []) if timeMin <= e["start"]["dateTime"] < timeMax]
    cl = {"items": [{"id": "x", "summary": "Other", "accessRole": "owner"}, {"id": "p", "summary": "me@x.cz", "accessRole": "owner", "primary": True}, {"id": "s", "summary": SCHOOL, "accessRole": "owner"}]}
    svc = type("S", (), {"calendarList": lambda s: type("L", (), {"list": lambda s: type("E", (), {"execute": lambda s: cl})()})()})()
    assert calendars(svc) == [("s", SCHOOL)]
    g["EXTRA_CALS"] = ["primary", "Other", "Nope"]
    assert calendars(svc) == [("s", SCHOOL), ("p", "me@x.cz"), ("x", "Other")]
    g["EXTRA_CALS"] = []
    g["calendars"] = lambda svc: [("s", SCHOOL), ("primary", "me@example.com")]
    msg = evening(None, T("2026-11-02T20:00"))
    assert msg.startswith("**🌙 Zítra** úterý 3. 11.\n**🎓 Škola**"), msg
    assert "N103" in msg and "Vyraž" not in msg, msg
    assert "IIS" not in msg, msg  # the 11-05 exam is not tomorrow
    # each deadline once: it shows in tomorrow's Škola block, no separate deadline section
    assert msg.count("Fáze I") == 1 and "⏳ Zítra končí" not in msg, msg
    assert evening(None, T("2026-11-05T20:00")) == ""  # empty tomorrow: no message
    assert points_text(None) == "–" and points_text(7) == "7 b." and points_text(7, {"points": 20, "label": "zápočet"}) == "7/20 (zápočet)"
    import tempfile
    g["NOTES"] = Path(tempfile.mkdtemp()) / "n.json"
    assert load_notes() == {"notes": [], "thresholds": {}, "done": {}}  # missing
    NOTES.write_text("{broken")
    assert load_notes() == {"notes": [], "thresholds": {}, "done": {}}  # corrupt
    NOTES.write_text(json.dumps({"notes": [{"id": 1, "text": "a"}, {"text": "no id"}, 3], "thresholds": {"ISA": {"points": 20, "label": "zápočet"}, "X": {"points": "20"}}}))
    assert load_notes() == {"notes": [{"id": 1, "text": "a"}], "thresholds": {"ISA": {"points": 20, "label": "zápočet"}}, "done": {}}
    NOTES.write_text(json.dumps({"notes": [{"id": 2, "text": "r", "at": 5}], "done": {"ITU – odevzdání: Fáze I": "2026-11-03T23:59:00+01:00", "x": 1}}))
    assert load_notes()["notes"] == [] and list(load_notes()["done"]) == ["ITU – odevzdání: Fáze I"]
    # done deadlines leave the countdown; 3+ due in 10 days -> busy warning on top of the weekly
    g["datetime"] = type("D", (datetime,), {"now": classmethod(lambda c, tz=None: T("2026-11-02T17:00"))})
    w = weekly(None)
    assert "Náročné" not in w and "Fáze I" in w, w  # 1 left (IIS); the done ITU one still shows on its day
    data["s"] += [ev("2026-11-06T09:00", "2026-11-06T10:00", "IMP (zkouška)"), ev("2026-11-08T23:00", "2026-11-08T23:59", "ISA – odevzdání: Projekt")]
    w = weekly(None)
    assert "⚠️ Náročné období: 3 termínů" in w and w.index("Náročné") < w.index("Úterý"), w
    g["datetime"] = datetime
    # registration windows: open now and opening within a week are listed, later terms (the exam period brings many) are not
    wt = lambda o, c, **kw: {"course": "IIS", "kind": "zk", "registered": False, "free": True, "dates": [], "opens": o, "closes": c, **kw}  # noqa: E731
    g["studis"] = lambda: {"terms": [wt("2026-11-01T09:00", "2026-11-09T09:00"), wt("2026-11-05T09:00", "2026-11-12T09:00"), wt("2026-12-29T09:00", "2027-01-05T09:00"),
                                     wt(None, None, registered=True)]}
    r = registrations(T("2026-11-02T10:00"))
    assert r.count("\n") == 2 and "ZAREGISTRUJ SE" in r and "registrace od 5.11." in r and "29.12" not in r, r
    # titles in DMs: one line, markdown defused; ordinary titles unchanged
    assert plain("ISA (P) – odevzdání: Projekt 1") == "ISA (P) – odevzdání: Projekt 1" and plain("x" * 300) == "x" * 200
    assert plain("[klikni](https://x.cz) **a**\n# b <@1> <t:1:R>") == "\\[klikni\\](https://x.cz) \\*\\*a\\*\\* # b \\<@1\\> \\<t:1:R\\>"
    assert "\\[x\\]" in line({"summary": "[x](http://a)", "start": {"dateTime": "2026-11-02T10:00:00+01:00"}, "end": {"dateTime": "2026-11-02T11:00:00+01:00"}})
    print("selftest ok")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["--selftest"] else main()
