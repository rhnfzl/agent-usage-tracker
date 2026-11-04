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
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import CollectorUnavailable, UsageEvent, make_event_id
from ..net import HTTP_ERRORS, NoRedirect
from ._util import epoch_ms_to_iso, safe_int
from .cursor import connect_ro, cursor_db

NAME = "cursor_api"      # registry / config key + event_id namespace (internal)
_AGENT = "cursor"        # emitted agent label: this IS the accurate Cursor source, so it reads as
                         # plain "cursor" in Grafana + reports (the local lower-bound collector is
                         # kept out of the default agent set, so there is no clash).
_BASE = "https://cursor.com/api/dashboard"
_PAGE = 100
_MAX_PAGES = 200                 # hard stop (200*100 = 20k events/scan) so a bug can't loop forever


def _auth(cfg: Config) -> tuple[str, int] | None:
    """(session-cookie, teamId) from the local Cursor state DB, or None if unavailable."""
    db = cursor_db(cfg)
    if not db.exists():
        return None
    con = connect_ro(db)
    if con is None:
        return None
    try:
        def item(k: str) -> Any:
            row = con.execute("SELECT value FROM ItemTable WHERE key=?", (k,)).fetchone()
            return row[0] if row else None
        token = item("cursorAuth/accessToken")
        team_raw = item("cursorAuth/cachedTeam")
        if not token:
            return None
        # sub is the second JWT segment's "sub"; the web cookie is  <sub>::<jwt>  (:: url-encoded)
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(claims, dict):     # a JWT payload that decodes to a list/scalar
            return None
        sub = claims.get("sub", "")
        try:
            team = json.loads(team_raw) if team_raw else {}
            team_id = int(team.get("teamId") or 0) if isinstance(team, dict) else 0
        except (json.JSONDecodeError, TypeError, ValueError):
            team_id = 0
        cookie = f"WorkosCursorSessionToken={urllib.parse.quote(sub)}%3A%3A{token}"
        return cookie, team_id
    except (sqlite3.DatabaseError, ValueError, KeyError, IndexError, TypeError,
            AttributeError, binascii.Error, json.JSONDecodeError):
        return None                      # malformed/partial token -> treat as no auth, never crash
    finally:
        con.close()


def _post(cookie: str, name: str, body: dict) -> Any:
    """POST a dashboard endpoint. Origin/Referer clear Cursor's CSRF ('Invalid origin') guard.
    A redirect is not followed: the session cookie must reach cursor.com and nowhere else.
    Raises one of HTTP_ERRORS on transport/HTTP failure; the caller decides how to handle it."""
    req = urllib.request.Request(
        f"{_BASE}/{name}", method="POST", data=json.dumps(body).encode(),
        headers={"Cookie": cookie, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0", "Origin": "https://cursor.com",
                 "Referer": "https://cursor.com/dashboard"})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as resp:
        return json.loads(resp.read())


def _same_user(owner: object, uid: object) -> bool:
    """True only when BOTH ids are present, non-empty SCALARS and equal. Guards the privacy
    boundary: a missing/empty owningUser must never match an empty/'None' uid, and a container
    (list/dict) must never match another equal container via str() coercion."""
    if not isinstance(owner, (str, int)) or not isinstance(uid, (str, int)):
        return False                      # scalar-only (also excludes list/dict/None)
    if isinstance(owner, bool) or isinstance(uid, bool):   # bool is an int subclass
        return False
    o, u = str(owner).strip(), str(uid).strip()
    return o != "" and u != "" and o.lower() != "none" and o == u


def _map_event(e: dict, uid: object) -> UsageEvent | None:
    """One Cursor usage event -> UsageEvent, or None if it is not this user's or has no tokens."""
    if not isinstance(e, dict) or not _same_user(e.get("owningUser"), uid):
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
        agent=_AGENT,
        model=model,
        provider="cursor",
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        total_tokens=total,
        project=None,
    )


def describe(cfg: Config, wm: dict) -> str:
    if "since_ms" in wm:
        return f"since_ms={wm['since_ms']}"
    return "enabled, not yet scanned" if cfg.cursor_api else "disabled (cursor_api=false)"


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    if not cfg.cursor_api:            # opt-in: no network unless explicitly enabled
        return
    auth = _auth(cfg)
    if auth is None:
        if cursor_db(cfg).exists():
            raise CollectorUnavailable("cursor_api: no usable token in Cursor's state DB (signed "
                                       "out?); skipping this run")
        return                       # Cursor not installed -> nothing to read
    cookie, team_id = auth
    try:
        me = _post(cookie, "get-me", {})
        uid = me.get("userId") if isinstance(me, dict) else None
        if not uid:                       # None, empty, or non-dict response -> can't self-scope, skip
            raise CollectorUnavailable("cursor_api: could not resolve a userId; skipping")
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
            fresh_rows = 0                  # rows AT/AFTER the watermark (whether or not we emit them)
            for raw in batch:
                ms = safe_int(raw.get("timestamp"))
                # SKIP (not break) rows older than the watermark: robust to a page that isn't
                # perfectly newest-first, so a stray old row can't hide newer rows behind it.
                if ms < since:
                    continue
                fresh_rows += 1             # count BEFORE _map_event so a page of teammate/zero rows
                ev = _map_event(raw, uid)   # (dropped here) doesn't look like "nothing new" and halt.
                if ev is None:
                    continue
                newest = max(newest, ms)
                events.append(ev)
            if fresh_rows == 0 or len(batch) < _PAGE:
                break                       # a whole page was older than the watermark, or last page
        else:
            # loop ran the full cap without a natural stop: a very large window remains unread.
            # Never happens for 6h deltas; only a huge first backfill. Flag it (don't silently orphan).
            print(f"aut: cursor_api: reached the {_MAX_PAGES}-page cap; older events this window may "
                  "be unread -- shorten cursor_api_lookback_days or run again.", file=sys.stderr)
    except (*HTTP_ERRORS, json.JSONDecodeError, UnicodeDecodeError, AttributeError,
            TypeError) as exc:
        raise CollectorUnavailable(f"cursor_api: usage fetch failed ({exc}); skipping this run") \
            from exc

    def advance_watermark(w: dict = wm, n: int = newest) -> None:
        if n:
            w["since_ms"] = n

    yield events, advance_watermark


def _now_ms() -> int:
    return int(time.time() * 1000)
