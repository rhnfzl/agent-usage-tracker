"""Collector registry. Each collector exposes:

    collect(cfg, wm) -> Iterator[(events, commit)]

where each yield is (events for one source unit, a callback that advances that unit's
watermark). The runner pushes `events`, and only on a successful push calls `commit()`.
"""

from __future__ import annotations

from . import claude_code, codex, cursor, pi

REGISTRY = {
    claude_code.NAME: claude_code.collect,
    codex.NAME: codex.collect,
    cursor.NAME: cursor.collect,
    pi.NAME: pi.collect,
}

__all__ = ["REGISTRY"]
