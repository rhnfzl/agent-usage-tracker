"""ActivityWatch collector (OPT-IN, local HTTP): not-AFK window time as agents.time_interval rows.

Reads THIS host's window and AFK buckets through aw-server's own query API, keeps only window
time while the AFK watcher says not-afk, and emits one TimeInterval per (clipped) window event.
Nothing leaves the machine before these guards run, in this order:

  1. login window / screen saver rows are dropped (not active time);
  2. deny-list: password managers, private-browsing titles, and whatever the config adds
     (matched on the title and the URL) are dropped entirely (no interval, no app, no title);
  3. titles are redacted (keys, tokens, JWTs, long hex/base64 runs, password=..., URL
     credentials, emails) and cut to 300 chars; `activitywatch_titles = false` sends none, and
     when activitywatch_title_days is set, a title older than that is never sent (default 0:
     titles are kept for good).

Category and work relevance come from an app mapping (config over code defaults); a browser row
is 'web-work' when its title or URL matches a work pattern, else 'web'. A scan never fails
because ActivityWatch is closed: no server or no buckets means no rows.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import TimeInterval, make_event_id

NAME = "activitywatch"
_LAG = timedelta(minutes=5)          # aw-watcher-window extends its last event; leave it settle
_CHUNK = timedelta(days=1)           # one query per pass; the scan's drain loop runs the next day
_MIN_RANGE = timedelta(minutes=1)    # below this, wait for the next scan (lets the drain loop end)
_TITLE_MAX = 300
_PURGE_BATCH = 500
_PURGE_BUDGET = 20.0                 # seconds of GET+DELETE per scan; a backlog drains over scans
_TIMEOUT = 10

_QUERY = """window = flood(query_bucket({win}));
afk = flood(query_bucket({afk}));
not_afk = filter_keyvals(afk, "status", ["not-afk"]);
RETURN = filter_period_intersect(window, not_afk);"""

R = "[redacted]"
# (name, pattern, replacement). Order matters: URL credentials before emails, specific before
# the generic long run.
_REDACT = (
    ("url_credentials", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I),
     r"\1" + R + "@"),
    # no leading \b: env names (PGPASSWORD=, DB_PASSWORD=, GITHUB_TOKEN=) carry a prefix
    ("assignment", re.compile(r"([A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key"
                              r"|access[_-]?key))(\s*[=:]\s*)\S+", re.I), r"\1\2" + R),
    ("bearer", re.compile(r"\b(bearer\s+)\S+", re.I), r"\1" + R),
    ("jwt", re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), R),
    ("sk_key", re.compile(r"\bsk-[\w-]{16,}"), R),
    ("github_pat", re.compile(r"\bgithub_pat_\w{20,}"), R),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), R),
    ("slack_token", re.compile(r"\bxox[abprs]-[\w-]{10,}"), R),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), R),
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), R),
    # long hex and base64 runs; also takes long slugs and paths, which is the safe side
    ("long_run", re.compile(r"[A-Za-z0-9+/_=-]{32,}"), R),
)

_IDLE_APPS = ("loginwindow", "screensaverengine", "screen saver")
_DENY_APPS = ("1password", "bitwarden", "keepassxc", "keychain access", "passwords", "dashlane",
              "lastpass", "proton pass")
_DENY_TITLES = (r"private browsing", r"\bincognito\b", r"\binprivate\b")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")   # Postgres text refuses NUL; the rest is noise

_EDITORS = ("code", "visual studio code", "cursor", "zed", "sublime text", "xcode",
            "intellij idea", "pycharm", "webstorm", "goland", "clion", "rider", "rubymine",
            "phpstorm", "datagrip", "rustrover", "android studio", "fleet")
_DEFAULT_APPS = {
    **{a: "editor" for a in _EDITORS},
    **{a: "terminal" for a in ("terminal", "iterm2", "iterm", "ghostty", "warp", "supacode")},
    **{a: "agent" for a in ("claude", "chatgpt", "codex")},
    **{a: "comms" for a in ("slack", "microsoft teams", "zoom", "zoom.us")},
    **{a: "docs" for a in ("notion", "obsidian", "typora", "notes")},
    "figma": "design",
    **{a: "media" for a in ("spotify", "music", "vlc", "iina", "quicktime player")},
    **{a: "browser" for a in ("safari", "google chrome", "chrome", "chromium", "brave browser",
                              "arc", "firefox", "helium", "microsoft edge")},
}
_WORK_TITLES = (
    r"github|gitlab|bitbucket|codeberg|sourcegraph|pull request|merge request",
    r"\bjira\b|linear|youtrack|sentry|confluence",
    r"\bdocs?\b|documentation|readthedocs|stack ?overflow|\bmdn\b|pypi|npmjs|api reference",
    r"\baws\b|console\.cloud\.google|portal\.azure|cloudflare|vercel|grafana|kubernetes",
    r"localhost|127\.0\.0\.1",
    r"chatgpt|claude|gemini|perplexity",
)
_RELEVANT = frozenset(("editor", "terminal", "agent", "comms", "docs", "design", "web-work"))


def _http(cfg: Config, method: str, path: str, body: Any = None) -> Any:
    """The one HTTP call to aw-server (tests stub this). Raises on transport or HTTP failure."""
    url = cfg.activitywatch_url.rstrip("/") + path
    if not url.startswith(("http://", "https://")):
        raise ValueError("activitywatch_url must be http(s)")
    req = urllib.request.Request(url, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _buckets(cfg: Config) -> tuple[str, str, datetime] | None:
    """(window bucket, afk bucket, window bucket's first moment) of THIS host, or None."""
    host = _http(cfg, "GET", "/api/0/info").get("hostname")
    buckets = _http(cfg, "GET", "/api/0/buckets/")

    def pick(kind: str) -> str | None:
        ids = sorted(b for b, v in buckets.items()
                     if isinstance(v, dict) and v.get("type") == kind and v.get("hostname") == host)
        return ids[0] if ids else None

    win, afk = pick("currentwindow"), pick("afkstatus")
    if not (host and win and afk):
        return None
    meta = buckets[win].get("metadata") or {}
    if meta.get("start"):
        return win, afk, _parse(meta["start"])
    # aw-server-rust writes 'created' in local time labelled UTC, up to 14 h late: start a day
    # earlier (empty chunks cost one query each)
    return win, afk, _parse(buckets[win]["created"]) - timedelta(days=1)


def _norm(app: str) -> str:
    return app.strip().lower()


def _lookup(table, name: str):
    """Exact name, else a known name followed by a space ('pycharm professional')."""
    if name in table:
        return table[name]
    return next((v for k, v in table.items() if name.startswith(k + " ")), None)


def denied(cfg: Config, app: str, title: str, data: dict | None = None) -> bool:
    """Password managers, private browsing, and config deny patterns, matched on title + URL.
    A Chromium private window (Brave, Chrome, Helium) has neither a title marker nor an
    incognito flag from today's watcher, so it is not detectable here."""
    name = _norm(app)
    if _lookup(dict.fromkeys((*_DENY_APPS, *cfg.activitywatch_deny_apps), True), name):
        return True
    if (data or {}).get("incognito") is True:
        return True
    text = f"{title} {(data or {}).get('url') or ''}"
    return any(re.search(p, text, re.I) for p in (*_DENY_TITLES, *cfg.activitywatch_deny_titles))


def clean_title(title: str) -> str:
    for _, pattern, repl in _REDACT:
        title = pattern.sub(repl, title)
    return title[:_TITLE_MAX]


def browser_category(cfg: Config, title: str, url: str = "") -> tuple[str, str]:
    """Browser row -> (web-work | web, rule). Seam: phase 4 replaces this rule with a model.
    The URL is matched here, on the machine, and is never sent."""
    text = f"{title} {url}"
    if any(re.search(p, text, re.I) for p in (*_WORK_TITLES, *cfg.activitywatch_work_titles)):
        return "web-work", "browser_work_title"
    return "web", "browser_other"


def classify(cfg: Config, app: str, title: str, url: str = "") -> tuple[str, bool, str]:
    """(category, work_relevant, rule) for one window row."""
    name = _norm(app)
    cat, rule = _lookup(cfg.activitywatch_categories, name), "config_app"
    if cat is None:
        cat, rule = _lookup(_DEFAULT_APPS, name), "default_app"
    if cat is None:
        cat, rule = "other", "unknown"
    if cat == "browser":
        cat, rule = browser_category(cfg, title, url)
    return cat, cat in _RELEVANT, rule


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[TimeInterval], Callable[[], None]]]:
    try:
        found = _buckets(cfg)
        if found is None:
            return
        win, afk, first = found
        start = _parse(wm["end"]) if wm.get("end") else first
        end = min(_now() - _LAG, start + _CHUNK)
        if end - start < _MIN_RANGE:
            return
        query = _QUERY.format(win=json.dumps(win), afk=json.dumps(afk)).splitlines()
        res = _http(cfg, "POST", "/api/0/query/",
                    {"query": query, "timeperiods": [f"{start.isoformat()}/{end.isoformat()}"]})
        raw = res[0] if isinstance(res, list) and res and isinstance(res[0], list) else []
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, TypeError,
            AttributeError, KeyError) as exc:
        print(f"aut: activitywatch: aw-server not usable ({type(exc).__name__}); skipping",
              file=sys.stderr)
        return

    rows: list[TimeInterval] = []
    days = cfg.activitywatch_title_days
    title_from = _now() - timedelta(days=days) if days else None
    denied_n, denied_s, idle_s = 0, 0.0, 0.0
    for ev in raw:
        try:
            s = _parse(ev["timestamp"])
            e = s + timedelta(seconds=float(ev.get("duration") or 0))
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        s, e = max(s, start), min(e, end)
        if e <= s:
            continue
        secs = (e - s).total_seconds()
        app = _CTRL.sub("", str(data.get("app") or ""))
        title = _CTRL.sub(" ", str(data.get("title") or ""))
        if _norm(app) in _IDLE_APPS:
            idle_s += secs
            continue
        if denied(cfg, app, title, data):
            denied_n, denied_s = denied_n + 1, denied_s + secs
            continue
        category, relevant, rule = classify(cfg, app, title, str(data.get("url") or ""))
        keep = cfg.activitywatch_titles and (title_from is None or s >= title_from)
        sent = (clean_title(title) or None) if keep else None
        s_iso, e_iso = s.astimezone(timezone.utc).isoformat(), e.astimezone(timezone.utc).isoformat()
        # No title in the id: only one window has focus at a time, and an id must not let anyone
        # confirm a guessed title.
        rows.append(TimeInterval(
            event_id=make_event_id(NAME, cfg.machine, app, s_iso, e_iso),
            ts=s_iso, end_ts=e_iso, source="aw", app=app or None, title=sent,
            category=category, work_relevant=relevant, attrs={"rule": rule}))
    if denied_n or idle_s:
        print(f"aut: activitywatch: dropped {denied_n} intervals ({denied_s / 60:.1f} min) by the "
              f"deny-list, {idle_s / 60:.1f} min idle (login window / screen saver)",
              file=sys.stderr)

    def commit(w: dict = wm, end_iso: str = end.isoformat()) -> None:
        w["end"] = end_iso

    yield rows, commit


