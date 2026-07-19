"""Cursor usage-API collector (OPT-IN, network): accurate per-event token usage from Cursor's
own accounting, including CLI (`cursor-agent`) and SDK usage the local state.vscdb never records.

The local `cursor` collector is a lower bound (IDE bubbles only, no model/cache attribution) and
the Cursor CLI stores no token counts on disk at all. Cursor's dashboard, however, has an exact
per-event feed. This collector reads the local Cursor auth token (never stored, read at run time),
resolves the signed-in user, and pulls that user's OWN usage events.

Enable with `cursor_api = true` in config; OFF by default because it makes authenticated network
calls with your Cursor session token. Reads the token from the same state.vscdb the `cursor`
collector uses (or `cursor_db_path`); if Cursor is not installed, it no-ops.

PRIVACY / MULTI-USER SAFETY: on a team plan the events feed is team-scoped. This collector scopes
the query to the signed-in user's own userId AND re-checks `owningUser == userId` on every event
before emitting, so a teammate's usage can never be captured even if server-side scoping changed.
No prompt/response content is read - only token counts, model, and timestamp.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import epoch_ms_to_iso, safe_int
from .cursor import _cursor_db

NAME = "cursor_api"
_BASE = "https://cursor.com/api/dashboard"
_PAGE = 100
_MAX_PAGES = 200                 # hard stop (200*100 = 20k events/scan) so a bug can't loop forever


def _auth(cfg: Config) -> tuple[str, int] | None:
    """(session-cookie, teamId) from the local Cursor state DB, or None if unavailable."""
    db = _cursor_db(cfg)
    if db is None or not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.execute("PRAGMA query_only = ON")
    except sqlite3.DatabaseError:
        return None
    try:
        def item(k: str):
            row = con.execute("SELECT value FROM ItemTable WHERE key=?", (k,)).fetchone()
            return row[0] if row else None
        token = item("cursorAuth/accessToken")
        team_raw = item("cursorAuth/cachedTeam")
        if not token:
            return None
        # sub is the second JWT segment's "sub"; the web cookie is  <sub>::<jwt>  (:: url-encoded)
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub", "")
        try:
            team_id = int(json.loads(team_raw).get("teamId")) if team_raw else 0
        except (json.JSONDecodeError, TypeError, ValueError):
            team_id = 0
        cookie = f"WorkosCursorSessionToken={urllib.parse.quote(sub)}%3A%3A{token}"
        return cookie, team_id
    except (sqlite3.DatabaseError, ValueError, KeyError, IndexError,
            binascii.Error, json.JSONDecodeError):
        return None                      # malformed/partial token -> treat as no auth, never crash
    finally:
        con.close()


def _post(cookie: str, name: str, body: dict) -> Any:
    """POST a dashboard endpoint. Origin/Referer clear Cursor's CSRF ('Invalid origin') guard.
    Raises urllib.error on transport/HTTP failure; the caller decides how to handle it."""
    req = urllib.request.Request(
        f"{_BASE}/{name}", method="POST", data=json.dumps(body).encode(),
        headers={"Cookie": cookie, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0", "Origin": "https://cursor.com",
                 "Referer": "https://cursor.com/dashboard"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _map_event(e: dict, uid) -> UsageEvent | None:
    """One Cursor usage event -> UsageEvent, or None if it is not this user's or has no tokens."""
    if not isinstance(e, dict) or str(e.get("owningUser")) != str(uid):
        return None                       # SAFETY: never emit another team member's event
    tu = e.get("tokenUsage")
    if not isinstance(tu, dict):
        return None
    inp = safe_int(tu.get("inputTokens"))
    out = safe_int(tu.get("outputTokens"))
    cr = safe_int(tu.get("cacheReadTokens"))
    cw = safe_int(tu.get("cacheWriteTokens"))
    total = inp + out + cr + cw
    if total == 0:
        return None
    ts_ms = e.get("timestamp")
    conv = str(e.get("conversationId") or "")
    model = e.get("model")
    if not isinstance(model, str):
        model = None
    return UsageEvent(
        # no per-event id in the feed; (user, ts, conversation, model) is unique per event
        event_id=make_event_id(NAME, str(uid), str(ts_ms), conv, model or ""),
        ts=epoch_ms_to_iso(ts_ms),
        agent=NAME,
        model=model,
        provider="cursor",
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        total_tokens=total,
        project=None,
    )


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    if not cfg.cursor_api:            # opt-in: no network unless explicitly enabled
        return
    auth = _auth(cfg)
    if auth is None:
        return                       # Cursor not installed / no token -> silently skip
    cookie, team_id = auth
    try:
        me = _post(cookie, "get-me", {})
        uid = me.get("userId")
        if uid is None:
            print("aut: cursor_api: could not resolve userId; skipping", file=sys.stderr)
            return
        # incremental: only fetch events newer than the last one we committed (epoch ms)
        since = safe_int(wm.get("since_ms"))
        # first run (no watermark): bounded lookback window instead of all history
        start = since if since else _now_ms() - cfg.cursor_api_lookback_days * 86_400_000
        events: list[UsageEvent] = []
        newest = since
        for page in range(1, _MAX_PAGES + 1):
            body = {"teamId": team_id, "userId": uid, "page": page, "pageSize": _PAGE,
                    "startDate": start, "endDate": _now_ms()}
            data = _post(cookie, "get-filtered-usage-events", body)
            batch = data.get("usageEventsDisplay") if isinstance(data, dict) else None
            if not batch:
                break
            for raw in batch:
                ms = safe_int(raw.get("timestamp"))
                if ms < since:           # STRICTLY older than the watermark -> past new events, stop.
                    batch = None         # events at exactly `since` are re-emitted (event_id dedups
                    break                # at the sink) so a same-ms sibling event is never missed.
                ev = _map_event(raw, uid)
                if ev is None:
                    continue
                newest = max(newest, ms)
                events.append(ev)
            if batch is None or len(batch) < _PAGE:
                break
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        print(f"aut: cursor_api: usage fetch failed ({exc}); skipping this run", file=sys.stderr)
        return

    def commit(w: dict = wm, n: int = newest) -> None:
        if n:
            w["since_ms"] = n

    yield events, commit


def _now_ms() -> int:
    # isolated so the (rare) no-clock test envs and the collector share one source of "now"
    import time
    return int(time.time() * 1000)
