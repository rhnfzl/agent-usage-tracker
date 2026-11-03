"""aut CLI: scan | backfill | status.

  aut scan              incremental: read new usage since the watermark, push, advance
  aut scan --dry-run    parse + normalize + AGGREGATE, print a report, send/commit nothing
  aut backfill          reset the watermark for the selected agents, then scan everything
  aut backfill --sessions-only [--dry-run]
                        re-read all history and send ONLY session rows; watermark untouched
  aut backfill --activity-only [--dry-run]
                        the same for activity rows (steers, commits, turns, ...); never usage
  aut status            show config + watermark state
  aut time add --start <local> --end <local> | --minutes N [--at <local end>] [--not-relevant]
                        record a manual active-time interval (needs transport.pg_dsn)

The dry-run report doubles as the accuracy check: it prints per-agent, per-model token
sums straight from your real logs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from . import __version__, pricing
from .collectors import REGISTRY, activitywatch
from .config import Config, load_config, _config_path
from .event import ActivityEvent, TimeInterval, UsageEvent, make_event_id, valid_ts
from .push import (_insert_pg, make_sender, scrub_error, send_activity_rows, send_scan_rows,
                   send_session_rows)
from .state import collector_wm, load_state, save_state

BATCH = 500
# Each collect() pass reads at most _MAX_LINES (50k) new lines per file (see collectors/_util.py).
# One-shot commands (export/backfill) and each scan drain by re-running collect() until the
# watermark stops advancing. Bound the loop far above any real file so a misbehaving collector
# can't spin forever (50k lines/pass * this = an absurd ceiling, never hit in practice).
_MAX_DRAIN_PASSES = 100_000


def _wm_fp(wm: dict) -> str:
    """Stable fingerprint of a collector's watermark; unchanged across a pass == fully drained.
    Full-scan collectors (cursor, cursor_sdk) never mutate wm, so they settle after one pass."""
    return json.dumps(wm, sort_keys=True, default=str)


def _blank_agg() -> dict:
    return {"events": 0, "human": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "reasoning": 0, "total": 0, "skipped_no_ts": 0,
            "duplicates": 0, "by_model": {}, "activity": {},
            "intervals": 0, "time": {}, "relevant_s": 0.0}    # time: category -> seconds


def _add(agg: dict, e: UsageEvent | ActivityEvent | TimeInterval) -> None:
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


def _deliver_scan_rows(cfg: Config, rows: list[dict]) -> None:
    """The heartbeat is best effort: whatever goes wrong here must not touch the usage scan."""
    try:
        send_scan_rows(cfg, rows)
    except Exception as exc:
        print(f"aut: warning: scan heartbeat not delivered: {scrub_error(exc)}", file=sys.stderr)


def run_scan(cfg: Config, agents: list[str], dry_run: bool) -> dict:
    state = load_state()
    sender = None if dry_run else make_sender(cfg)
    report: dict = {}
    scan_rows: list[dict] = []      # one heartbeat row per collector that ran (agents.collector_scan)

    for name in agents:
        collect = REGISTRY[name]
        wm = collector_wm(state, name)
        agg = report.setdefault(name, _blank_agg())
        # The same session can sit at two paths -- Claude Code re-encodes a project directory
        # (e.g. '-Users-me-proj' and 'prefix---Users-me-proj'), so one session file is globbed
        # twice and every turn in it is emitted twice. Both copies produce the same event_id, so
        # the sink stores one and the numbers there are right; without this set the PRINTED tally
        # counted both and overstated the scan. Per-agent, discarded when the agent finishes.
        seen: set[str] = set()
        buf_events: list[UsageEvent] = []
        buf_commits: list = []
        started_at = datetime.now(timezone.utc)
        status, error, newest_ts, interrupted = "ok", None, None, False

        def flush() -> None:
            if buf_events and sender is not None:   # sender is None in dry-run -> nothing sent
                sender(buf_events)          # raises on delivery failure -> commits skipped
            for commit in buf_commits:      # always advance the (in-memory) watermark so the
                commit()                    # drain loop below terminates; only scan persists it
            buf_events.clear()
            buf_commits.clear()

        try:
            for _ in range(_MAX_DRAIN_PASSES):
                before = _wm_fp(wm)
                for events, commit in collect(cfg, wm):
                    for e in events:
                        e.machine = cfg.machine
                        if not valid_ts(e.ts):
                            agg["skipped_no_ts"] += 1
                            continue
                        try:                # heartbeat bookkeeping must never stop the scan
                            ts = datetime.fromisoformat(e.ts.replace("Z", "+00:00"))
                            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                            newest_ts = ts if newest_ts is None or ts > newest_ts else newest_ts
                        except Exception:
                            pass
                        if e.event_id in seen:
                            agg["duplicates"] += 1      # counted once, above
                        else:
                            seen.add(e.event_id)
                            _add(agg, e)
                        # Still queued for the sink either way: deduping the PUSH would mean a
                        # copy whose batch failed could not be re-sent from the other path, and
                        # ON CONFLICT already makes the repeat free.
                        if not dry_run:
                            buf_events.append(e)
                    buf_commits.append(commit)
                    if len(buf_events) >= BATCH:
                        flush()
                flush()
                if _wm_fp(wm) == before:    # a whole pass advanced nothing -> fully drained
                    break
            else:                           # loop exhausted without draining (astronomically rare)
                print(f"aut: warning: {name} hit the drain-pass cap ({_MAX_DRAIN_PASSES}); more "
                      "usage remains and will be picked up on the next scan.", file=sys.stderr)
            if name == activitywatch.NAME and not dry_run:     # a dry run changes nothing
                activitywatch.purge(cfg)                       # never raises
        except BaseException as exc:
            status, error = "error", scrub_error(exc)
            interrupted = not isinstance(exc, Exception)    # Ctrl-C: leave at once, send nothing
            raise                           # unchanged: the scan still fails as before
        finally:
            if not dry_run:
                save_state(state)
                scan_rows.append({
                    "machine": cfg.machine, "collector": name, "started_at": started_at,
                    "finished_at": datetime.now(timezone.utc), "status": status,
                    "events": agg["events"] + agg["intervals"], "human": agg["human"],
                    "newest_ts": newest_ts,
                    "error": error, "aut_version": __version__,
                })
                if status == "error" and not interrupted:   # the loop ends here: one delivery
                    _deliver_scan_rows(cfg, scan_rows)

    if not dry_run:
        _deliver_scan_rows(cfg, scan_rows)
    return report


def _print_report(cfg: Config, report: dict, dry_run: bool) -> None:
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


def run_export(cfg: Config, agents: list[str], out) -> tuple[int, int]:
    """Write all normalized events (from scratch, ignoring the watermark) as CSV to `out`.
    Column order matches push.COLUMNS / the sink table, so it loads via COPY. Useful for
    an offline bulk backfill or a bridged load where the live transport is not wired yet.
    Returns (written, skipped) where skipped counts records with a missing/invalid ts."""
    import csv

    from .push import COLUMNS

    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    n = skipped = 0
    for name in agents:
        wm: dict = {}
        # Must dedup here, unlike the push path: this CSV is meant to be loaded with COPY, which
        # has no ON CONFLICT to fall back on, so a session stored under two project paths would
        # land twice in the target table. See the note in run_scan.
        seen: set[str] = set()
        for _ in range(_MAX_DRAIN_PASSES):    # drain past the per-file line cap (one-shot command)
            before = _wm_fp(wm)
            for events, commit in REGISTRY[name](cfg, wm):
                for e in events:
                    if not isinstance(e, UsageEvent):   # the CSV is agents.usage_event only
                        continue
                    e.machine = cfg.machine
                    if not valid_ts(e.ts):
                        skipped += 1
                        continue
                    if e.event_id in seen:
                        continue
                    seen.add(e.event_id)
                    d = e.to_dict()
                    writer.writerow(["" if d[c] is None else d[c] for c in COLUMNS])
                    n += 1
                commit()                      # advance wm so the next pass continues past the cap
            if _wm_fp(wm) == before:
                break
    return n, skipped


def run_session_backfill(cfg: Config, agents: list[str], dry_run: bool, sessions: bool = True,
                         activity: bool = False) -> dict:
    """Send agents.event_session rows (sessions) and/or agents.activity_event rows (activity) for
    ALL history, never usage rows: re-reading files from byte 0 re-emits (event_id, ts) pairs that
    can differ from the stored ones (see the ts note in collectors/claude_code.py), and those would
    land as new duplicates. Works on a throwaway watermark, so the live one is neither read nor
    saved. ON CONFLICT makes a re-run free."""
    if not dry_run and not cfg.has_pg:
        raise SystemExit("backfill --sessions-only/--activity-only needs transport.pg_dsn "
                         "(or use --dry-run).")
    counts: dict = {"rows": 0, "by_origin": {}, "turn_null": 0, "subagent_no_parent": 0,
                    "activity": 0, "by_kind": {}, "via": {}}
    buf: list[dict] = []
    abuf: list[ActivityEvent] = []

    def flush() -> None:
        if buf and not dry_run:
            send_session_rows(cfg.pg_dsn, buf)      # raises -> the command fails, nothing to undo
        if abuf and not dry_run:
            send_activity_rows(cfg.pg_dsn, abuf)
        buf.clear()
        abuf.clear()

    for name in agents:
        wm: dict = {}
        seen: set[str] = set()                      # same session under two paths: one row
        for _ in range(_MAX_DRAIN_PASSES):
            before = _wm_fp(wm)
            for events, commit in REGISTRY[name](cfg, wm):
                for e in events:
                    e.machine = cfg.machine
                    if isinstance(e, TimeInterval):     # neither a session nor an activity row
                        continue
                    if isinstance(e, ActivityEvent):
                        if activity and valid_ts(e.ts) and e.event_id not in seen:
                            seen.add(e.event_id)
                            counts["activity"] += 1
                            counts["by_kind"][e.kind] = counts["by_kind"].get(e.kind, 0) + 1
                            if e.kind == "steer":
                                v = e.attrs["via"]
                                counts["via"][v] = counts["via"].get(v, 0) + 1
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
                    counts["rows"] += 1
                    origin = row["run_origin"]
                    counts["by_origin"][origin] = counts["by_origin"].get(origin, 0) + 1
                    counts["turn_null"] += row["turn_id"] is None
                    counts["subagent_no_parent"] += (origin == "subagent"
                                                     and row["parent_session_id"] is None)
                    buf.append(row)
                    if len(buf) >= BATCH:
                        flush()
                        if counts["rows"] % (BATCH * 20) == 0:
                            print(f"aut: {name}: {counts['rows']:,} session rows so far",
                                  file=sys.stderr)
                commit()                            # advances only the throwaway wm
            if _wm_fp(wm) == before:
                break
        flush()
        print(f"aut: {name}: {len(seen):,} {'session/activity' if activity else 'session'} rows "
              f"{'counted' if dry_run else 'sent'}", file=sys.stderr)
    return counts


def cmd_status(cfg: Config, agents: tuple[str, ...]) -> None:
    state = load_state()
    print(f"machine       : {cfg.machine}")
    print(f"agents        : {', '.join(agents)}")
    if cfg.has_webhook and not cfg.webhook_secret:
        webhook_state = "url set but SECRET MISSING (scan will refuse)"
    else:
        webhook_state = "configured" if cfg.has_webhook else "NOT set"
    print(f"webhook       : {webhook_state}")
    print(f"pg fallback   : {'configured' if cfg.has_pg else 'not set'}")
    print("watermarks    :")
    for name in agents:
        wm = state.get("collectors", {}).get(name, {})
        if name in ("cursor", "cursor_sdk"):
            detail = "full-scan each run (no watermark)"
        elif name == "cursor_api":
            detail = ("since_ms=" + str(wm["since_ms"])) if "since_ms" in wm else \
                     ("enabled, not yet scanned" if cfg.cursor_api else "disabled (cursor_api=false)")
        elif "chats" in wm:
            detail = f"{len(wm['chats'])} chats, {len(wm.get('sdk', {}))} SDK stores tracked"
        elif name == activitywatch.NAME:
            detail = f"read until {wm['end']}" if "end" in wm else "never scanned"
        elif "files" in wm:
            detail = f"{len(wm['files'])} files tracked"
        else:
            detail = "never scanned"
        print(f"    {name:<12} {detail}")


_MAX_MANUAL = timedelta(hours=16)


def _local(value: str) -> datetime:
    """A datetime typed by the user; no zone means this machine's local time."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"time add: not a date and time: {value!r} (use YYYY-MM-DD HH:MM)") \
            from None
    return dt if dt.tzinfo else dt.astimezone()


