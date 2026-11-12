# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Daily Anthropic spend guard used by zhelper/agent.py.

record(r.usage) after every messages.create, ok() before asking. api-usage.json: date -> {calls, input, output, cache_read, cache_write, usd}.
ZHELPER_DAILY_USD caps the day (default 1.00); ZHELPER_PRICES="in,out,cache_read,cache_write" overrides the USD/MTok prices.
uv run spend.py --selftest
"""
import json, os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import envutil

USAGE = Path(__file__).parent / "api-usage.json"
PRAGUE = ZoneInfo("Europe/Prague")
KEYS = ("input", "output", "cache_read", "cache_write")
# claude-haiku-5-5, USD per MTok for prompts up to 100K tokens (longer ones cost 5x; ours are a few K)
DEFAULT_PRICE = "0.10,0.50,0.01,0.125"


def prices(s):
    try:
        p = [float(x) for x in s.split(",")]
        if len(p) != len(KEYS):
            raise ValueError(len(p))
        return dict(zip(KEYS, p))
    except ValueError:
        print(f"bad ZHELPER_PRICES {s!r}, using {DEFAULT_PRICE}")
        return prices(DEFAULT_PRICE)


PRICE = prices(os.environ.get("ZHELPER_PRICES", DEFAULT_PRICE))
CAP = envutil.num("ZHELPER_DAILY_USD", 1.0, float)


def _load():
    try:
        return json.loads(USAGE.read_text())
    except (OSError, ValueError):
        return {}


def today(now=None):
    return f"{(now or datetime.now(PRAGUE)):%Y-%m-%d}"


def ok(now=None):
    return _load().get(today(now), {}).get("usd", 0) < CAP


def record(usage, now=None):
    """Add one response's usage to today's row and return the row; usage=None (test stubs) is ignored. Never raises:
    a failure here must not cost the caller its answer, it only under-counts the day."""
    if usage is None:
        return None
    try:
        return _record(usage, now)
    except Exception as e:
        print(f"spend.record failed: {e!r}")
        return None


def _record(usage, now):
    n = dict(zip(KEYS, (usage.input_tokens, usage.output_tokens, getattr(usage, "cache_read_input_tokens", 0) or 0,
                        getattr(usage, "cache_creation_input_tokens", 0) or 0)))
    data = _load()
    row = data.setdefault(today(now), {"calls": 0, **dict.fromkeys(KEYS, 0), "usd": 0.0})
    row["calls"] += 1
    for k in KEYS:
        row[k] += n[k]
    row["usd"] = round(row["usd"] + sum(n[k] * PRICE[k] for k in KEYS) / 1e6, 6)
    old = today((now or datetime.now(PRAGUE)) - timedelta(days=60))
    data = {d: v for d, v in data.items() if d > old}
    # ponytail: read-modify-write without a lock; calls rarely overlap, a lost row only under-counts one call
    with tempfile.NamedTemporaryFile("w", dir=USAGE.parent, suffix=".tmp", delete=False) as f:
        json.dump(data, f, indent=1)
    os.replace(f.name, USAGE)
    return row


def line(now=None):
    r = _load().get(today(now)) or {"calls": 0, **dict.fromkeys(KEYS, 0), "usd": 0.0}
    seen = r["input"] + r["cache_read"] + r["cache_write"]
    return f"🤖 AI dnes: {r['calls']} dotazů, ${r['usd']:.2f} (cache {(r['cache_read'] / seen if seen else 0):.0%})"


if __name__ == "__main__" and sys.argv[1:] == ["--selftest"]:
    from types import SimpleNamespace as N
    USAGE = Path(tempfile.mkdtemp()) / "api-usage.json"
    PRICE, CAP = dict(zip(KEYS, (0.10, 0.50, 0.01, 0.125))), 1.0
    d1 = datetime(2026, 11, 1, 23, 59, tzinfo=PRAGUE)
    u = N(input_tokens=10**6, output_tokens=10**6, cache_read_input_tokens=10**6, cache_creation_input_tokens=10**6)
    r = record(u, d1)
    assert r["usd"] == 0.735 and r["calls"] == 1 and r["cache_read"] == 10**6, r
    assert ok(d1)
    record(u, d1)
    assert not ok(d1)  # 1.47 >= 1.00
    assert line(d1) == "🤖 AI dnes: 2 dotazů, $1.47 (cache 33%)", line(d1)
    d2 = d1 + timedelta(minutes=1)  # midnight: a new day starts at zero
    assert ok(d2) and line(d2) == "🤖 AI dnes: 0 dotazů, $0.00 (cache 0%)", line(d2)
    assert record(None, d1) is None  # test stubs without usage
    record(u, d1 + timedelta(days=61))
    assert "2026-11-01" not in _load()  # pruned after 60 days
    # never raises: unwritable file, broken usage object, bad price table
    USAGE = Path(tempfile.mkdtemp()) / "missing-dir" / "api-usage.json"
    assert record(u, d1) is None
    USAGE = Path(tempfile.mkdtemp()) / "api-usage.json"
    assert record(N(input_tokens="x", output_tokens=1), d1) is None
    PRICE = {}
    assert record(u, d1) is None
    # malformed ZHELPER_PRICES falls back to the defaults
    assert prices("1,2") == prices("abc") == prices(DEFAULT_PRICE) == dict(zip(KEYS, (0.10, 0.50, 0.01, 0.125)))
    assert prices("1,2,3,4")["cache_write"] == 4.0
    print("selftest ok")
