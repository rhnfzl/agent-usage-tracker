"""Rank the authorized text of one frozen evidence scope in memory."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from typing import Any

from ..config import AutError

MAX_EVENTS = 50_000
MAX_BYTES = 64 * 1024 * 1024


def rank(query: str, candidates: Iterable[dict[str, Any]], offset: int,
         limit: int) -> tuple[list[dict[str, Any]], bool, int]:
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.lower())))
    if not 1 <= len(terms) <= 32:
        raise AutError("evidence: ranked search needs 1..32 distinct words")
    expression = " OR ".join(f'"{term}"' for term in terms)
    with closing(sqlite3.connect(":memory:")) as db:
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
        rows = db.execute(
            """
            SELECT hit, snippet(events, 0, '', '', ' ... ', 26)
            FROM events WHERE events MATCH ?
            ORDER BY instr(lower(body), lower(?)) > 0 DESC, bm25(events), rowid
            LIMIT ? OFFSET ?
            """,
            (expression, query, limit + 1, offset),
        ).fetchall()
    return [{**json.loads(hit), "preview": preview[:320]} for hit, preview in rows[:limit]], (
        len(rows) > limit), count
