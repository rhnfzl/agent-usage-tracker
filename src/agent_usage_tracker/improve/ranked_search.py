"""Rank the authorized text of one frozen evidence scope in memory."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Lock
from typing import Any, Literal, get_args
from weakref import finalize

from ..config import AutError
from .semantic_search import Embedder, SemanticIndex, SemanticUnavailable

SearchMode = Literal["auto", "exact", "ranked", "sessions", "semantic", "hybrid"]
SEARCH_MODES: tuple[SearchMode, ...] = get_args(SearchMode)
DEFAULT_SEARCH_MODE: SearchMode = "auto"

MAX_EVENTS = 50_000
MAX_BYTES = 64 * 1024 * 1024
_ORDER = "ORDER BY instr(lower(body), lower(?)) > 0 DESC, bm25(events), rowid"


class WordSearchUnavailable(AutError):
    """The authorized scope cannot use the bounded FTS index."""


def words(query: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[^\W_]+", query.lower())))


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
        self.semantic: SemanticIndex | None = None
        self.semantic_failure: str | None = None
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
            raise WordSearchUnavailable("evidence: SQLite FTS5 is unavailable; use exact search") from exc
        count = size = 0
        with db:
            for candidate in candidates:
                body = candidate.pop("text")
                hit = json.dumps(candidate, ensure_ascii=False)
                count += 1
                size += len(body.encode("utf-8")) + len(hit.encode("utf-8"))
                if count > MAX_EVENTS or size > MAX_BYTES:
                    raise WordSearchUnavailable("evidence: ranked scope exceeds 50,000 events or 64 MiB; narrow "
                                   "the filters or use exact search")
                db.execute("INSERT INTO events VALUES (?, ?)", (body, hit))
        return count

    def page(self, expression: str, query: str, offset: int,
             limit: int) -> tuple[list[dict[str, Any]], bool, int]:
        rows = self.db.execute(
            f"""
            SELECT hit, snippet(events, 0, '', '', ' ... ', 26)
            FROM events WHERE events MATCH ?
            {_ORDER}
            LIMIT ? OFFSET ?
            """,
            (expression, query, limit + 1, offset),
        ).fetchall()
        return [{**json.loads(hit), "preview": preview[:320]} for hit, preview in rows[:limit]], (
            len(rows) > limit), self.count

    def expanded_page(self, expression: str, query: str, offset: int, limit: int, *,
                      by_session: bool, mode: SearchMode,
                      embedder: Embedder | None, prefer_phrase: bool = False) -> tuple[list[dict[str, Any]], bool, int]:
        lexical: dict[int, int] = {}
        phrases: set[int] = set()
        if mode != "semantic":
            rows = self.db.execute(
                f"SELECT rowid, instr(lower(body), lower(?)) > 0 FROM events "
                f"WHERE events MATCH ? {_ORDER}", (query, expression, query),
            )
            for rank, (row_id, phrase) in enumerate(rows, 1):
                lexical[row_id] = rank
                if prefer_phrase and phrase:
                    phrases.add(row_id)
        semantic: dict[int, str] = {}
        if embedder is not None:
            if self.semantic is None:
                rows = self.db.execute("SELECT rowid, body FROM events ORDER BY rowid")
                self.semantic = embedder.prepare(rows)
            semantic = self.semantic.rank(query)

        semantic_ranks = {row_id: rank for rank, row_id in enumerate(semantic, 1)}
        order = sorted(set(lexical) | set(semantic_ranks), key=lambda row_id: (
            row_id not in phrases,
            -sum(1 / (60 + ranks[row_id]) for ranks in (lexical, semantic_ranks) if row_id in ranks),
            row_id,
        ))
        counts: dict[str, int] = {}
        if by_session:
            sessions = dict(self.db.execute(
                "SELECT rowid, json_extract(hit, '$.session_id') FROM events"))
            best: dict[str, int] = {}
            for row_id in order:
                key = sessions[row_id]
                best.setdefault(key, row_id)
                counts[key] = counts.get(key, 0) + 1
            order = list(best.values())

        items = []
        for row_id in order[offset:offset + limit]:
            if row_id in lexical:
                hit, preview = self.db.execute(
                    "SELECT hit, snippet(events, 0, '', '', ' ... ', 26) FROM events "
                    "WHERE events MATCH ? AND rowid = ?", (expression, row_id),
                ).fetchone()
            else:
                hit, = self.db.execute("SELECT hit FROM events WHERE rowid = ?", (row_id,)).fetchone()
                preview = semantic[row_id]
            item = {**json.loads(hit), "preview": preview[:320]}
            if embedder is not None:
                item.update(word_rank=lexical.get(row_id), semantic_rank=semantic_ranks.get(row_id))
            if by_session:
                item["matching_events"] = counts[item["session_id"]]
            items.append(item)
        return items, offset + limit < len(order), self.count


class RankedSearch:
    """Reuse one authorized index and serialize access across MCP worker threads."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = embedder
        self.model_id = embedder.model_id if embedder is not None else None
        self._lock = Lock()
        self._index: _Index | None = None

    def search(
        self, scope: str, query: str,
        candidates: Callable[[Callable[[Path], None]], Iterable[dict[str, Any]]],
        offset: int, limit: int, *, mode: SearchMode = "ranked", by_session: bool = False,
        prefer_phrase: bool = False,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        embedder = self._embedder if mode in ("semantic", "hybrid") else None
        if embedder is None and mode in ("semantic", "hybrid"):
            raise AutError("evidence: configure a local --embedding-model for semantic or hybrid search")
        terms = words(query)
        if not 1 <= len(terms) <= 32:
            raise AutError("evidence: ranked search needs 1..32 distinct words")
        expression = " OR ".join(f'"{term}"' for term in terms)
        with self._lock:
            if self._index is not None and not self._index.current(scope):
                self._index.close()
                self._index = None
            if self._index is None:
                self._index = _Index(scope, candidates)
            if embedder is not None:
                if self._index.semantic_failure:
                    raise SemanticUnavailable(self._index.semantic_failure)
                try:
                    return self._index.expanded_page(
                        expression, query, offset, limit, by_session=by_session, mode=mode,
                        embedder=embedder, prefer_phrase=prefer_phrase)
                except SemanticUnavailable as exc:
                    self._index.semantic_failure = str(exc)
                    raise
            if by_session:
                return self._index.expanded_page(
                    expression, query, offset, limit, by_session=by_session, mode=mode, embedder=embedder)
            return self._index.page(expression, query, offset, limit)
