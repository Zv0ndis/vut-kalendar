"""Importing this loads .env next to it into os.environ (never overriding real env vars), so scripts also work run by hand.
systemd's EnvironmentFile does not strip inline comments, so values here are kept as written; put comments on their own line."""
import os, sys
from pathlib import Path

_f = Path(__file__).parent / ".env"
for _l in _f.read_text().splitlines() if _f.exists() else []:
    _k, _, _v = _l.partition("=")
    if _k.strip() and not _k.lstrip().startswith("#"):
        os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))


def num(name, default, kind=int):
    """Numeric env var; empty or invalid -> default with a warning."""
    v = os.environ.get(name, "").strip()
    try:
        return kind(v) if v else default
    except ValueError:
        print(f"bad {name}={v!r}, using {default}", file=sys.stderr)
        return default
