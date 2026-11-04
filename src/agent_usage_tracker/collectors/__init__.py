"""Collector registry. Each collector exposes:

    collect(cfg, wm) -> Iterator[(events, advance_watermark)]

where each yield is (events for one source unit, a callback that advances that unit's
watermark). The runner pushes `events`, and only on a successful push calls the callback.
"""

from __future__ import annotations

from typing import Callable, Iterator

from ..config import Config
from ..event import Batch
from . import activitywatch, claude_code, codex, cursor, cursor_api, cursor_cli, cursor_sdk, pi

Collector = Callable[[Config, dict], Iterator[Batch]]

REGISTRY: dict[str, Collector] = {
    claude_code.NAME: claude_code.collect,
    codex.NAME: codex.collect,
    cursor.NAME: cursor.collect,
    cursor_sdk.NAME: cursor_sdk.collect,
    cursor_api.NAME: cursor_api.collect,
    cursor_cli.NAME: cursor_cli.collect,
    pi.NAME: pi.collect,
    activitywatch.NAME: activitywatch.collect,
}

__all__ = ["REGISTRY"]
