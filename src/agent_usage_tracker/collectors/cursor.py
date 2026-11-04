"""Cursor collector (EXPERIMENTAL): local state.vscdb SQLite.

Cursor stores per-message rows in cursorDiskKV under `bubbleId:<composerId>:<bubbleId>`,
each with a tokenCount {inputTokens, outputTokens} and sometimes a usageUuid. Reality
checks from real data:
  - the model id is NOT stored on the bubble (dashboard API would have it),
  - most token bubbles carry no timestamp on the bubble itself, but joining to the
    `composerData:<composerId>` row recovers a createdAt for ~100% of them.

Because the DB is small and Cursor can write a bubble's tokenCount AFTER first insert,
we full-scan every run (no watermark) and rely on event_id + ON CONFLICT to dedup at the
sink. This is a LOWER BOUND: sessions with no local snapshot are invisible. For complete,
model-attributed, timestamped data, use the Cursor dashboard API with the
`cursorAuth/accessToken` cookie from ItemTable (documented follow-up, not in v1).

Known limitation: the first NONZERO tokenCount seen for a bubble wins. If Cursor later
rewrites that bubble to a different nonzero count, the sink's ON CONFLICT DO NOTHING keeps
the original (a 0 -> nonzero fill-in is fine, since zero rows are skipped). Rare in practice.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Iterator

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import epoch_ms_to_iso

NAME = "cursor"
# Cursor stores state.vscdb in the OS-specific app-data dir. Subpath is the same everywhere.
_CURSOR_SUBPATH = "Cursor/User/globalStorage/state.vscdb"
_BATCH = 500


def cursor_db(cfg: Config) -> Path | None:
    """Locate Cursor's state.vscdb across macOS / Windows / Linux, or an explicit override."""
    if cfg.cursor_db_path:
        return Path(cfg.cursor_db_path).expanduser()
    home = cfg.home
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / _CURSOR_SUBPATH
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") / _CURSOR_SUBPATH
    return home / ".config" / _CURSOR_SUBPATH  # linux + other


def connect_ro(path) -> sqlite3.Connection | None:
    """Read-only connect, safe alongside a running Cursor (WAL allows concurrent readers)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        con.execute("PRAGMA query_only = ON")
        return con
    except sqlite3.DatabaseError:   # locked, corrupt, or not-a-database file
        return None


def _model(bubble: dict) -> str | None:
    for k in ("model", "modelType", "modelName"):
        v = bubble.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _noop() -> None:
    return None


def _ts(bubble: dict, meta: dict) -> str | None:
    """Bubble createdAt is an ISO string; composerData createdAt is epoch-ms. Handle both."""
    bts = bubble.get("createdAt")
    if isinstance(bts, str) and bts:
        return bts
    return epoch_ms_to_iso(bts or meta.get("ts"))


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    db = cursor_db(cfg)
    if db is None or not db.exists():
        return
    con = connect_ro(db)
    if con is None:
        print("aut: cursor state.vscdb exists but could not be opened (locked/corrupt); "
              "skipping cursor this run", file=sys.stderr)
        return
    try:
        cur = con.cursor()

        # composerId -> {ts_ms, model}: recovers a timestamp for bubbles that lack one.
        comp: dict[str, dict] = {}
        try:
            for key, val in cur.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ):
                try:
                    o = json.loads(val)
                    if not isinstance(o, dict):
                        continue
                except (json.JSONDecodeError, TypeError):
                    continue
                comp[key.split(":", 1)[1]] = {
                    # createdAt only (stable). lastUpdatedAt changes on every composer update,
                    # which would shift the event ts and defeat ON CONFLICT (event_id, ts).
                    "ts": o.get("createdAt"),
                    "model": _model(o),
                }
        except sqlite3.DatabaseError:
            pass

        try:
            rows = cur.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
            )
        except sqlite3.DatabaseError as exc:
            print(f"aut: cursor bubbleId query failed ({exc}); skipping cursor", file=sys.stderr)
            return

        events: list[UsageEvent] = []
        try:
            for key, val in rows:
                try:
                    o = json.loads(val)
                    if not isinstance(o, dict):
                        continue
                    tc = o.get("tokenCount")
                    if not isinstance(tc, dict):
                        tc = {}
                    inp = int(tc.get("inputTokens") or 0)
                    out = int(tc.get("outputTokens") or 0)
                except (json.JSONDecodeError, TypeError, ValueError, AttributeError, OverflowError):
                    continue
                composer_id = key.split(":")[1]
                bubble_id = o.get("bubbleId") or key.rsplit(":", 1)[-1]
                meta = comp.get(composer_id) or {}
                if o.get("type") == 1:
                    # A user bubble = a human prompt. No token count of its own, and no bubble
                    # timestamp either, so ts falls back to the composer's createdAt (COARSE:
                    # every prompt in a conversation shares one time). event_id is per-bubble.
                    events.append(UsageEvent(
                        event_id=make_event_id(NAME, "human", composer_id, bubble_id),
                        ts=_ts(o, meta), agent=NAME, model=None, provider=None, kind="human",
                    ))
                elif cfg.cursor_human_only:
                    # Human-turns-only mode: the token rows are exactly what would double-count
                    # against cursor_api, and they are the inferior copy anyway (no cache
                    # attribution, no CLI usage). Drop them and keep the prompts, which
                    # cursor_api's feed does not carry at all.
                    continue
                elif inp + out > 0:
                    uid = o.get("usageUuid") or f"{composer_id}:{bubble_id}"
                    events.append(UsageEvent(
                        event_id=make_event_id(NAME, uid),
                        ts=_ts(o, meta),
                        agent=NAME,
                        model=_model(o) or meta.get("model"),
                        provider="cursor",
                        input_tokens=inp,
                        output_tokens=out,
                        total_tokens=inp + out,
                    ))
                else:
                    continue
                if len(events) >= _BATCH:
                    yield events, _noop
                    events = []
        except sqlite3.DatabaseError as exc:   # corrupt DB can raise mid-iteration
            print(f"aut: cursor row read failed ({exc}); emitting partial data", file=sys.stderr)
        yield events, _noop
    finally:
        con.close()
