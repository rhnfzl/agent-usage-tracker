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

Category and work relevance come from an app mapping (config over code defaults, then the app's
declared macOS category); a browser row is 'web-work' when its title or URL matches a work
pattern, else 'web'. A scan never fails because ActivityWatch is closed: no server or no buckets
means no rows and a 'degraded' heartbeat.
"""

from __future__ import annotations

import functools
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import CollectorUnavailable, TimeInterval, make_event_id
from ..net import HTTP_ERRORS
from ..redact import clean_text
from ..sink import LOCAL_HOSTS
from ._util import dict_or_empty

NAME = "activitywatch"
_LAG = timedelta(minutes=5)          # aw-watcher-window extends its last event; leave it settle
_CHUNK = timedelta(days=1)           # one query per pass; the scan's drain loop runs the next day
_MIN_RANGE = timedelta(minutes=1)    # below this, wait for the next scan (lets the drain loop end)
_PURGE_BATCH = 500
_PURGE_BUDGET = 20.0                 # seconds of GET+DELETE per scan; a backlog drains over scans
_TIMEOUT = 10

_QUERY = """window = flood(query_bucket({win}));
afk = flood(query_bucket({afk}));
not_afk = filter_keyvals(afk, "status", ["not-afk"]);
RETURN = filter_period_intersect(window, not_afk);"""

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
                              "arc", "firefox", "helium", "microsoft edge", "dia", "vivaldi",
                              "opera", "zen", "orion", "comet", "duckduckgo", "tor browser",
                              "librewolf", "sigmaos", "ladybird")},
}
_WORK_TITLES = (
    r"github|gitlab|bitbucket|codeberg|sourcegraph|pull request|merge request",
    r"\bjira\b|linear|youtrack|sentry|confluence",
    r"\bdocs?\b|documentation|readthedocs|stack ?overflow|\bmdn\b|pypi|npmjs|api reference",
    r"\baws\b|console\.cloud\.google|portal\.azure|cloudflare|vercel|grafana|kubernetes",
    r"localhost|127\.0\.0\.1",
    r"chatgpt|claude|gemini|perplexity",
)
_RELEVANT = frozenset(("editor", "terminal", "agent", "dev", "comms", "docs", "design",
                       "web-work"))

_DARWIN = sys.platform == "darwin"
_MAC_TIMEOUT = 2
# local app folders only (and ~/Applications): a bundle on a network or external volume could
# block the plist read
_MAC_APP_DIRS = ("/Applications/", "/System/")
# LSApplicationCategoryType (after 'public.app-category.') -> category; games-* are media too.
# 'browser' is what _MacCategories reports for a bundle that opens http(s) links.
_MAC_CATEGORIES = {
    "developer-tools": "dev", "business": "comms", "productivity": "docs",
    "graphics-design": "design", "browser": "browser",
    **dict.fromkeys(("music", "video", "entertainment", "games", "social-networking", "sports",
                     "lifestyle", "news", "photography"), "media"),
}


@functools.lru_cache(maxsize=None)     # once per URL per run: a scan makes many calls
def _warn_if_plaintext(url: str) -> None:
    """Window titles travel this link, and purge deletes over it. Warn, never refuse: an aw-server
    on a private network works today."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in LOCAL_HOSTS:
        print(f"aut: warning: activitywatch_url reaches {parsed.hostname} over plain http, so window "
              "titles travel unencrypted. Use https:// unless the link is private (for example "
              "Tailscale).", file=sys.stderr)


def _http(cfg: Config, method: str, path: str, body: Any = None) -> Any:
    """The one HTTP call to aw-server (tests stub this). Raises on transport or HTTP failure."""
    url = cfg.activitywatch_url.rstrip("/") + path
    if not url.startswith(("http://", "https://")):
        raise ValueError("activitywatch_url must be http(s)")
    _warn_if_plaintext(cfg.activitywatch_url)
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


def _lookup(table: dict[str, Any], name: str) -> Any:
    """Exact name, else a known name followed by a space ('pycharm professional')."""
    if name in table:
        return table[name]
    return next((v for k, v in table.items() if name.startswith(k + " ")), None)


