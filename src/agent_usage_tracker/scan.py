"""The ingest runs: `aut scan`, `aut export` and `aut backfill`, each with its print_* report.

A run returns its data and leaves the final summary to its print_* helper; cli.py only parses the
command line and calls the pair.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Sequence, TextIO, TypedDict

from .collectors import REGISTRY, Collect, drain
from .config import Config
from .event import (ActivityEvent, Batch, CollectorUnavailable, Row, TimeInterval, UsageEvent,
                    parse_ts, valid_ts)
from .push import (COLUMNS, ScanStatus, deliver_scan_rows, error_type_name, make_sender,
                   redacted_error, scan_row, send_activity_rows, send_session_rows)
from .repo_keys import require_readable_salt
from .state import collector_wm, load_state, save_state

BATCH = 500


class ModelTally(TypedDict):
    events: int
    total: int


class AgentTally(TypedDict):
    """One collector's line of the scan report."""
    events: int
    human: int
    input: int
    output: int
    cache_read: int
    cache_write: int
    reasoning: int
    total: int
    skipped_no_ts: int
    duplicates: int
    by_model: dict[str, ModelTally]
    activity: dict[str, int]
    intervals: int
    time: dict[str | None, float]       # category -> seconds
    relevant_s: float


class HistoryCounts(TypedDict):
    """The report of a history backfill."""
    rows: int
    by_origin: dict[str, int]
    turn_null: int
    subagent_no_parent: int
    activity: int
    by_kind: dict[str, int]
    via: dict[str, int]
    link: dict[str, dict[str, int]]
    failed: set[str]


def _blank_agent_tally() -> AgentTally:
    return {"events": 0, "human": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "reasoning": 0, "total": 0, "skipped_no_ts": 0,
            "duplicates": 0, "by_model": {}, "activity": {},
            "intervals": 0, "time": {}, "relevant_s": 0.0}


def _tally_event(agg: AgentTally, e: Row) -> None:
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


class _CollectorPass:
    """One collector's rows on their way to the sink: tallied, buffered, sent in batches."""

    def __init__(self, agg: AgentTally, sender: Callable[[list[Row]], None] | None) -> None:
        self.agg, self.sender = agg, sender      # sender None: a dry run, nothing is sent
        # The same session can sit at two paths -- Claude Code re-encodes a project directory
        # (e.g. '-Users-me-proj' and 'prefix---Users-me-proj'), so one session file is globbed
        # twice and every turn in it is emitted twice. Both copies produce the same event_id, so
        # the sink stores one and the numbers there are right; without this set the PRINTED tally
        # counted both and overstated the scan. Per-agent, discarded when the agent finishes.
        self.seen: set[str] = set()
        self.buf: list[Row] = []
        self.advances: list[Callable[[], None]] = []
        self.newest_ts: datetime | None = None
        self.status: ScanStatus = "ok"
        self.error: str | None = None

    def drain_all(self, collect: Collect, cfg: Config, wm: dict[str, Any], name: str) -> None:
        for batches in drain(collect, cfg, wm, name):
            try:
                for batch in batches:
                    self.take(batch)
            except CollectorUnavailable as exc:     # what it could read still goes out below
                print(f"aut: {exc}", file=sys.stderr)
                self.status, self.error = "degraded", error_type_name(exc.__cause__ or exc)
            self.flush()

    def take(self, batch: Batch) -> None:
        events, advance_watermark = batch
        for e in events:
            ts = parse_ts(e.ts)
            if ts is None:
                self.agg["skipped_no_ts"] += 1
                continue
            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            self.newest_ts = ts if self.newest_ts is None or ts > self.newest_ts else self.newest_ts
            if e.event_id in self.seen:
                self.agg["duplicates"] += 1      # counted once, above
            else:
                self.seen.add(e.event_id)
                _tally_event(self.agg, e)
            # Still queued for the sink either way: deduping the PUSH would mean a copy whose batch
            # failed could not be re-sent from the other path, and ON CONFLICT already makes the
            # repeat free.
            if self.sender is not None:
                self.buf.append(e)
        self.advances.append(advance_watermark)
        if len(self.buf) >= BATCH:
            self.flush()

    def flush(self) -> None:
        if self.buf and self.sender is not None:    # a dry run fills no buf; this narrows sender
            self.sender(self.buf)           # raises on delivery failure -> advances skipped
        for advance in self.advances:       # always advance the (in-memory) watermark so the
            advance()                       # drain terminates; only scan persists it
        self.buf.clear()
        self.advances.clear()


