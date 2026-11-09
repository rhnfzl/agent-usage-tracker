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
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import CollectorUnavailable, UsageEvent, make_event_id
from ..net import HTTP_ERRORS, NoRedirect
from ._util import MS_PER_S, epoch_ms_to_iso, safe_int
from .cursor import connect_ro, cursor_db

NAME = "cursor_api"      # registry / config key + event_id namespace (internal)
_AGENT = "cursor"        # emitted agent label: this IS the accurate Cursor source, so it reads as
                         # plain "cursor" in Grafana + reports. The local `cursor` collector emits
                         # the same label: run one token source, or set cursor_human_only = true.
_ORIGIN = "https://cursor.com"
_BASE = f"{_ORIGIN}/api/dashboard"
_DAY_MS = 86_400 * MS_PER_S
_PAGE = 100
_MAX_PAGES = 200                 # hard stop (200*100 = 20k events/window) so a bug can't loop forever
_WINDOW_MS = _DAY_MS             # a gap is read oldest first, one day per request window


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
        except (TypeError, ValueError):
            team_id = 0
        cookie = f"WorkosCursorSessionToken={urllib.parse.quote(sub)}%3A%3A{token}"
        return cookie, team_id
    except (sqlite3.DatabaseError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None                      # malformed/partial token -> treat as no auth, never crash
    finally:
        con.close()


def _post(cookie: str, name: str, body: dict[str, Any]) -> Any:
    """POST a dashboard endpoint. Origin/Referer clear Cursor's CSRF ('Invalid origin') guard.
    A redirect is not followed: the session cookie must reach cursor.com and nowhere else.
    Raises one of HTTP_ERRORS on transport/HTTP failure; the caller decides how to handle it."""
    req = urllib.request.Request(
        f"{_BASE}/{name}", method="POST", data=json.dumps(body).encode(),
        headers={"Cookie": cookie, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0", "Origin": _ORIGIN,
                 "Referer": f"{_ORIGIN}/dashboard"})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as resp:
        return json.loads(resp.read())


def _same_user(owner: object, uid: object) -> bool:
    """True only when BOTH ids are present, non-empty SCALARS and equal. Guards the privacy
    boundary: a missing/empty owningUser must never match an empty/'None' uid, and a container
    (list/dict) must never match another equal container via str() coercion."""
    if not isinstance(owner, (str, int)) or not isinstance(uid, (str, int)):
        return False
    if isinstance(owner, bool) or isinstance(uid, bool):   # bool is an int subclass
        return False
    o, u = str(owner).strip(), str(uid).strip()
    return o != "" and u != "" and o.lower() != "none" and o == u


def _map_event(e: dict[str, Any], uid: object) -> UsageEvent | None:
    """One Cursor usage event -> UsageEvent, or None if it is not this user's or has no tokens."""
    if not _same_user(e.get("owningUser"), uid):
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


def describe(cfg: Config, wm: dict[str, Any]) -> str:
    if "since_ms" in wm:
        return f"since_ms={wm['since_ms']}"
    return "enabled, not yet scanned" if cfg.cursor_api else "disabled (cursor_api=false)"


def _page_events(batch: list[Any], uid: object, since: int) -> tuple[list[UsageEvent], int, int]:
    """(this user's events, how many rows were AT/AFTER the watermark, the newest of the events)."""
    events: list[UsageEvent] = []
    fresh_rows, newest = 0, since
    for raw in batch:
        if not isinstance(raw, dict):     # a stray row must not fail the page
            continue
        ms = safe_int(raw.get("timestamp"))
        # SKIP (not break) rows older than the watermark: robust to a page that isn't perfectly
        # newest-first, so a stray old row can't hide newer rows behind it.
        if ms < since:
            continue
        fresh_rows += 1             # count BEFORE _map_event so a page of teammate/zero rows
        ev = _map_event(raw, uid)   # (dropped here) doesn't look like "nothing new" and halt.
        if ev is not None:
            newest = max(newest, ms)
            events.append(ev)
    return events, fresh_rows, newest


def _fetch_events(cookie: str, team_id: int, uid: object, since: int, start: int,
                  end: int) -> tuple[list[UsageEvent], int]:
    """Read a complete window, splitting on saturation; an irreducible cap refuses an ACK."""
    events: list[UsageEvent] = []
    newest = since
    for page in range(1, _MAX_PAGES + 1):
        # Boundary events stay inside the request whether the API uses inclusive or strict dates.
        body = {"teamId": team_id, "userId": uid, "page": page, "pageSize": _PAGE,
                "startDate": start - 1, "endDate": end + 1}
        data = _post(cookie, "get-filtered-usage-events", body)
        batch = data.get("usageEventsDisplay") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise CollectorUnavailable("cursor_api: invalid usage page; watermark unchanged")
        bounded = [raw for raw in batch if isinstance(raw, dict)
                   and start <= safe_int(raw.get("timestamp")) <= end]
        got, _, page_newest = _page_events(bounded, uid, since)
        events += got
        newest = max(newest, page_newest)
        if len(batch) < _PAGE:
            return events, newest
    if start >= end:
        raise CollectorUnavailable("cursor_api: page cap reached within one millisecond; "
                                   "cannot prove completeness, watermark unchanged")
    events.clear()
    middle = (start + end) // 2
    left, left_newest = _fetch_events(cookie, team_id, uid, since, start, middle)
    right, right_newest = _fetch_events(cookie, team_id, uid, since, middle + 1, end)
    return left + right, max(left_newest, right_newest)


def collect(cfg: Config, wm: dict[str, Any]) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    if not cfg.cursor_api:
        return
    auth = _auth(cfg)
    if auth is None:
        if cursor_db(cfg).exists():
            raise CollectorUnavailable("cursor_api: no usable token in Cursor's state DB (signed "
                                       "out?); skipping this run")
        return
    cookie, team_id = auth
    try:
        me = _post(cookie, "get-me", {})
        uid = me.get("userId") if isinstance(me, dict) else None
        if not uid:                       # None, empty, or non-dict response -> can't self-scope, skip
            raise CollectorUnavailable("cursor_api: could not resolve a userId; skipping")
        since = safe_int(wm.get("since_ms"))
        # first run (no watermark): bounded lookback window instead of all history
        lo = since if since else _now_ms() - cfg.cursor_api_lookback_days * _DAY_MS
        # A gap is walked oldest first, one window at a time, and each window moves the watermark
        # once delivered: a month away can hold more events than one request's page cap.
        while True:
            now = _now_ms()
            hi = min(lo + _WINDOW_MS, now)
            events, newest = _fetch_events(cookie, team_id, uid, lo, lo, hi)
            # A closed window moves the watermark to its end (the next starts there, so an event
            # exactly on the edge is read twice and deduped). The open window ends at the newest
            # event, as before, so an event Cursor records late is still picked up.
            mark = hi if hi < now else newest

            def advance_watermark(w: dict[str, Any] = wm, n: int = mark) -> None:
                if n:
                    w["since_ms"] = n

            yield events, advance_watermark
            if hi >= now:
                return
            lo = hi
    except (*HTTP_ERRORS, json.JSONDecodeError, UnicodeDecodeError, AttributeError,
            TypeError) as exc:
        raise CollectorUnavailable(f"cursor_api: usage fetch failed ({exc}); skipping this run") \
            from exc


def _now_ms() -> int:
    return int(time.time() * MS_PER_S)
