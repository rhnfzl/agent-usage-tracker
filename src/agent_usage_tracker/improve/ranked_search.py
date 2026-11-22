"""Rank the authorized text of one frozen evidence scope in memory."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Lock
from typing import Any
from weakref import finalize

from ..config import AutError

MAX_EVENTS = 50_000
MAX_BYTES = 64 * 1024 * 1024
_FileStamp = tuple[Path, int, int, int, int, int, int]


def _file_stamp(path: Path) -> _FileStamp | None:
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (resolved, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            stat.st_ctime_ns, stat.st_mode)


class _Index:
    def __init__(self, scope: str, candidates: Callable[
        [Callable[[Path], None]], Iterable[dict[str, Any]]
    ]) -> None:
        self.scope = scope
        self.files: dict[Path, _FileStamp | None] = {}
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.close = finalize(self, self.db.close)
        try:
            self.count = self._load(candidates(self._observe))
        except BaseException:
            self.close()
            raise

    def _observe(self, path: Path) -> None:
        self.files[path] = _file_stamp(path)

    def current(self, scope: str) -> bool:
        try:
            return self.scope == scope and all(
                _file_stamp(path) == stamp for path, stamp in self.files.items()
            )
        except OSError:
            return False

    def _load(self, candidates: Iterable[dict[str, Any]]) -> int:
        db = self.db
        db.execute("PRAGMA temp_store = MEMORY")
        try:
            db.execute("CREATE VIRTUAL TABLE events USING fts5(body, hit UNINDEXED)")
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc):
                raise
            raise AutError("evidence: SQLite FTS5 is unavailable; use exact search") from exc
        count = size = 0
        with db:
            for candidate in candidates:
                body = candidate.pop("text")
                hit = json.dumps(candidate, ensure_ascii=False)
                count += 1
                size += len(body.encode("utf-8")) + len(hit.encode("utf-8"))
                if count > MAX_EVENTS or size > MAX_BYTES:
                    raise AutError("evidence: ranked scope exceeds 50,000 events or 64 MiB; narrow "
                                   "the filters or use exact search")
                db.execute("INSERT INTO events VALUES (?, ?)", (body, hit))
        return count

    def page(self, expression: str, query: str, offset: int,
             limit: int) -> tuple[list[dict[str, Any]], bool, int]:
        rows = self.db.execute(
            """
            SELECT hit, snippet(events, 0, '', '', ' ... ', 26)
            FROM events WHERE events MATCH ?
            ORDER BY instr(lower(body), lower(?)) > 0 DESC, bm25(events), rowid
            LIMIT ? OFFSET ?
            """,
            (expression, query, limit + 1, offset),
        ).fetchall()
        return [{**json.loads(hit), "preview": preview[:320]} for hit, preview in rows[:limit]], (
            len(rows) > limit), self.count


class RankedSearch:
    """Reuse one authorized index and serialize access across MCP worker threads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._index: _Index | None = None

    def search(self, scope: str, query: str, candidates: Callable[
        [Callable[[Path], None]], Iterable[dict[str, Any]]
    ], offset: int, limit: int) -> tuple[list[dict[str, Any]], bool, int]:
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.lower())))
        if not 1 <= len(terms) <= 32:
            raise AutError("evidence: ranked search needs 1..32 distinct words")
        expression = " OR ".join(f'"{term}"' for term in terms)
        with self._lock:
            if self._index is not None and not self._index.current(scope):
                self._index.close()
                self._index = None
            if self._index is None:
                self._index = _Index(scope, candidates)
            return self._index.page(expression, query, offset, limit)