def run_scan(cfg: Config, agents: Sequence[str], *, dry_run: bool) -> dict[str, AgentTally]:
    state = load_state()
    sender = None if dry_run else make_sender(cfg)
    if not dry_run:
        require_readable_salt(cfg.repo_key_salt)
    report: dict[str, AgentTally] = {}
    scan_rows: list[dict[str, Any]] = []      # one heartbeat row per collector that ran (agents.collector_scan)

    for name in agents:
        collector = REGISTRY[name]
        wm = collector_wm(state, name)
        agg = report.setdefault(name, _blank_agent_tally())
        run = _CollectorPass(agg, sender)
        started_at = datetime.now(timezone.utc)
        failure: BaseException | None = None

        try:
            run.drain_all(collector.collect, cfg, wm, name)
            if collector.after_scan and not dry_run:    # a dry run changes nothing
                collector.after_scan(cfg)
        except BaseException as exc:
            run.status, run.error = "error", error_type_name(exc)
            failure = exc
        if not dry_run:
            try:
                save_state(state, [name])
            except OSError as exc:      # never ahead of the heartbeat, or of the pass's own failure
                if failure:
                    print(f"aut: warning: watermark not saved: {redacted_error(exc)}", file=sys.stderr)
                else:
                    run.status, run.error = "error", error_type_name(exc)
                    failure = exc
            scan_rows.append(scan_row(cfg, name, started_at, status=run.status, error=run.error,
                                      events=agg["events"] + agg["intervals"],
                                      human=agg["human"], newest_ts=run.newest_ts))
            # the loop ends here: one delivery. Ctrl-C leaves at once and sends nothing.
            if failure and not isinstance(failure, KeyboardInterrupt):
                deliver_scan_rows(cfg, scan_rows)
        if failure:
            raise failure                   # the scan still fails

    if not dry_run:
        deliver_scan_rows(cfg, scan_rows)
    return report


def _print_tokens(name: str, agg: AgentTally) -> None:
    print(f"{name:<12} {agg['events']:>6} turns  {agg['total']:>12,} tokens "
          f"(in {agg['input']:,} / out {agg['output']:,} / "
          f"cacheR {agg['cache_read']:,} / cacheW {agg['cache_write']:,}"
          + (f" / reason {agg['reasoning']:,}" if agg['reasoning'] else "") + ")")
    for model, mv in sorted(agg["by_model"].items(), key=lambda kv: -kv[1]["total"]):
        print(f"    {model:<28} {mv['events']:>5} turns  {mv['total']:>12,} tokens")


def _print_time(name: str, agg: AgentTally) -> None:
    total_s = sum(agg["time"].values())
    print(f"{name:<12} {agg['intervals']:>6} intervals  {total_s / 60:,.1f} min active "
          f"({agg['relevant_s'] / 60:,.1f} min work-relevant)")
    for cat, secs in sorted(agg["time"].items(), key=lambda kv: -kv[1]):
        print(f"    {cat:<28} {secs / 60:>9,.1f} min")


def _print_agent(name: str, agg: AgentTally) -> None:
    if agg["events"] or agg["total"]:
        _print_tokens(name, agg)
    if agg["human"]:
        print(f"{name:<12} {agg['human']:>6} human turns (prompts you sent)")
    if agg["intervals"]:
        _print_time(name, agg)
    if agg["activity"]:
        print(f"{name:<12} activity rows {json.dumps(agg['activity'], sort_keys=True)}")
    if agg["skipped_no_ts"]:
        print(f"    (skipped {agg['skipped_no_ts']} records with missing/invalid timestamp)")
    if agg["duplicates"]:
        print(f"    (counted once: {agg['duplicates']:,} repeat records from sessions stored "
              "under more than one project path)")


def print_scan(cfg: Config, report: dict[str, AgentTally], *, dry_run: bool) -> None:
    mode = "DRY-RUN (nothing sent)" if dry_run else "scan"
    print(f"\naut {mode}  machine={cfg.machine}\n" + "=" * 60)
    grand = 0
    for name, agg in report.items():
        if not (agg["events"] or agg["human"] or agg["activity"] or agg["skipped_no_ts"]
                or agg["duplicates"] or agg["intervals"]):
            print(f"{name:<12} -  no new usage")
            continue
        grand += agg["total"]
        _print_agent(name, agg)
    print("-" * 60)
    print(f"{'TOTAL':<12} {grand:>32,} tokens")


def reset_watermarks(agents: Sequence[str]) -> None:
    """Forget the selected collectors' watermarks, so the next scan reads all their history."""
    state = load_state()
    for name in agents:
        state.get("collectors", {}).pop(name, None)
    save_state(state, agents)


def _one_shot_batches(name: str, cfg: Config, wm: dict[str, Any], failed: set[str],
                      keeps: set[str]) -> Iterator[Batch]:
    """All of a collector's batches for a one-shot command, which calls each batch's advance
    itself: an unavailable source ends that pass, and its name goes into `failed` when the collector
    can emit a kind of row in `keeps`. Otherwise the command is missing nothing."""
    for batches in drain(REGISTRY[name].collect, cfg, wm, name):
        try:
            yield from batches
        except CollectorUnavailable as exc:
            print(f"aut: {exc}", file=sys.stderr)
            if REGISTRY[name].rows & keeps:
                failed.add(name)