def build_manual(cfg: Config, start: str | None = None, end: str | None = None,
                 minutes: int | None = None, at: str | None = None, not_relevant: bool = False,
                 now: datetime | None = None) -> TimeInterval:
    """One manual agents.time_interval row, or SystemExit with the reason it was refused."""
    now = now or datetime.now(timezone.utc)
    if minutes is not None:
        if start or end:
            raise SystemExit("time add: use --start/--end or --minutes, not both")
        end_dt = _local(at) if at else now
        start_dt = end_dt - timedelta(minutes=minutes)
    elif start and end and not at:
        start_dt, end_dt = _local(start), _local(end)
    else:
        raise SystemExit("time add: give --start and --end, or --minutes [--at]")
    if end_dt <= start_dt:
        raise SystemExit("time add: the end must be after the start")
    if end_dt - start_dt > _MAX_MANUAL:
        raise SystemExit("time add: an interval can span at most 16 hours")
    if end_dt > now:                    # hours not yet worked are not active time
        raise SystemExit("time add: the end is in the future")
    s_iso = start_dt.astimezone(timezone.utc).isoformat()
    e_iso = end_dt.astimezone(timezone.utc).isoformat()
    return TimeInterval(event_id=make_event_id("manual", cfg.machine, s_iso, e_iso), ts=s_iso,
                        end_ts=e_iso, source="manual", app=None, title=None, category=None,
                        work_relevant=not not_relevant, attrs={}, machine=cfg.machine)


