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
model-attributed, timestamped data, use the `cursor_api` collector, with `cursor_human_only` here
to keep the prompts without double-counting the tokens.

Known limitation: the first NONZERO tokenCount seen for a bubble wins. If Cursor later
rewrites that bubble to a different nonzero count, the sink's ON CONFLICT DO NOTHING keeps
the original (a 0 -> nonzero fill-in is fine, since zero rows are skipped). Rare in practice.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import CollectorUnavailable, UsageEvent, make_event_id
from .activity_common import human_event
from ._util import (MALFORMED_RECORD, count_field, dict_or_empty, epoch_ms_to_iso, noop_advance,
                    warn_skipped)

NAME = "cursor"
# Cursor stores state.vscdb in the OS-specific app-data dir. Subpath is the same everywhere.
_CURSOR_SUBPATH = "Cursor/User/globalStorage/state.vscdb"
_BATCH = 500


def cursor_db(cfg: Config) -> Path:
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


def connect_ro(path: object) -> sqlite3.Connection | None:
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


def _ts(bubble: dict, meta: dict) -> str | None:
    """Bubble createdAt is an ISO string; composerData createdAt is epoch-ms. Handle both."""
    bts = bubble.get("createdAt")
    if isinstance(bts, str) and bts:
        return bts
    return epoch_ms_to_iso(bts or meta.get("ts"))


def _composers(cur: sqlite3.Cursor, skipped: Counter) -> tuple[dict[str, dict],
                                                              sqlite3.DatabaseError | None]:
    """composerId -> {ts, model}: recovers a timestamp for bubbles that lack one. Also the read
    failure, if any: the bubbles still go out, and some lose their ts or model."""
    comp: dict[str, dict] = {}
    try:
        for key, val in cur.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            try:
                o = json.loads(val)
            except (ValueError, TypeError):
                o = None
            if not isinstance(o, dict):
                skipped["malformed"] += 1
                continue
            comp[key.split(":", 1)[1]] = {
                # createdAt only (stable). lastUpdatedAt changes on every composer update,
                # which would shift the event ts and defeat ON CONFLICT (event_id, ts).
                "ts": o.get("createdAt"),
                "model": _model(o),
            }
    except sqlite3.DatabaseError as exc:
        return comp, exc
    return comp, None


def _bubble_event(key: str, val: Any, comp: dict[str, dict], human_only: bool,
                  skipped: Counter) -> UsageEvent | None:
    """The row of one bubble; None when it has none."""
    try:
        o = json.loads(val)
        if not isinstance(o, dict):
            skipped["malformed"] += 1
            return None
        tc = dict_or_empty(o.get("tokenCount"))
        inp = count_field(tc, "inputTokens")
        out = count_field(tc, "outputTokens")
    except MALFORMED_RECORD:
        skipped["malformed"] += 1
        return None
    composer_id = key.split(":")[1]
    bubble_id = o.get("bubbleId") or key.rsplit(":", 1)[-1]
    meta = comp.get(composer_id) or {}
    if o.get("type") == 1:
        # A user bubble = a human prompt. No token count of its own, and no bubble timestamp
        # either, so ts falls back to the composer's createdAt (COARSE: every prompt in a
        # conversation shares one time). event_id is per-bubble.
        return human_event(NAME, (composer_id, bubble_id), _ts(o, meta))
    # Human-turns-only mode: the token rows are exactly what would double-count against
    # cursor_api, and they are the inferior copy anyway (no cache attribution, no CLI usage). Drop
    # them and keep the prompts, which cursor_api's feed does not carry at all.
    if human_only or inp + out <= 0:
        return None
    uid = o.get("usageUuid") or f"{composer_id}:{bubble_id}"
    return UsageEvent(
        event_id=make_event_id(NAME, uid),
        ts=_ts(o, meta),
        agent=NAME,
        model=_model(o) or meta.get("model"),
        provider="cursor",
        input_tokens=inp,
        output_tokens=out,
        total_tokens=inp + out,
    )


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    db = cursor_db(cfg)
    if not db.exists():
        return
    con = connect_ro(db)
    if con is None:
        raise CollectorUnavailable("cursor state.vscdb exists but could not be opened "
                                   "(locked/corrupt); skipping cursor this run")
    try:
        cur = con.cursor()
        skipped: Counter = Counter()
        comp, read_failure = _composers(cur, skipped)
        try:
            rows = cur.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
            )
        except sqlite3.DatabaseError as exc:
            raise CollectorUnavailable(f"cursor bubbleId query failed ({exc}); skipping cursor") \
                from exc

        events: list[UsageEvent] = []
        try:
            for key, val in rows:
                event = _bubble_event(key, val, comp, cfg.cursor_human_only, skipped)
                if event is None:
                    continue
                events.append(event)
                if len(events) >= _BATCH:
                    yield events, noop_advance
                    events = []
        except sqlite3.DatabaseError as exc:   # corrupt DB can raise mid-iteration
            read_failure = exc
        yield events, noop_advance
        warn_skipped(NAME, skipped)
        if read_failure:
            raise CollectorUnavailable(f"cursor row read failed ({read_failure}); emitted partial "
                                       "data") from read_failure
    finally:
        con.close()