def run_export(cfg: Config, agents: Sequence[str], out: TextIO) -> tuple[int, int, set[str]]:
    """Write all normalized events (from scratch, ignoring the watermark) as CSV to `out`.
    Column order matches push.COLUMNS / the sink table, so it loads via COPY. Useful for
    an offline bulk backfill or a bridged load where the live transport is not wired yet.
    Returns (written, skipped, failed): skipped counts records with a missing/invalid ts, failed
    names the usage collectors with a source they could not read, so the CSV is partial."""
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    n = skipped = 0
    failed: set[str] = set()
    for name in agents:
        # Must dedup here, unlike the push path: this CSV is meant to be loaded with COPY, which
        # has no ON CONFLICT to fall back on, so a session stored under two project paths would
        # land twice in the target table. See the note in run_scan.
        seen: set[str] = set()
        for events, advance_watermark in _one_shot_batches(name, cfg, {}, failed, {"usage"}):
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
    return n, skipped, failed


def print_export(written: int, skipped: int, out_path: str | None) -> None:
    dest = f" -> {out_path}" if out_path else ""
    skip_note = f" ({skipped} skipped: missing/invalid ts)" if skipped else ""
    print(f"exported {written} events{skip_note}{dest}", file=sys.stderr)


def _tally_activity(counts: HistoryCounts, e: ActivityEvent) -> None:
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


def _tally_session(counts: HistoryCounts, row: dict[str, Any]) -> None:
    counts["rows"] += 1
    origin = row["run_origin"]
    counts["by_origin"][origin] = counts["by_origin"].get(origin, 0) + 1
    counts["turn_null"] += row["turn_id"] is None
    counts["subagent_no_parent"] += origin == "subagent" and row["parent_session_id"] is None


class _History:
    """The rows of a history backfill: tallied, buffered and sent in batches."""

    def __init__(self, dsn: str, dry_run: bool, *, sessions: bool, activity: bool) -> None:
        self.dsn, self.dry_run, self.sessions, self.activity = dsn, dry_run, sessions, activity
        self.counts: HistoryCounts = {"rows": 0, "by_origin": {}, "turn_null": 0,
                                      "subagent_no_parent": 0, "activity": 0, "by_kind": {},
                                      "via": {}, "link": {}, "failed": set()}
        self.session_buf: list[UsageEvent] = []
        self.activity_buf: list[ActivityEvent] = []

    def flush(self) -> None:
        if self.session_buf and not self.dry_run:
            send_session_rows(self.dsn, self.session_buf)   # raises -> the command fails, nothing to undo
        if self.activity_buf and not self.dry_run:
            send_activity_rows(self.dsn, self.activity_buf)
        self.session_buf.clear()
        self.activity_buf.clear()

    def take(self, e: Row, seen: set[str], name: str) -> None:
        if isinstance(e, ActivityEvent):
            if self.activity:
                self._take_activity(e, seen)
        elif isinstance(e, UsageEvent) and self.sessions:   # a time row is neither
            self._take_session(e, seen, name)

    def _take_activity(self, e: ActivityEvent, seen: set[str]) -> None:
        if valid_ts(e.ts) and e.event_id not in seen:
            seen.add(e.event_id)
            _tally_activity(self.counts, e)
            self.activity_buf.append(e)
            if len(self.activity_buf) >= BATCH:
                self.flush()

    def _take_session(self, e: UsageEvent, seen: set[str], name: str) -> None:
        row = e.session_row()
        # no row for an event the usage path drops (bad ts), or one already sent
        if row is None or not valid_ts(e.ts) or e.event_id in seen:
            return
        seen.add(e.event_id)
        _tally_session(self.counts, row)
        self.session_buf.append(e)
        if len(self.session_buf) >= BATCH:
            self.flush()
            if self.counts["rows"] % (BATCH * 20) == 0:
                print(f"aut: {name}: {self.counts['rows']:,} session rows so far", file=sys.stderr)


def run_history_backfill(cfg: Config, agents: Sequence[str], *, dry_run: bool,
                         sessions: bool = True, activity: bool = False) -> HistoryCounts:
    """Send agents.event_session rows (sessions) and/or agents.activity_event rows (activity) for
    ALL history, never usage rows: re-reading files from byte 0 re-emits (event_id, ts) pairs that
    can differ from the stored ones (see the ts note in collectors/claude_code.py), and those would
    land as new duplicates. Works on a throwaway watermark, so the live one is neither read nor
    saved. ON CONFLICT makes a re-run free."""
    dsn = "" if dry_run else cfg.require_pg("backfill --sessions-only/--activity-only without "
                                            "--dry-run")
    if not dry_run:
        require_readable_salt(cfg.repo_key_salt)
    history = _History(dsn, dry_run, sessions=sessions, activity=activity)
    keeps = {kind for kind, on in (("session", sessions), ("activity", activity)) if on}
    for name in agents:
        seen: set[str] = set()                      # same session under two paths: one row
        for events, advance_watermark in _one_shot_batches(name, cfg, {},
                                                           history.counts["failed"], keeps):
            for e in events:
                history.take(e, seen, name)
            advance_watermark()                 # advances only the throwaway wm
        history.flush()
        print(f"aut: {name}: {len(seen):,} {'session/activity' if activity else 'session'} rows "
              f"{'counted' if dry_run else 'sent'}", file=sys.stderr)
    return history.counts


def print_history_backfill(c: HistoryCounts, *, dry_run: bool, sessions: bool, activity: bool) -> None:
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
