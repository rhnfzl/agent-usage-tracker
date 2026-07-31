"""Read side: pull safe aggregate rows from the sink and write metrics.json.

I/O only — the math lives in metrics.compute_metrics. The SELECT deliberately omits `project`,
so a project name can never reach the output (privacy by construction).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .metrics import Row, compute_metrics
from .pricing import load_prices

_SELECT = """
    SELECT event_id, ts, agent, machine, model, kind,
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
           reasoning_tokens, total_tokens
    FROM agents.usage_event
    ORDER BY agent, machine, ts
"""


def dedup_by_event(records: Iterable[Sequence]) -> list[Row]:
    """Collapse to one Row per event_id, keeping the largest total_tokens. Each input record is
    (event_id, *Row fields).

    The sink's primary key is (event_id, ts) and cannot be event_id alone: it is a TimescaleDB
    hypertable, so the partitioning column must appear in the unique index. That leaves room for
    one message to land twice under different timestamps, and Claude Code does log the same
    message more than once -- sometimes re-emitting it a thousand-plus lines later in the same
    session file, sometimes with its lines minutes apart. A scan that sees only part of that
    spread takes the timestamp from the lines it happened to read, so a later scan can insert a
    second row with the same event_id, an earlier timestamp, and a partial token total.

    No rule the collector could apply per-scan fixes this, because which lines a scan sees is
    exactly what varies. Nor can the collector clean up: its role holds INSERT and SELECT only,
    deliberately. So the read side settles it. Per-message token counts are cumulative, so the
    largest total for an event_id is the complete one and any smaller row is a prefix of it."""
    best: dict[str, Row] = {}
    for rec in records:
        event_id, row = rec[0], Row(*rec[1:])
        prev = best.get(event_id)
        if prev is None or row.total_tokens > prev.total_tokens:
            best[event_id] = row
    return list(best.values())


def fetch_rows(dsn: str) -> list[Row]:
    """Read all usage rows from the sink, one per event_id. `project` is never selected.

    ponytail: pulls the whole table into memory. Fine for a personal dataset; if it ever
    outgrows RAM, push the monthly bucketing into SQL. Not needed now.
    """
    try:
        import psycopg  # optional dependency; install with the [postgres] extra
    except ImportError as exc:
        raise SystemExit("aut report needs the [postgres] extra: "
                         "uv tool install 'agent-usage-tracker[postgres]'") from exc
    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT)
            return dedup_by_event(cur.fetchall())


def run_report(dsn: str | None, prices_path: str | Path, out: str | Path, *,
               idle_gap_s: int = 1800, now: datetime | None = None,
               fetch: Callable[[str], list[Row]] | None = None) -> dict:
    """Fetch rows -> compute -> atomic-write metrics.json. Returns the metrics dict."""
    if not dsn:
        raise SystemExit("aut report needs a read DSN: set [transport].pg_dsn in your config "
                         "(it doubles as the report's read source).")
    fetch = fetch or fetch_rows
    rows = fetch(dsn)
    metrics = compute_metrics(rows, load_prices(prices_path), idle_gap_s=idle_gap_s,
                              now=now or datetime.now(timezone.utc))
    path = Path(out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return metrics