class _MacCategories:
    """macOS lookups: once per app, and the first timeout (a stalled Spotlight) ends them. Each
    returns 'browser' when the app bundle opens http(s) links (browsers declare
    productivity), else its declared LSApplicationCategoryType, or None: off macOS, no local
    bundle, no key, or any error or timeout. The app name only goes to the local Spotlight index."""

    def __init__(self) -> None:
        self.seen: dict[str, str | None] = {}
        self.off = False

    def __call__(self, app: str) -> str | None:
        if app not in self.seen:
            self.seen[app] = None if self.off else self._lookup(app)
        return self.seen[app]

    def _lookup(self, app: str) -> str | None:
        if not _DARWIN or not app or any(c in app for c in "'\\*"):  # no quoting
            return None
        query = ("kMDItemContentType == 'com.apple.application-bundle' && "
                 f"kMDItemDisplayName == '{app}'")
        dirs = (*_MAC_APP_DIRS, os.path.expanduser("~/Applications/"))
        try:
            out = subprocess.run(["mdfind", query], capture_output=True, text=True, check=True,
                                 timeout=_MAC_TIMEOUT).stdout.splitlines()
            hit = next((p for p in out if p.startswith(dirs)), None)
            if hit is None:
                return None
            with open(f"{hit}/Contents/Info.plist", "rb") as f:
                info = plistlib.load(f)
            schemes = {str(x).lower() for t in info.get("CFBundleURLTypes") or ()
                       for x in t.get("CFBundleURLSchemes") or ()}
            cat = "browser" if schemes & {"http", "https"} \
                else info.get("LSApplicationCategoryType")
        except subprocess.TimeoutExpired:
            self.off = True
            return None
        except Exception:                 # a lookup must never fail a scan
            return None
        return cat if isinstance(cat, str) and cat else None


# One per process, not per collect(): a pass covers one day, and a catch-up runs many.
_MAC = _MacCategories()


def _category_from_declared(declared: str) -> str:
    kind = declared.removeprefix("public.app-category.")
    return "media" if kind.endswith("games") else _MAC_CATEGORIES.get(kind, "other")


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
    if cat is None and (declared := _MAC(app.strip())):
        cat, rule = _category_from_declared(declared), "macos_category"
    if cat is None:
        cat, rule = "other", "unknown"
    if cat == "browser":
        cat, rule = browser_category(cfg, title, url)
    return cat, cat in _RELEVANT, rule


def describe(cfg: Config, wm: dict) -> str:
    return f"read until {wm['end']}" if "end" in wm else "never scanned"


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[TimeInterval], Callable[[], None]]]:
    try:
        found = _buckets(cfg)
        if found is None:
            raise CollectorUnavailable("activitywatch: aw-server has no window and AFK buckets for "
                                       "this host; skipping")
        win, afk, first = found
        start = _parse(wm["end"]) if wm.get("end") else first
        end = min(_now() - _LAG, start + _CHUNK)
        if end - start < _MIN_RANGE:
            return
        query = _QUERY.format(win=json.dumps(win), afk=json.dumps(afk)).splitlines()
        res = _http(cfg, "POST", "/api/0/query/",
                    {"query": query, "timeperiods": [f"{start.isoformat()}/{end.isoformat()}"]})
        raw = res[0] if isinstance(res, list) and res and isinstance(res[0], list) else []
    except (*HTTP_ERRORS, ValueError, TypeError, AttributeError, KeyError) as exc:
        raise CollectorUnavailable(f"activitywatch: aw-server not usable ({type(exc).__name__}); "
                                   "skipping") from exc

    rows: list[TimeInterval] = []
    days = cfg.activitywatch_title_days
    title_from = _now() - timedelta(days=days) if days else None
    denied_n, denied_s, idle_s = 0, 0.0, 0.0
    for ev in raw:
        try:
            s = _parse(ev["timestamp"])
            e = s + timedelta(seconds=float(ev.get("duration") or 0))
            data = dict_or_empty(ev.get("data"))
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
        sent = (clean_text(title) or None) if keep else None
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

    def advance_watermark(w: dict = wm, end_iso: str = end.isoformat()) -> None:
        w["end"] = end_iso

    yield rows, advance_watermark


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
