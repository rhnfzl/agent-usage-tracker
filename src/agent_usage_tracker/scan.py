"""The ingest runs: `aut scan`, `aut export` and `aut backfill`, each with its print_* report.

A run returns its data and leaves the final summary to its print_* helper; cli.py only parses the
command line and calls the pair.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from typing import Iterator, Sequence, TextIO

from .collectors import REGISTRY, drain
from .config import Config
from .event import (ActivityEvent, Batch, CollectorUnavailable, Row, TimeInterval, UsageEvent,
                    parse_ts, valid_ts)
from .push import (COLUMNS, deliver_scan_rows, make_sender, scan_row, scrub_error,
                   send_activity_rows, send_session_rows)
from .state import collector_wm, load_state, save_state

BATCH = 500


def _blank_agent_tally() -> dict:
    return {"events": 0, "human": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "reasoning": 0, "total": 0, "skipped_no_ts": 0,
            "duplicates": 0, "by_model": {}, "activity": {},
            "intervals": 0, "time": {}, "relevant_s": 0.0}    # time: category -> seconds


def _tally_event(agg: dict, e: Row) -> None:
    if isinstance(e, TimeInterval):      # active time; no tokens, kept out of events/human
        secs = (datetime.fromisoformat(e.end_ts) - datetime.fromisoformat(e.ts)).total_seconds()
        agg["intervals"] += 1
        agg["time"][e.category] = agg["time"].get(e.category, 0.0) + secs
        agg["relevant_s"] += secs if e.work_relevant else 0.0
        return
    if isinstance(e, ActivityEvent):     # no tokens; tallied by kind, kept out of events/human
        agg["activity"][e.kind] = agg["activity"].get(e.kind, 0) + 1
        return
    if e.kind == "human":            # a user prompt: no tokens, kept out of the token/model rollup
        agg["human"] += 1
        return
    agg["events"] += 1
    agg["input"] += e.input_tokens
    agg["output"] += e.output_tokens
    agg["cache_read"] += e.cache_read_tokens
    agg["cache_write"] += e.cache_write_tokens
    agg["reasoning"] += e.reasoning_tokens or 0
    agg["total"] += e.total_tokens
    m = agg["by_model"].setdefault(e.model or "unknown", {"events": 0, "total": 0})
    m["events"] += 1
    m["total"] += e.total_tokens


def run_scan(cfg: Config, agents: Sequence[str], *, dry_run: bool) -> dict:
    state = load_state()
    sender = None if dry_run else make_sender(cfg)
    report: dict = {}
    scan_rows: list[dict] = []      # one heartbeat row per collector that ran (agents.collector_scan)

    for name in agents:
        collector = REGISTRY[name]
        wm = collector_wm(state, name)
        agg = report.setdefault(name, _blank_agent_tally())
        # The same session can sit at two paths -- Claude Code re-encodes a project directory
        # (e.g. '-Users-me-proj' and 'prefix---Users-me-proj'), so one session file is globbed
        # twice and every turn in it is emitted twice. Both copies produce the same event_id, so
        # the sink stores one and the numbers there are right; without this set the PRINTED tally
        # counted both and overstated the scan. Per-agent, discarded when the agent finishes.
        seen: set[str] = set()
        buf_events: list[Row] = []
        pending_advances: list = []
        started_at = datetime.now(timezone.utc)
        status, error, newest_ts, interrupted = "ok", None, None, False

        def flush() -> None:
            if buf_events and sender is not None:   # sender is None in dry-run -> nothing sent
                sender(buf_events)          # raises on delivery failure -> advances skipped
            for advance in pending_advances:    # always advance the (in-memory) watermark so the
                advance()                       # drain terminates; only scan persists it
            buf_events.clear()
            pending_advances.clear()

        try:
            for batches in drain(collector.collect, cfg, wm, name):
                try:
                    for events, advance_watermark in batches:
                        for e in events:
                            ts = parse_ts(e.ts)
                            if ts is None:
                                agg["skipped_no_ts"] += 1
                                continue
                            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                            newest_ts = ts if newest_ts is None or ts > newest_ts else newest_ts
                            if e.event_id in seen:
                                agg["duplicates"] += 1      # counted once, above
                            else:
                                seen.add(e.event_id)
                                _tally_event(agg, e)
                            # Still queued for the sink either way: deduping the PUSH would mean a
                            # copy whose batch failed could not be re-sent from the other path, and
                            # ON CONFLICT already makes the repeat free.
                            if not dry_run:
                                buf_events.append(e)
                        pending_advances.append(advance_watermark)
                        if len(buf_events) >= BATCH:
                            flush()
                except CollectorUnavailable as exc:     # what it could read still goes out below
                    print(f"aut: {exc}", file=sys.stderr)
                    status, error = "degraded", scrub_error(exc.__cause__ or exc)
                flush()
            if collector.after_scan and not dry_run:    # a dry run changes nothing
                collector.after_scan(cfg)
        except BaseException as exc:
            status, error = "error", scrub_error(exc)
            interrupted = isinstance(exc, KeyboardInterrupt)    # leave at once, send nothing
            raise                           # the heartbeat goes out in finally; the scan still fails
        finally:
            if not dry_run:
                save_state(state)
                scan_rows.append(scan_row(cfg, name, started_at, status, error,
                                          agg["events"] + agg["intervals"], agg["human"],
                                          newest_ts))
                if status == "error" and not interrupted:   # the loop ends here: one delivery
                    deliver_scan_rows(cfg, scan_rows)

    if not dry_run:
        deliver_scan_rows(cfg, scan_rows)
    return report


def print_scan(cfg: Config, report: dict, dry_run: bool) -> None:
    mode = "DRY-RUN (nothing sent)" if dry_run else "scan"
    print(f"\naut {mode}  machine={cfg.machine}\n" + "=" * 60)
    grand = 0
    for name, agg in report.items():
        if (agg["events"] == 0 and agg["human"] == 0 and agg["skipped_no_ts"] == 0
                and agg["duplicates"] == 0 and agg["intervals"] == 0):
            print(f"{name:<12} -  no new usage")
            continue
        grand += agg["total"]
        if agg["events"] or agg["total"]:
            print(f"{name:<12} {agg['events']:>6} turns  {agg['total']:>12,} tokens "
                  f"(in {agg['input']:,} / out {agg['output']:,} / "
                  f"cacheR {agg['cache_read']:,} / cacheW {agg['cache_write']:,}"
                  + (f" / reason {agg['reasoning']:,}" if agg['reasoning'] else "") + ")")
            for model, mv in sorted(agg["by_model"].items(), key=lambda kv: -kv[1]["total"]):
                print(f"    {model:<28} {mv['events']:>5} turns  {mv['total']:>12,} tokens")
        if agg["human"]:
            print(f"{name:<12} {agg['human']:>6} human turns (prompts you sent)")
        if agg["intervals"]:
            total_s = sum(agg["time"].values())
            print(f"{name:<12} {agg['intervals']:>6} intervals  {total_s / 60:,.1f} min active "
                  f"({agg['relevant_s'] / 60:,.1f} min work-relevant)")
            for cat, secs in sorted(agg["time"].items(), key=lambda kv: -kv[1]):
                print(f"    {cat:<28} {secs / 60:>9,.1f} min")
        if agg["activity"]:
            print(f"{name:<12} activity rows {json.dumps(agg['activity'], sort_keys=True)}")
        if agg["skipped_no_ts"]:
            print(f"    (skipped {agg['skipped_no_ts']} records with missing/invalid timestamp)")
        if agg["duplicates"]:
            print(f"    (counted once: {agg['duplicates']:,} repeat records from sessions stored "
                  "under more than one project path)")
    print("-" * 60)
    print(f"{'TOTAL':<12} {grand:>32,} tokens")


def reset_watermarks(agents: Sequence[str]) -> None:
    """Forget the selected collectors' watermarks, so the next scan reads all their history."""
    state = load_state()
    for name in agents:
        state.get("collectors", {}).pop(name, None)
    save_state(state)