def purge(cfg: Config) -> int:
    """Delete aw-server's own window events older than activitywatch_title_days, in batches of
    _PURGE_BATCH until none is left or _PURGE_BUDGET seconds pass. Best effort: it never raises,
    and logs a count only. activitywatch_title_days = 0 (the default) keeps everything."""
    n = 0
    if cfg.activitywatch_title_days <= 0:
        return 0
    deadline = time.monotonic() + _PURGE_BUDGET
    try:
        found = _buckets(cfg)
        if found is None:
            return 0
        win = urllib.parse.quote(found[0], safe="")
        cutoff = _now() - timedelta(days=cfg.activitywatch_title_days)
        query = urllib.parse.urlencode({"end": cutoff.isoformat(), "limit": _PURGE_BATCH})
        # the server answers newest first, so each batch is the next older slice: loop to the end
        while time.monotonic() < deadline:
            batch = _http(cfg, "GET", f"/api/0/buckets/{win}/events?{query}") or []
            # aw-server-rust clips the event spanning `end` to it, so one that reaches the cutoff
            # is partly inside the retention window and stays
            gone = [int(ev["id"]) for ev in batch if _parse(ev["timestamp"])
                    + timedelta(seconds=float(ev.get("duration") or 0)) < cutoff]
            if not gone:
                break
            for i in gone:
                _http(cfg, "DELETE", f"/api/0/buckets/{win}/events/{i}")
                n += 1
    except Exception as exc:                 # purge must never fail a scan
        print(f"aut: activitywatch: purge stopped ({type(exc).__name__})", file=sys.stderr)
    if n:
        print(f"aut: activitywatch: purged {n} window events older than "
              f"{cfg.activitywatch_title_days} days", file=sys.stderr)
    return n
