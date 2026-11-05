"""Collector registry. Each collector exposes:

    collect(cfg, wm) -> Iterator[(events, advance_watermark)]

where each yield is (events for one source unit, a callback that advances that unit's
watermark). The runner pushes `events`, and only on a successful push calls the callback.
`wm` belongs to the collector: only its `describe` reads the keys, for `aut status`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import Batch
from . import activitywatch, claude_code, codex, cursor, cursor_api, cursor_cli, cursor_sdk, pi
from ._util import describe_files, describe_full_scan

# Each collect() pass reads at most _MAX_LINES (50k) new lines per file (see collectors/_util.py),
# so a consumer drains by re-running collect() until the watermark stops advancing. Bound the loop
# far above any real file so a misbehaving collector can't spin forever (50k lines/pass * this =
# an absurd ceiling, never hit in practice).
MAX_DRAIN_PASSES = 100_000

Collect = Callable[[Config, dict[str, Any]], Iterator[Batch]]
_USAGE_ONLY = frozenset({"usage"})      # no session or activity rows, see CONTRACT.md


@dataclass(frozen=True)
class Collector:
    collect: Collect
    describe: Callable[[Config, dict[str, Any]], str] = describe_files    # one line for `aut status`
    after_scan: Callable[[Config], object] | None = None        # never run by a dry run
    # What it can emit: a one-shot command is partial only if a collector it keeps rows from failed.
    rows: frozenset[str] = frozenset({"usage", "session", "activity"})


REGISTRY: dict[str, Collector] = {
    claude_code.NAME: Collector(claude_code.collect),
    codex.NAME: Collector(codex.collect),
    cursor.NAME: Collector(cursor.collect, describe_full_scan, rows=_USAGE_ONLY),
    cursor_sdk.NAME: Collector(cursor_sdk.collect, describe_full_scan, rows=_USAGE_ONLY),
    cursor_api.NAME: Collector(cursor_api.collect, cursor_api.describe, rows=_USAGE_ONLY),
    cursor_cli.NAME: Collector(cursor_cli.collect, cursor_cli.describe),
    pi.NAME: Collector(pi.collect),
    activitywatch.NAME: Collector(activitywatch.collect, activitywatch.describe,
                                  activitywatch.purge, rows=frozenset({"time"})),
}


def _stamped(collect: Collect, cfg: Config, wm: dict[str, Any]) -> Iterator[Batch]:
    for events, advance_watermark in collect(cfg, wm):
        for e in events:
            e.machine = cfg.machine
        yield events, advance_watermark


def drain(collect: Collect, cfg: Config, wm: dict[str, Any], name: str) -> Iterator[Iterator[Batch]]:
    """One iterator of batches per pass of collect() over wm, each row stamped with this machine,
    until a pass leaves wm unchanged. Full-scan collectors (cursor, cursor_sdk) never mutate wm, so
    they settle after one pass. The consumer applies a pass's advances before asking for the next;
    what it does with a row (validate, dedup, send) is its own."""
    for _ in range(MAX_DRAIN_PASSES):
        before = json.dumps(wm, sort_keys=True, default=str)
        yield _stamped(collect, cfg, wm)
        if json.dumps(wm, sort_keys=True, default=str) == before:
            return
    print(f"aut: warning: {name} hit the drain-pass cap ({MAX_DRAIN_PASSES}), so more rows remain "
          "unread: a scan continues from here next run; export and backfill would restart.",
          file=sys.stderr)


__all__ = ["REGISTRY", "Collect", "Collector", "drain"]
