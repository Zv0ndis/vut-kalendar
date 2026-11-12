"""Client for the official VUT IS API (https://api.vut.cz), stdlib only.

OAuth2 client_credentials. The API allows 10 tokens per hour per client and a token lives 1 h, so ONE token is cached in a
0600 file next to this module and shared by every script, and a file lock keeps concurrent runs from each minting their own.
The quota is protected on every side:
- a token the API refuses is replaced once per process, and only if the cache still holds that very token (another run may
  have renewed it already); a second refusal stops the run (no more calls, no more tokens);
- a failed or rate-limited minting leaves a "fail_until" marker in the cache, so the next runs fail fast instead of retrying.
Credentials: VUT_API_CLIENT_ID / VUT_API_SECRET in the environment or in .env next to this file. Neither the secret nor the
token is ever printed or put into an error message. Redirects are refused, so the Authorization header is never forwarded.
"""
import base64, fcntl, json, os, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
API = "https://api.vut.cz"
TOKEN_URL = "https://id.vut.cz/auth/common/oauth2/token"
CACHE = Path(os.environ.get("VUT_TOKEN_FILE") or HERE / ".vut-token.json")
SPARE = 300  # reuse a token until 5 min before it expires
PAUSE = 900  # after a failed minting, no new attempt for 15 min
_state = {"refreshed": False, "dead": False}  # per process: one forced refresh at most; a second refusal stops the run


class ApiError(Exception):
    pass


class TokenError(ApiError):
    """No usable token (minting failed or paused, token refused twice): every further call would fail too."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # the 3xx surfaces as an HTTPError


OPENER = urllib.request.build_opener(NoRedirect)


def creds():
    env = dict(os.environ)
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"'))
    if not (env.get("VUT_API_CLIENT_ID") and env.get("VUT_API_SECRET")):
        raise TokenError("VUT_API_CLIENT_ID / VUT_API_SECRET not set")
    return env["VUT_API_CLIENT_ID"], env["VUT_API_SECRET"]


def configured():
    """True when API credentials are set; without them every script quietly uses the Studis scraper only."""
    try:
        creds()
        return True
    except TokenError:
        return False


def _mint():
    cid, secret = creds()
    req = urllib.request.Request(TOKEN_URL, b"grant_type=client_credentials", {
        "Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode(), "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with OPENER.open(req, timeout=30) as r:
            d = json.load(r)
        tok = d["access_token"]
        if not isinstance(tok, str) or not tok:
            raise ValueError
        life = min(max(int(d.get("expires_in", 3600)), 600), 86400)  # clamp: a bogus value must not pin a dead token for days, or expire it at once
    except urllib.error.HTTPError as e:  # status only: the body or request could echo credentials
        raise TokenError(f"token request failed: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise TokenError(f"token request failed: {type(e).__name__}") from None
    return {"access_token": tok, "expires_at": time.time() + life}


def _store(c):
    """Cache file, atomically and 0600; a leftover temp file is removed first. False when the disk refuses (the caller still has its token)."""
    tmp = CACHE.with_name(CACHE.name + ".tmp")
    try:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(c, f)
        os.replace(tmp, CACHE)
        return True
    except OSError:
        return False


def token(rejected=None):
    """The cached access token. Mints one (under the lock) only when none is left, or when `rejected` - the token the API just refused - is still the cached one."""
    try:
        lock = os.fdopen(os.open(CACHE.with_name(CACHE.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600), "w")
    except OSError as e:
        raise TokenError(f"token cache unavailable: {type(e).__name__}") from None
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            c = json.loads(CACHE.read_text())
        except (OSError, ValueError):
            c = {}
        now = time.time()
        if not isinstance(c, dict):
            c = {}
        if now < c.get("fail_until", 0):
            raise TokenError("token minting paused after a failure")
        if isinstance(c.get("access_token"), str) and c.get("expires_at", 0) - now > SPARE and c["access_token"] != rejected:
            return c["access_token"]
        try:
            new = _mint()
        except TokenError:
            _store({"fail_until": now + PAUSE})
            raise
        _store(new)  # a failing disk costs a token next time, not this run
        return new["access_token"]


def get(path, **params):
    """GET api.vut.cz<path>?params -> parsed JSON. A 401 swaps the token once per process (then the run is stopped); 429/5xx/network errors retry after a pause."""
    if _state["dead"]:
        raise TokenError("API refused the token twice; stopped for this run")
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    tok, attempt = token(), 0
    while True:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
        try:
            with OPENER.open(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                if _state["refreshed"]:
                    _state["dead"] = True
                    raise TokenError("API refused the token twice; stopped for this run") from None
                _state["refreshed"] = True
                tok = token(rejected=tok)
                continue
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise ApiError(f"GET {path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt == 2:
                raise ApiError(f"GET {path}: {type(e).__name__}") from None
        attempt += 1
        time.sleep(2 * attempt)
