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

from .pricing import normalize_model_key


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


def cost_of(row: Row, prices: dict[str, dict[str, float]]) -> float | None:
    """API-equivalent cost of one row, or None if the model is not priced.
    cache_read/write use their own rate when present, else fall back to the input rate.
    Reasoning bills as output."""
    p = prices.get(normalize_model_key(row.model or ""))
    if not p:
        return None
    in_rate = p.get("input_cost", 0.0)
    out_rate = p.get("output_cost", 0.0)
    return (
        row.input_tokens * in_rate
        + row.output_tokens * out_rate
        + row.cache_read_tokens * p.get("cache_read_cost", in_rate)
        + row.cache_write_tokens * p.get("cache_write_cost", in_rate)
        + (row.reasoning_tokens or 0) * out_rate
    )


def naive_cost_of(row: Row, prices: dict[str, dict[str, float]]) -> float | None:
    """What the row WOULD cost with no cache discount: cache_read repriced at the full
    input rate. sum(naive) / sum(actual) over priced rows is the cache-savings multiple."""
    p = prices.get(normalize_model_key(row.model or ""))
    if not p:
        return None
    in_rate = p.get("input_cost", 0.0)
    out_rate = p.get("output_cost", 0.0)
    return (
        row.input_tokens * in_rate
        + row.output_tokens * out_rate
        + row.cache_read_tokens * in_rate
        + row.cache_write_tokens * p.get("cache_write_cost", in_rate)
        + (row.reasoning_tokens or 0) * out_rate
    )
