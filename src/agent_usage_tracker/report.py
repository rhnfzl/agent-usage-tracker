"""Read side: pull safe aggregate rows from the sink and write metrics.json.

I/O only — the math lives in metrics.compute_metrics. The SELECT deliberately omits `project`,
so a project name can never reach the output (privacy by construction).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .metrics import Row, compute_metrics
from .pricing import load_prices

_SELECT = """
    SELECT ts, agent, machine, model, kind,
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
           reasoning_tokens, total_tokens
    FROM agents.usage_event
    ORDER BY agent, machine, ts
"""


def fetch_rows(dsn: str) -> list[Row]:
    """Read all usage rows from the sink. `project` is never selected.

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
            return [Row(*r) for r in cur.fetchall()]


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