def cmd_time_add(cfg: Config, args) -> None:
    row = build_manual(cfg, args.start, args.end, args.minutes, args.at, args.not_relevant)
    if not cfg.has_pg:
        raise SystemExit("time add needs transport.pg_dsn")
    _insert_pg(cfg.pg_dsn, [row])
    s, e = (datetime.fromisoformat(x).astimezone() for x in (row.ts, row.end_ts))
    mins = (e - s).total_seconds() / 60
    print(f"added manual interval {s:%Y-%m-%d %H:%M} -> {e:%Y-%m-%d %H:%M} ({mins:.0f} min, "
          f"{'relevant' if row.work_relevant else 'not relevant'})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aut", description="coding-agent token usage tracker")
    parser.add_argument("--agents", help="comma-separated subset override (default: config)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="push new usage since the watermark")
    p_scan.add_argument("--dry-run", action="store_true", help="parse + report, send nothing")
    p_back = sub.add_parser("backfill",
                            help="reset watermark for selected agents, then scan all history")
    p_back.add_argument("--sessions-only", action="store_true",
                        help="send only session rows for all history; watermark untouched")
    p_back.add_argument("--activity-only", action="store_true",
                        help="send only activity rows for all history; watermark untouched")
    p_back.add_argument("--dry-run", action="store_true",
                        help="with --sessions-only/--activity-only: counts only")
    p_back.add_argument("--agents", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub.add_parser("status", help="show config + watermark state")
    p_export = sub.add_parser("export", help="write all usage as CSV (bulk load / offline bridge)")
    p_export.add_argument("--out", help="output CSV file (default: stdout)")
    p_sync = sub.add_parser("sync-prices", help="refresh the API-equivalent model price map")
    p_sync.add_argument("--out", help="output JSON (default: <config-dir>/model_prices.json)")
    p_sync.add_argument("--litellm-url", default=pricing.LITELLM_URL)
    p_sync.add_argument("--models-dev-url", default=pricing.MODELS_DEV_URL)
    p_report = sub.add_parser("report",
                              help="compute showcase metrics from the sink -> metrics.json")
    p_report.add_argument("--out", help="output JSON (default: <config-dir>/metrics.json)")
    p_report.add_argument("--prices", help="price map (default: <config-dir>/model_prices.json)")
    p_report.add_argument("--idle-gap", type=int, default=1800,
                          help="session idle-gap seconds (default 1800)")
    p_time = sub.add_parser("time", help="manual active time")
    time_sub = p_time.add_subparsers(dest="time_cmd", required=True)
    p_tadd = time_sub.add_parser("add", help="record one manual interval (local times)")
    p_tadd.add_argument("--start", help="local start, e.g. '2026-11-03 09:00'")
    p_tadd.add_argument("--end", help="local end")
    p_tadd.add_argument("--minutes", type=int, help="length in minutes, ending at --at")
    p_tadd.add_argument("--at", help="local end for --minutes (default: now)")
    p_tadd.add_argument("--not-relevant", action="store_true",
                        help="not work-relevant (default: relevant)")
    args = parser.parse_args(argv)

    if args.cmd == "sync-prices":
        out = args.out or str(_config_path().parent / "model_prices.json")
        n = pricing.sync_prices(out, litellm_url=args.litellm_url,
                                models_dev_url=args.models_dev_url)
        print(f"synced {n} model prices -> {out}", file=sys.stderr)
        return 0

    cfg = load_config()
    agents = cfg.agents
    if args.agents:
        agents = tuple(a.strip() for a in args.agents.split(",") if a.strip())
        bad = [a for a in agents if a not in REGISTRY]
        if bad:
            parser.error(f"unknown agent(s): {bad}; valid = {list(REGISTRY)}")

    if args.cmd == "report":
        from . import report as report_mod
        out = args.out or str(_config_path().parent / "metrics.json")
        prices = args.prices or str(_config_path().parent / "model_prices.json")
        metrics = report_mod.run_report(cfg.pg_dsn, prices, out,
                                        idle_gap_s=args.idle_gap)
        cov = metrics["priced_coverage"]
        print(f"wrote metrics -> {out}  (priced {cov['models_priced']} models, "
              f"{cov['unpriced_token_share']:.0%} of tokens unpriced)", file=sys.stderr)
        return 0

    if args.cmd == "status":
        cmd_status(cfg, agents)
        return 0

    if args.cmd == "time":
        cmd_time_add(cfg, args)
        return 0

    if args.cmd == "export":
        out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
        try:
            n, skipped = run_export(cfg, list(agents), out)
        finally:
            if args.out:
                out.close()
        dest = f" -> {args.out}" if args.out else ""
        skip_note = f" ({skipped} skipped: missing/invalid ts)" if skipped else ""
        print(f"exported {n} events{skip_note}{dest}", file=sys.stderr)
        return 0

    if args.cmd == "backfill" and (args.sessions_only or args.activity_only):
        c = run_session_backfill(cfg, list(agents), args.dry_run, sessions=args.sessions_only,
                                 activity=args.activity_only)
        mode = "DRY-RUN (nothing sent)" if args.dry_run else "sent"
        if args.sessions_only:
            print(f"sessions-only backfill {mode}: {c['rows']} rows, by run_origin "
                  f"{json.dumps(c['by_origin'], sort_keys=True)}, turn_id NULL {c['turn_null']}, "
                  f"subagent rows without a parent {c['subagent_no_parent']}")
        if args.activity_only:
            print(f"activity-only backfill {mode}: {c['activity']} rows, by kind "
                  f"{json.dumps(c['by_kind'], sort_keys=True)}, steer via "
                  f"{json.dumps(c['via'], sort_keys=True)}")
        return 0
    if args.cmd == "backfill" and args.dry_run:
        parser.error("backfill --dry-run needs --sessions-only or --activity-only (a plain "
                     "backfill resets the watermark; preview it with `aut scan --dry-run`)")

    if args.cmd == "backfill":
        state = load_state()
        for name in agents:
            state.get("collectors", {}).pop(name, None)
        save_state(state)
        print(f"watermark reset for: {', '.join(agents)}")

    dry = getattr(args, "dry_run", False)
    report = run_scan(cfg, list(agents), dry)
    _print_report(cfg, report, dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