def _one_shot_batches(name: str, cfg: Config, wm: dict) -> Iterator[Batch]:
    """All of a collector's batches for a one-shot command, which calls each batch's advance
    itself: an unavailable source just ends that pass."""
    for batches in drain(REGISTRY[name].collect, cfg, wm, name):
        try:
            yield from batches
        except CollectorUnavailable as exc:
            print(f"aut: {exc}", file=sys.stderr)


def run_export(cfg: Config, agents: Sequence[str], out: TextIO) -> tuple[int, int]:
    """Write all normalized events (from scratch, ignoring the watermark) as CSV to `out`.
    Column order matches push.COLUMNS / the sink table, so it loads via COPY. Useful for
    an offline bulk backfill or a bridged load where the live transport is not wired yet.
    Returns (written, skipped) where skipped counts records with a missing/invalid ts."""
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    n = skipped = 0
    for name in agents:
        # Must dedup here, unlike the push path: this CSV is meant to be loaded with COPY, which
        # has no ON CONFLICT to fall back on, so a session stored under two project paths would
        # land twice in the target table. See the note in run_scan.
        seen: set[str] = set()
        for events, advance_watermark in _one_shot_batches(name, cfg, {}):
            for e in events:
                if not isinstance(e, UsageEvent):   # the CSV is agents.usage_event only
                    continue
                if not valid_ts(e.ts):
                    skipped += 1
                    continue
                if e.event_id in seen:
                    continue
                seen.add(e.event_id)
                d = e.to_dict()
                writer.writerow(["" if d[c] is None else d[c] for c in COLUMNS])
                n += 1
            advance_watermark()           # so the next pass continues past the cap
    return n, skipped


