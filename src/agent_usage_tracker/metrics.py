"""Pure derivation of showcase metrics from sink rows + a price map. No I/O here: the caller
supplies rows (report.fetch_rows) and prices (pricing.load_prices), so this module is
deterministic and unit-tested without a database or network.

`input` throughout means FRESH (non-cached) prompt tokens. Every $ figure is NOTIONAL /
API-equivalent (subscriptions are flat-rate).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, NamedTuple

from .pricing import normalize_model_key  # noqa: F401


class Row(NamedTuple):
    ts: datetime
    agent: str
    machine: str | None
    model: str | None
    kind: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int | None
    total_tokens: int


def iter_sessions(rows: Iterable[Row], idle_gap_s: int):
    """Yield (agent, machine, first_ts) per session. A session is a run of one
    agent+machine's events with <= idle_gap_s between consecutive events
    (ordered by ts)."""
    buckets: dict[tuple, list[datetime]] = defaultdict(list)
    for r in rows:
        buckets[(r.agent, r.machine)].append(r.ts)
    for (agent, machine), times in buckets.items():
        times.sort()
        session_start: datetime | None = None
        prev: datetime | None = None
        for t in times:
            if prev is None or (t - prev).total_seconds() > idle_gap_s:
                if session_start is not None:
                    yield (agent, machine, session_start)
                session_start = t
            prev = t
        if session_start is not None:
            yield (agent, machine, session_start)
