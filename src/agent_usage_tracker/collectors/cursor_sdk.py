"""Cursor-SDK collector: token usage from SDK-driven agents that persist a run manifest.

Agents built on the Cursor Agent SDK don't write the CLI/IDE logs the other collectors read
- the SDK hands per-run usage back in-process and the calling app saves it. The
research-backend-tickets-orchestrator is the reference case: it writes a per-ticket
`manifest.json` under `<root>/.state/runs/<TICKET>/` whose `runs[]` each carry
`{input_tokens, output_tokens, total_tokens, cache_read_tokens}` (real shape; `total` already
sums input+output+cache_read) plus the `model` and `phase`.

Point config `cursor_sdk_manifests` at a glob of those manifest files (e.g.
`~/proj/**/.state/runs/*/manifest.json`); unset -> this collector no-ops. Like the `cursor`
collector we FULL-SCAN every manifest each run and dedup at the sink via `event_id` +
ON CONFLICT: the manifest file is small and rewritten in place, but a run's `usage` is written
once at finish and never changes, so re-emitting the same run is idempotent.

LOWER BOUND: the orchestrator deletes a ticket's transcripts when its PR merges, so a ticket
finished-and-merged between two scans is invisible. Runs are scoped to the manifest schema,
so unrelated JSON matched by the glob is ignored (no `runs` list -> skipped).
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter
from typing import Any, Callable, Iterator

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import noop_advance, project_name, raise_if_unreadable, safe_int, warn_skipped

NAME = "cursor_sdk"


def _run_event(r: object, pos: int, ticket: object, project: str | None) -> UsageEvent | None:
    """One entry of runs[] as a usage row; None when it has no usage."""
    if not isinstance(r, dict) or not isinstance(r.get("usage"), dict):
        return None
    u = r["usage"]
    inp = safe_int(u.get("input_tokens"))
    out = safe_int(u.get("output_tokens"))
    cr = safe_int(u.get("cache_read_tokens"))
    cw = safe_int(u.get("cache_write_tokens"))
    total = safe_int(u.get("total_tokens")) or (inp + out + cr + cw)
    if total == 0:
        return None
    # runs[] is append-only (never reordered/removed mid-list), so `pos` is a stable,
    # unique per-manifest run id even when the optional `index`/`run_id` fields are absent.
    idx = r.get("index")
    run_disc = str(pos if idx is None else idx)
    model = r.get("model")
    return UsageEvent(
        event_id=make_event_id(NAME, str(ticket), run_disc, str(r.get("run_id") or "")),
        ts=r.get("finished_at") or r.get("started_at"),
        agent=NAME,
        model=model if isinstance(model, str) else None,    # a list/dict is unhashable downstream
        provider="cursor",
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        total_tokens=total,
        project=project,
    )


def collect(cfg: Config, wm: dict[str, Any]) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    pattern = cfg.cursor_sdk_manifests
    if not pattern:
        return
    pattern = os.path.expanduser(pattern)   # glob does not expand ~; the documented form uses it
    events: list[UsageEvent] = []
    skipped: Counter[str] = Counter()
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path, encoding="utf-8") as fh:
                m = json.load(fh)
        except FileNotFoundError:           # gone since the glob
            continue
        except OSError:
            skipped["unreadable"] += 1      # a later scan re-reads it: this is a full scan
            continue
        except ValueError:                  # JSONDecodeError and UnicodeDecodeError
            skipped["malformed"] += 1
            continue
        if not isinstance(m, dict) or not isinstance(m.get("runs"), list):
            continue
        ticket = m.get("ticket") or path
        project = project_name(m.get("project"))   # basename-only (never a full path) per CONTRACT.md
        rows = (_run_event(r, pos, ticket, project) for pos, r in enumerate(m["runs"]))
        events += [e for e in rows if e]
    yield events, noop_advance
    warn_skipped(NAME, skipped)
    raise_if_unreadable(NAME, skipped)