def print_export(written: int, skipped: int, out_path: str | None) -> None:
    dest = f" -> {out_path}" if out_path else ""
    skip_note = f" ({skipped} skipped: missing/invalid ts)" if skipped else ""
    print(f"exported {written} events{skip_note}{dest}", file=sys.stderr)


def _tally_activity(counts: dict, e: ActivityEvent) -> None:
    counts["activity"] += 1
    counts["by_kind"][e.kind] = counts["by_kind"].get(e.kind, 0) + 1
    if e.kind == "steer":
        v = e.attrs["via"]
        counts["via"][v] = counts["via"].get(v, 0) + 1
    elif e.kind == "link":     # rows per role, and how many carry each key
        r = counts["link"].setdefault(e.attrs["role"], {"rows": 0})
        r["rows"] += 1
        for k, v in e.attrs.items():
            if k != "role" and v:
                r[k] = r.get(k, 0) + 1


def _tally_session(counts: dict, row: dict) -> None:
    counts["rows"] += 1
    origin = row["run_origin"]
    counts["by_origin"][origin] = counts["by_origin"].get(origin, 0) + 1
    counts["turn_null"] += row["turn_id"] is None
    counts["subagent_no_parent"] += origin == "subagent" and row["parent_session_id"] is None


def run_history_backfill(cfg: Config, agents: Sequence[str], *, dry_run: bool,
                         sessions: bool = True, activity: bool = False) -> dict:
    """Send agents.event_session rows (sessions) and/or agents.activity_event rows (activity) for
    ALL history, never usage rows: re-reading files from byte 0 re-emits (event_id, ts) pairs that
    can differ from the stored ones (see the ts note in collectors/claude_code.py), and those would
    land as new duplicates. Works on a throwaway watermark, so the live one is neither read nor
    saved. ON CONFLICT makes a re-run free."""
    dsn = "" if dry_run else cfg.require_pg("backfill --sessions-only/--activity-only without "
                                            "--dry-run")
    counts: dict = {"rows": 0, "by_origin": {}, "turn_null": 0, "subagent_no_parent": 0,
                    "activity": 0, "by_kind": {}, "via": {}, "link": {}}
    buf: list[dict] = []
    abuf: list[ActivityEvent] = []

    def flush() -> None:
        if buf and not dry_run:
            send_session_rows(dsn, buf)      # raises -> the command fails, nothing to undo
        if abuf and not dry_run:
            send_activity_rows(dsn, abuf)
        buf.clear()
        abuf.clear()

    for name in agents:
        seen: set[str] = set()                      # same session under two paths: one row
        for events, advance_watermark in _one_shot_batches(name, cfg, {}):
            for e in events:
                if isinstance(e, TimeInterval):     # neither a session nor an activity row
                    continue
                if isinstance(e, ActivityEvent):
                    if activity and valid_ts(e.ts) and e.event_id not in seen:
                        seen.add(e.event_id)
                        _tally_activity(counts, e)
                        abuf.append(e)
                        if len(abuf) >= BATCH:
                            flush()
                    continue
                if not sessions:
                    continue
                row = e.session_row()
                # no row for an event the usage path drops (bad ts), or one already sent
                if row is None or not valid_ts(e.ts) or e.event_id in seen:
                    continue
                seen.add(e.event_id)
                _tally_session(counts, row)
                buf.append(row)
                if len(buf) >= BATCH:
                    flush()
                    if counts["rows"] % (BATCH * 20) == 0:
                        print(f"aut: {name}: {counts['rows']:,} session rows so far",
                              file=sys.stderr)
            advance_watermark()                 # advances only the throwaway wm
        flush()
        print(f"aut: {name}: {len(seen):,} {'session/activity' if activity else 'session'} rows "
              f"{'counted' if dry_run else 'sent'}", file=sys.stderr)
    return counts


def print_history_backfill(c: dict, *, dry_run: bool, sessions: bool, activity: bool) -> None:
    mode = "DRY-RUN (nothing sent)" if dry_run else "sent"
    if sessions:
        print(f"sessions-only backfill {mode}: {c['rows']} rows, by run_origin "
              f"{json.dumps(c['by_origin'], sort_keys=True)}, turn_id NULL {c['turn_null']}, "
              f"subagent rows without a parent {c['subagent_no_parent']}")
    if activity:
        print(f"activity-only backfill {mode}: {c['activity']} rows, by kind "
              f"{json.dumps(c['by_kind'], sort_keys=True)}, steer via "
              f"{json.dumps(c['via'], sort_keys=True)}, link rows with each key by role "
              f"{json.dumps(c['link'], sort_keys=True)}")
